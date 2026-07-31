"""Stage 5a - deterministic execution of a QuerySpec against the ranked backlog.

Every number the chatbot says comes from here. No LLM touches this module.

Ordering of operations, which is the one thing worth getting right:

    rank the WHOLE backlog  ->  then filter  ->  then order/aggregate

not filter-then-rank. Two of the five exposure factors are population-relative -
`age` is a percentile rank and `value` is a percentile of log amount - so
scoring a filtered subset would rescale them against that subset. The top EMEA
alert would then score 1.00 on age simply for being the oldest EMEA alert, and
exposure would stop meaning the same thing across two answers in the same
conversation. Rank once, globally, then filter; the score always reads
"relative to the entire unresolved backlog".

Scope note: ExposureRanker.backlog() is unresolved alerts only
(RESOLUTION_CODE is null). That is the whole population this stage can see, and
every response says so - "12 alerts" must never be mistaken for 12 alerts ever
raised.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.exposure_ranking import ExposureRanker          # noqa: E402
from pipeline.stats.spec import (                        # noqa: E402
    DIMENSIONS, MEASURES, ORDINAL_DIMENSIONS, QuerySpec, Filter,
)

#: Columns returned for intent='list'. Deliberately narrow - an investigator
#: needs the exposure, why it is high, and enough to open the case.
DISPLAY_COLS = [
    "ALERT_ID", "exposure_rank", "exposure", "ALERT_PRIORITY", "ALERT_TYPE",
    "age_days", "band", "CLIENT_REGION_CODE", "DEST_COUNTRY_NAME",
    "DEST_FATF_STATUS", "AMOUNT_USD", "ASSIGNED_TO", "SLA_BREACHED",
    "COMPANY_ID", "TRANSACTION_ID", "CREATED_AT",
]


def _round(x, dp=4):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return None
    return round(float(x), dp)


class StatsEngine:
    """Executes QuerySpecs against one ExposureRanker.

    The ranked frame is computed once and cached; rank() over ~1.5k unresolved
    alerts is milliseconds, but the cache also guarantees that every answer in a
    conversation is drawn from the same snapshot, which matters more.
    """

    def __init__(self, ranker: ExposureRanker | None = None, **ranker_kwargs):
        self.ranker = ranker or ExposureRanker(**ranker_kwargs)
        self._frame: pd.DataFrame | None = None

    # -- frame ------------------------------------------------------------
    def frame(self, recompute: bool = False) -> pd.DataFrame:
        if self._frame is not None and not recompute:
            return self._frame
        B = self.ranker.rank().copy()
        self._frame = self._enrich(B)
        return self._frame

    def _enrich(self, B: pd.DataFrame) -> pd.DataFrame:
        """Rename the geography columns to say which geography they are, and
        attach destination region.

        ExposureRanker's backlog carries COUNTRY_NAME/FATF_STATUS/RISK_TIER from
        the DESTINATION country, and REGION_CODE from the CLIENT's HQ country.
        Two different geographies under names that do not distinguish them is
        exactly the kind of thing a chatbot will get wrong in public, so the
        names become explicit here.
        """
        ren = {"COUNTRY_NAME": "DEST_COUNTRY_NAME",
               "FATF_STATUS": "DEST_FATF_STATUS",
               "RISK_TIER": "DEST_RISK_TIER",
               "REGION_CODE": "CLIENT_REGION_CODE"}
        B = B.rename(columns={k: v for k, v in ren.items() if k in B.columns})

        if "DEST_REGION_CODE" not in B.columns and "DESTINATION_COUNTRY_ID" in B.columns:
            try:
                ctry = self.ranker._load("COUNTRIES")[["COUNTRY_ID", "REGION_ID"]]
                reg = self.ranker._load("REGIONS")[["REGION_ID", "REGION_CODE"]]
                geo = (ctry.merge(reg, on="REGION_ID")
                           .rename(columns={"COUNTRY_ID": "DESTINATION_COUNTRY_ID",
                                            "REGION_CODE": "DEST_REGION_CODE"})
                           [["DESTINATION_COUNTRY_ID", "DEST_REGION_CODE"]])
                B = B.merge(geo, on="DESTINATION_COUNTRY_ID", how="left")
            except Exception:
                pass    # destination region simply unavailable; filters on it return empty

        for c in list(DIMENSIONS) + list(MEASURES):
            if c not in B.columns:
                B[c] = pd.NA
        return B

    # -- filtering --------------------------------------------------------
    @staticmethod
    def _apply(df: pd.DataFrame, f: Filter) -> pd.DataFrame:
        col = df[f.field]
        op, v = f.op, f.value

        if op == "is_null":
            return df[col.isna()]
        if op == "not_null":
            return df[col.notna()]
        if op == "in":
            return df[col.isin(v)]
        if op == "not_in":
            return df[~col.isin(v)]
        if op == "contains":
            return df[col.astype(str).str.contains(str(v), case=False, na=False)]

        if DIMENSIONS.get(f.field) and DIMENSIONS[f.field].kind == "categorical":
            if op == "eq":
                return df[col.astype(str).str.casefold() == str(v).casefold()]
            if op == "ne":
                return df[col.astype(str).str.casefold() != str(v).casefold()]

        if f.field in MEASURES and MEASURES[f.field].kind == "date":
            col = pd.to_datetime(col, errors="coerce")
            v = pd.to_datetime(v) if not isinstance(v, list) else [pd.to_datetime(x) for x in v]

        ops = {"eq": lambda c, x: c == x, "ne": lambda c, x: c != x,
               "gte": lambda c, x: c >= x, "lte": lambda c, x: c <= x,
               "gt": lambda c, x: c > x, "lt": lambda c, x: c < x}
        if op == "between":
            return df[(col >= v[0]) & (col <= v[1])]
        return df[ops[op](col, v)]

    def filtered(self, spec: QuerySpec) -> pd.DataFrame:
        df = self.frame()
        for f in spec.filters:
            df = self._apply(df, f)
        return df

    # -- ordering ---------------------------------------------------------
    @staticmethod
    def _sorted(df: pd.DataFrame, order_by: str, descending: bool) -> pd.DataFrame:
        """Sort by a measure, or by an ordinal categorical with exposure as the
        tiebreak.

        The tiebreak is not a nicety. ALERT_PRIORITY has three levels over ~1.5k
        alerts, so sorting by the label alone leaves 194 CRITICALs in whatever
        order the frame happened to be in, and "top 20 by priority" would return
        an arbitrary 20 of them - stable across runs, but meaningless, and
        wrong in a way nobody would notice. Within a level, exposure decides.
        """
        if order_by in ORDINAL_DIMENSIONS:
            order = list(ORDINAL_DIMENSIONS[order_by].ordinal)
            known = df[order_by].isin(order)
            key = df[order_by].map({v: i for i, v in enumerate(order)})
            if not descending:
                key = (len(order) - 1) - key       # direction baked into the key,
            key = key.where(known, len(order))     # so unknowns stay last either way
            return (df.assign(_ord=key)
                      .sort_values(["_ord", "exposure"], ascending=[True, False])
                      .drop(columns="_ord"))
        return df.sort_values(order_by, ascending=not descending, na_position="last")

    # -- metrics ----------------------------------------------------------
    def _metric(self, g: pd.DataFrame, name: str, total: int, matched: int) -> Any:
        if name == "count":
            return int(len(g))
        if name == "pct_of_backlog":
            return _round(100.0 * len(g) / total, 1) if total else None
        if name == "pct_of_matched":
            # Share of the FILTERED population, which is what a reader of a
            # filtered breakdown assumes a percentage means. Both are offered
            # because they answer different questions and, under a filter, they
            # differ a lot: EMEA CRITICALs are 12.5% of the backlog but 30.9% of
            # EMEA. Reporting only the first next to a region filter invites the
            # reader to do the wrong division in their head.
            return _round(100.0 * len(g) / matched, 1) if matched else None
        if g.empty:
            return None
        if name == "avg_exposure":
            return _round(g["exposure"].mean())
        if name == "max_exposure":
            return _round(g["exposure"].max())
        if name == "sum_exposure":
            return _round(g["exposure"].sum())
        if name == "avg_age_days":
            return _round(g["age_days"].mean(), 1)
        if name == "median_age_days":
            return _round(g["age_days"].median(), 1)
        if name == "max_age_days":
            return _round(g["age_days"].max(), 1)
        if name == "sum_amount_usd":
            return _round(g["AMOUNT_USD"].sum(), 2)
        if name == "avg_amount_usd":
            return _round(g["AMOUNT_USD"].mean(), 2)
        if name == "median_amount_usd":
            return _round(g["AMOUNT_USD"].median(), 2)
        if name == "pct_unassigned":
            return _round(100.0 * g["ASSIGNED_TO"].isna().mean(), 1)
        if name == "pct_sla_breached":
            return _round(100.0 * g["SLA_BREACHED"].fillna(False).astype(bool).mean(), 1)
        if name == "pct_critical":
            return _round(100.0 * g["ALERT_PRIORITY"].eq("CRITICAL").mean(), 1)
        raise ValueError("unknown metric %r" % name)

    # -- banded allocation ------------------------------------------------
    def _banded(self, df: pd.DataFrame, capacity: int) -> pd.DataFrame:
        """Reserve capacity per ageing band instead of taking a flat top-N.

        A flat ranking starves new critical alerts: the aged cohort outnumbers
        everything else, so any weight on age fills the queue with it. Reserving
        10/20/70% across NEW/CURRENT/AGED is capacity allocation, not ranking -
        see ExposureRanker.queues and .starvation for the numbers.
        """
        picked = []
        for name, lo, hi, share in ExposureRanker.BANDS:
            sub = df[(df["age_days"] >= lo) & (df["age_days"] <= hi)]
            k = int(round(share * capacity))
            if k and len(sub):
                picked.append(sub.nlargest(min(k, len(sub)), "exposure"))
        if not picked:
            return df.head(0)
        return pd.concat(picked).sort_values("exposure", ascending=False)

    # -- execution --------------------------------------------------------
    def run(self, spec: QuerySpec) -> dict:
        full = self.frame()
        df = self.filtered(spec)
        n_matched = len(df)

        if spec.intent == "list":
            if spec.allocation == "banded":
                out = self._banded(df, spec.limit)
            else:
                out = self._sorted(df, spec.order_by, spec.descending).head(spec.limit)
            cols = [c for c in DISPLAY_COLS if c in out.columns]
            rows = out[cols].replace({np.nan: None}).to_dict("records")
            for r in rows:
                if isinstance(r.get("CREATED_AT"), pd.Timestamp):
                    r["CREATED_AT"] = r["CREATED_AT"].isoformat()
                if r.get("exposure") is not None:
                    r["exposure"] = _round(r["exposure"])
        else:
            rows = self._aggregate(df, spec, total=len(full), matched=n_matched)

        return {
            "spec": spec.model_dump(),
            "spec_english": spec.describe(),
            "n_matched": n_matched,
            "n_returned": len(rows),
            "rows": rows,
            "provenance": self.provenance(),
        }

    # -- single-record lookup ----------------------------------------------
    def alert(self, alert_id: int) -> dict | None:
        """One alert by ALERT_ID, or None if it is not in the unresolved
        backlog. Bypasses the QuerySpec/Filter surface on purpose - ALERT_ID
        is deliberately not a registered filterable field (spec.py), since
        this stage answers questions OVER the backlog, not per-record key
        lookups. A dedicated method keeps that boundary intact while still
        giving a UI a fast, deterministic "look up this case" path with no
        LLM involved.
        """
        df = self.frame()
        match = df[df["ALERT_ID"] == alert_id]
        if match.empty:
            return None
        cols = [c for c in DISPLAY_COLS if c in match.columns]
        row = match[cols].replace({np.nan: None}).to_dict("records")[0]
        if isinstance(row.get("CREATED_AT"), pd.Timestamp):
            row["CREATED_AT"] = row["CREATED_AT"].isoformat()
        if row.get("exposure") is not None:
            row["exposure"] = _round(row["exposure"])
        return row

    def _aggregate(self, df: pd.DataFrame, spec: QuerySpec, total: int,
                   matched: int) -> list[dict]:
        if not spec.group_by:
            return [{m: self._metric(df, m, total, matched) for m in spec.metrics}]
        keys = list(spec.group_by)
        out = []
        for key, g in df.groupby(keys, dropna=False, observed=False):
            key = key if isinstance(key, tuple) else (key,)
            row = {k: (None if pd.isna(v) else v) for k, v in zip(keys, key)}
            row.update({m: self._metric(g, m, total, matched) for m in spec.metrics})
            out.append(row)
        # An ordered dimension sorts by its own severity order - a breakdown by
        # priority reading CRITICAL, HIGH, MEDIUM is what the reader expects;
        # sorting it by count gives HIGH, CRITICAL, MEDIUM, which looks like a
        # mistake even though the numbers are right.
        if len(keys) == 1 and keys[0] in ORDINAL_DIMENSIONS:
            order = list(ORDINAL_DIMENSIONS[keys[0]].ordinal)
            out.sort(key=lambda r: order.index(r[keys[0]])
                     if r[keys[0]] in order else len(order))
        else:
            sort_metric = spec.metrics[0]
            out.sort(key=lambda r: (r.get(sort_metric) is None, r.get(sort_metric)),
                     reverse=spec.descending)
        return out[:spec.limit]

    # -- standing summary -------------------------------------------------
    def summary(self) -> dict:
        """Precomputed shape of the backlog. Answers "how bad is it" in one hop
        and gives the demo page real numbers instead of placeholders."""
        B = self.frame()
        def brk(col):
            if col not in B.columns:
                return {}
            return {(str(k) if not pd.isna(k) else "UNKNOWN"): int(v)
                    for k, v in B[col].value_counts(dropna=False).items()}
        return {
            "backlog": int(len(B)),
            "scope": "unresolved alerts only (RESOLUTION_CODE is null)",
            "as_of": str(self.ranker.as_of.date()),
            "by_priority": brk("ALERT_PRIORITY"),
            "by_type": brk("ALERT_TYPE"),
            "by_client_region": brk("CLIENT_REGION_CODE"),
            "by_dest_fatf_status": brk("DEST_FATF_STATUS"),
            "by_band": brk("band"),
            "unassigned": int(B["ASSIGNED_TO"].isna().sum()),
            "pct_unassigned": _round(100.0 * B["ASSIGNED_TO"].isna().mean(), 1),
            "pct_sla_breached": _round(
                100.0 * B["SLA_BREACHED"].fillna(False).astype(bool).mean(), 1),
            "exposure": {
                "mean": _round(B["exposure"].mean()),
                "p50": _round(B["exposure"].median()),
                "p90": _round(B["exposure"].quantile(0.90)),
                "max": _round(B["exposure"].max()),
            },
            "age_days": {
                "median": _round(B["age_days"].median(), 1),
                "p90": _round(B["age_days"].quantile(0.90), 1),
                "max": _round(B["age_days"].max(), 1),
            },
            "capacity": self.ranker.capacity_plan(),
        }

    def provenance(self) -> dict:
        """What an examiner needs to reproduce any number in the response."""
        return {
            "source": "TEAM_07.RISK_ALERTS joined to TRANSACTIONS, COUNTRIES, "
                      "COMPANIES, REGIONS; ranked by src.exposure_ranking.ExposureRanker",
            "scope": "unresolved alerts only (RESOLUTION_CODE is null)",
            "population": int(len(self.frame())),
            "as_of": str(self.ranker.as_of.date()),
            "exposure_weights": dict(self.ranker.weights),
            "weights_basis": "policy-set, not fitted against outcomes",
            "ranked_over": "the full unresolved backlog, before any filter",
        }


if __name__ == "__main__":
    import json
    e = StatsEngine()
    print(json.dumps(e.summary(), indent=2, default=str))
