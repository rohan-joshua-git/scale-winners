"""Stage 5a - the query contract between the chatbot and the alert backlog.

Everything an analyst can ask about the backlog has to be expressible as a
QuerySpec. That is the point, not an inconvenience:

  The LLM never computes a number and never writes SQL. It fills in this schema
  and nothing else. A deterministic executor (engine.StatsEngine) then does the
  arithmetic against ExposureRanker's frame. So the same governance argument
  that covers risk_drivers.py covers this stage - the ordering is policy-set and
  reproducible, and the language model is confined to (a) picking which
  pre-approved question was asked and (b) reading the computed answer aloud.

  Pydantic gives that guarantee for free. Any field, operator or metric outside
  the Literals below fails validation, so a hallucinated column name or an
  injected SQL fragment is a 422 rather than a wrong number in front of an
  examiner.

Two ambiguities in the domain are resolved here rather than in a prompt, because
they are the ones that would silently produce a wrong-but-plausible answer:

  "priority"  ->  ExposureRanker.exposure, NOT the ALERT_PRIORITY label.
                  ALERT_PRIORITY carries weight 0.20 of five factors. Ranking by
                  the label alone displaces ~84% of the queue relative to
                  exposure (ExposureRanker.displacement) and is not what anyone
                  means by "which cases matter most". The resolution is always
                  reported back in the response so the user can override.

  "region"    ->  CLIENT_REGION_CODE (the client's HQ region) by default, since
                  that is the geography supervision attaches to and the one
                  ExposureRanker's supervisory factor uses. DEST_REGION_CODE
                  (counterparty geography) is a separate, addressable field.
                  Asking for "region" logs an assumption; naming either field
                  explicitly does not.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

# --------------------------------------------------------------------------
# field registry
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class FieldDef:
    name: str
    kind: Literal["categorical", "numeric", "date", "boolean"]
    description: str
    values: tuple[str, ...] = ()          # closed domain, if there is one
    aliases: tuple[str, ...] = ()         # words a user might say instead
    value_aliases: dict = dc_field(default_factory=dict)
    #: Severity order, worst first. A categorical with an ordinal is sortable,
    #: so "order by alert priority" is expressible instead of being silently
    #: reinterpreted as something else. Ties inside a level break by exposure -
    #: 194 alerts share the label CRITICAL, so the label alone does not order
    #: them and pretending otherwise would return an arbitrary 20 of them.
    ordinal: tuple[str, ...] = ()


#: Dimensions - filterable and groupable.
DIMENSIONS: dict[str, FieldDef] = {
    "ALERT_PRIORITY": FieldDef(
        "ALERT_PRIORITY", "categorical",
        "Raw priority label assigned at alert creation. One of five exposure "
        "factors (weight 0.20); not the same thing as exposure.",
        values=("CRITICAL", "HIGH", "MEDIUM"),
        aliases=("priority label", "severity", "alert priority", "criticality"),
        value_aliases={"critical": "CRITICAL", "high": "HIGH", "medium": "MEDIUM",
                       "med": "MEDIUM", "urgent": "CRITICAL"},
        ordinal=("CRITICAL", "HIGH", "MEDIUM"),
    ),
    "ALERT_TYPE": FieldDef(
        "ALERT_TYPE", "categorical", "What tripped the alert.",
        values=("VELOCITY_ANOMALY", "COUNTERPARTY_RISK", "SUSPICIOUS_PATTERN",
                "HIGH_RISK_GEOGRAPHY", "THRESHOLD_BREACH"),
        aliases=("type", "alert type", "category", "reason", "trigger"),
        value_aliases={"velocity": "VELOCITY_ANOMALY", "counterparty": "COUNTERPARTY_RISK",
                       "suspicious": "SUSPICIOUS_PATTERN", "geography": "HIGH_RISK_GEOGRAPHY",
                       "geographic": "HIGH_RISK_GEOGRAPHY", "threshold": "THRESHOLD_BREACH"},
    ),
    "ALERT_SUBTYPE": FieldDef("ALERT_SUBTYPE", "categorical", "Subtype of the trigger.",
                              aliases=("subtype",)),
    "ALERT_SOURCE": FieldDef("ALERT_SOURCE", "categorical", "System that raised the alert.",
                             aliases=("source", "raised by")),
    "STATUS": FieldDef("STATUS", "categorical", "Workflow status of the alert.",
                       aliases=("state", "workflow status")),
    "CLIENT_REGION_CODE": FieldDef(
        "CLIENT_REGION_CODE", "categorical",
        "Region of the client's headquarters country. This is the geography "
        "supervision attaches to, and the one ExposureRanker's optional "
        "supervisory factor keys on (EMEA/APAC).",
        values=("APAC", "EMEA", "AMER", "LATAM", "GLOBAL"),
        aliases=("region", "client region", "hq region", "booking region",
                 "where the client is"),
        value_aliases={
            "apac": "APAC", "asia": "APAC", "asia pacific": "APAC", "asia-pacific": "APAC",
            "singapore": "APAC", "emea": "EMEA", "europe": "EMEA", "european": "EMEA",
            "middle east": "EMEA", "africa": "EMEA", "amer": "AMER", "americas": "AMER",
            "north america": "AMER", "us": "AMER", "usa": "AMER", "united states": "AMER",
            "latam": "LATAM", "latin america": "LATAM", "south america": "LATAM",
            "global": "GLOBAL",
        },
    ),
    "DEST_REGION_CODE": FieldDef(
        "DEST_REGION_CODE", "categorical",
        "Region of the transaction's destination country - counterparty "
        "geography, distinct from where the client is booked.",
        values=("APAC", "EMEA", "AMER", "LATAM", "GLOBAL"),
        aliases=("destination region", "counterparty region", "sending to",
                 "beneficiary region"),
        value_aliases={},   # filled below from CLIENT_REGION_CODE
    ),
    "DEST_COUNTRY_NAME": FieldDef(
        "DEST_COUNTRY_NAME", "categorical", "Destination country of the transaction.",
        aliases=("country", "destination country", "counterparty country"),
    ),
    "DEST_FATF_STATUS": FieldDef(
        "DEST_FATF_STATUS", "categorical",
        "FATF standing of the destination country. Drives the jurisdiction "
        "factor (weight 0.25).",
        values=("BLACK_LIST", "GREY_LIST", "NON_COMPLIANT", "MEMBER"),
        aliases=("fatf", "fatf status", "jurisdiction", "jurisdiction risk"),
        value_aliases={"black list": "BLACK_LIST", "blacklist": "BLACK_LIST",
                       "blacklisted": "BLACK_LIST", "grey list": "GREY_LIST",
                       "greylist": "GREY_LIST", "gray list": "GREY_LIST",
                       "non compliant": "NON_COMPLIANT", "member": "MEMBER"},
        # Matches ExposureRanker.FATF_WEIGHT ordering (1.00/0.80/0.60/0.15),
        # not alphabetical and not the order they appear in the source table.
        ordinal=("BLACK_LIST", "NON_COMPLIANT", "GREY_LIST", "MEMBER"),
    ),
    "DEST_RISK_TIER": FieldDef("DEST_RISK_TIER", "categorical",
                               "Internal risk tier of the destination country.",
                               values=("HIGH", "MEDIUM", "LOW"),
                               aliases=("risk tier", "tier"),
                               value_aliases={"high": "HIGH", "medium": "MEDIUM",
                                              "low": "LOW"},
                               ordinal=("HIGH", "MEDIUM", "LOW")),
    "band": FieldDef(
        "band", "categorical",
        "Ageing band used for capacity allocation.",
        values=("NEW (<=30d)", "CURRENT (31-365d)", "AGED (>1yr)"),
        aliases=("age band", "ageing band", "aging band", "vintage"),
        value_aliases={"new": "NEW (<=30d)", "current": "CURRENT (31-365d)",
                       "aged": "AGED (>1yr)", "old": "AGED (>1yr)"},
        ordinal=("AGED (>1yr)", "CURRENT (31-365d)", "NEW (<=30d)"),
    ),
    "ASSIGNED_TO": FieldDef("ASSIGNED_TO", "categorical",
                            "Analyst the alert is assigned to; null = unassigned.",
                            aliases=("analyst", "owner", "assignee", "assigned")),
    "SLA_BREACHED": FieldDef("SLA_BREACHED", "boolean", "Whether the SLA has been breached.",
                             aliases=("sla", "sla breach", "breached", "overdue")),
}

DIMENSIONS["DEST_REGION_CODE"].value_aliases.update(
    DIMENSIONS["CLIENT_REGION_CODE"].value_aliases)

#: Measures - filterable and orderable, never groupable.
MEASURES: dict[str, FieldDef] = {
    "exposure": FieldDef(
        "exposure", "numeric",
        "ExposureRanker composite in [0,1]: age 0.30, jurisdiction 0.25, "
        "priority 0.20, value 0.15, unassigned 0.10. Policy-set weights, never "
        "fitted. Computed once over the whole unresolved backlog so the number "
        "stays comparable under any filter.",
        aliases=("priority", "risk", "urgency", "importance", "exposure score",
                 "risk score", "score"),
    ),
    "exposure_rank": FieldDef("exposure_rank", "numeric",
                              "1 = highest exposure in the backlog.",
                              aliases=("rank", "position")),
    "age_days": FieldDef("age_days", "numeric", "Days since CREATED_AT, as of the ranker's as_of date.",
                         aliases=("age", "days open", "how old", "ageing", "aging")),
    "AMOUNT_USD": FieldDef("AMOUNT_USD", "numeric", "Transaction value in USD.",
                           aliases=("amount", "value", "size", "usd", "notional")),
    "CREATED_AT": FieldDef("CREATED_AT", "date", "When the alert was raised.",
                           aliases=("raised", "created", "opened", "date")),
}

FILTERABLE = {**DIMENSIONS, **MEASURES}
GROUPABLE = set(DIMENSIONS)

#: Ordered categoricals are sortable. Without this, "order by alert priority"
#: has no valid spec and the extractor is pushed into reinterpreting the request
#: as something it can express - which is precisely the failure mode the spec
#: exists to prevent. An unexpressible request should clarify, never mutate.
ORDINAL_DIMENSIONS = {n: f for n, f in DIMENSIONS.items() if f.ordinal}
ORDERABLE = set(MEASURES) | set(ORDINAL_DIMENSIONS)

DimensionName = Literal[tuple(DIMENSIONS)]                  # type: ignore[valid-type]
FilterField = Literal[tuple(FILTERABLE)]                    # type: ignore[valid-type]
OrderField = Literal[tuple(sorted(ORDERABLE))]              # type: ignore[valid-type]

Metric = Literal[
    "count", "pct_of_backlog", "pct_of_matched",
    "avg_exposure", "max_exposure", "sum_exposure",
    "avg_age_days", "median_age_days", "max_age_days",
    "sum_amount_usd", "avg_amount_usd", "median_amount_usd",
    "pct_unassigned", "pct_sla_breached", "pct_critical",
]

Op = Literal["eq", "ne", "in", "not_in", "gte", "lte", "gt", "lt",
             "between", "is_null", "not_null", "contains"]

_NO_VALUE_OPS = {"is_null", "not_null"}
_LIST_OPS = {"in", "not_in"}


# --------------------------------------------------------------------------
# synonym resolution
# --------------------------------------------------------------------------

_ALIAS_INDEX: dict[str, str] = {}
for _reg in (MEASURES, DIMENSIONS):        # measures first: "priority" -> exposure
    for _name, _fd in _reg.items():
        _ALIAS_INDEX.setdefault(_name.lower(), _name)
        for _a in _fd.aliases:
            _ALIAS_INDEX.setdefault(_a.lower(), _name)

#: Phrases whose resolution we always disclose, because a different reading is
#: defensible and the user did not pick one.
AMBIGUOUS = {
    "priority": ("exposure",
                 "Read as the composite exposure score, not the ALERT_PRIORITY "
                 "label. Ask for ALERT_PRIORITY explicitly to rank by label."),
    "severity": ("ALERT_PRIORITY",
                 "Read as the ALERT_PRIORITY label. Ask for exposure to rank by "
                 "the composite score."),
    "region": ("CLIENT_REGION_CODE",
               "Read as the client's HQ region. Ask for DEST_REGION_CODE for "
               "counterparty geography."),
    "risk": ("exposure", "Read as the composite exposure score."),
    "urgent": ("exposure", "Read as the composite exposure score."),
}


def resolve_field(term: str) -> str | None:
    """Map a user's word to a registry field name, or None if unrecognised."""
    return _ALIAS_INDEX.get(str(term).strip().lower())


def resolve_value(field_name: str, value: Any) -> Any:
    """Normalise a filter value into the field's own vocabulary.

    Unrecognised values pass through untouched rather than being coerced - a
    filter that matches nothing is a truthful empty result, while a silent
    coercion to the nearest label is a wrong answer wearing a confident face.
    """
    fd = FILTERABLE.get(field_name)
    if fd is None or not isinstance(value, str):
        return value
    v = value.strip()
    if fd.value_aliases:
        hit = fd.value_aliases.get(v.lower())
        if hit is not None:
            return hit
    if fd.values:
        for allowed in fd.values:
            if allowed.lower() == v.lower():
                return allowed
    return v


# --------------------------------------------------------------------------
# the spec
# --------------------------------------------------------------------------

class Filter(BaseModel):
    field: FilterField                                    # type: ignore[valid-type]
    op: Op = "eq"
    value: str | float | bool | list[str | float] | None = None

    @model_validator(mode="after")
    def _check(self) -> "Filter":
        if self.op in _NO_VALUE_OPS:
            object.__setattr__(self, "value", None)
            return self
        if self.value is None:
            raise ValueError("op %r requires a value" % self.op)
        if self.op in _LIST_OPS and not isinstance(self.value, list):
            object.__setattr__(self, "value", [self.value])
        if self.op == "between":
            if not isinstance(self.value, list) or len(self.value) != 2:
                raise ValueError("op 'between' requires a two-element [low, high]")
        if isinstance(self.value, list):
            object.__setattr__(self, "value",
                               [resolve_value(self.field, v) for v in self.value])
        else:
            object.__setattr__(self, "value", resolve_value(self.field, self.value))
        return self

    def describe(self) -> str:
        words = {"eq": "=", "ne": "!=", "in": "in", "not_in": "not in", "gte": ">=",
                 "lte": "<=", "gt": ">", "lt": "<", "between": "between",
                 "is_null": "is unassigned/null", "not_null": "is set",
                 "contains": "contains"}
        if self.op in _NO_VALUE_OPS:
            return "%s %s" % (self.field, words[self.op])
        return "%s %s %s" % (self.field, words[self.op], self.value)


class QuerySpec(BaseModel):
    """The full surface area of what the chatbot may ask for.

    If a question cannot be expressed here, the right move is to say so - see
    nl.extract_spec's 'clarify' path - not to improvise a query.
    """

    intent: Literal["list", "aggregate"] = "list"
    filters: list[Filter] = Field(default_factory=list)
    group_by: list[DimensionName] = Field(default_factory=list)   # type: ignore[valid-type]
    metrics: list[Metric] = Field(default_factory=lambda: ["count"])
    order_by: OrderField = "exposure"                             # type: ignore[valid-type]
    descending: bool = True
    limit: int = Field(20, ge=1, le=500)
    allocation: Literal["flat", "banded"] = "flat"

    @model_validator(mode="after")
    def _check(self) -> "QuerySpec":
        if self.intent == "aggregate" and not self.metrics:
            raise ValueError("aggregate queries need at least one metric")
        if self.intent == "list" and self.group_by:
            raise ValueError("group_by is only meaningful for intent='aggregate'")
        if self.allocation == "banded" and self.intent != "list":
            raise ValueError("banded allocation applies to intent='list'")
        return self

    def describe(self) -> str:
        """One-line English rendering, for the audit trail and the UI."""
        where = " and ".join(f.describe() for f in self.filters) or "all unresolved alerts"
        if self.intent == "aggregate":
            by = (" by " + ", ".join(self.group_by)) if self.group_by else ""
            return "%s of %s%s" % (", ".join(self.metrics), where, by)
        if self.allocation == "banded":
            how = "banded capacity allocation across ageing bands"
        elif self.order_by in ORDINAL_DIMENSIONS:
            how = "%s (%s), ties broken by exposure" % (
                self.order_by,
                " > ".join(ORDINAL_DIMENSIONS[self.order_by].ordinal)
                if self.descending else
                " < ".join(ORDINAL_DIMENSIONS[self.order_by].ordinal))
        else:
            how = "%s %s" % (self.order_by, "descending" if self.descending else "ascending")
        return "top %d of %s, ordered by %s" % (self.limit, where, how)


def field_catalogue() -> dict:
    """Machine-readable description of the query surface.

    Serves two consumers: the LLM prompt in nl.py (so the vocabulary is
    generated from the registry rather than duplicated in prose that drifts),
    and a /stats/fields endpoint for the UI.
    """
    def pack(reg):
        return {n: {"kind": f.kind, "description": f.description,
                    "values": list(f.values), "aliases": list(f.aliases)}
                for n, f in reg.items()}
    return {"dimensions": pack(DIMENSIONS), "measures": pack(MEASURES),
            "metrics": list(Metric.__args__), "operators": list(Op.__args__)}
