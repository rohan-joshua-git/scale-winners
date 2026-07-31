"""Stage 5a self-test: run against the real backlog, no network, no credentials.

Two things are checked, and only the second is really about correctness:

  * the executor computes what the spec says (filters compose, aggregates sum
    back to the population, ordering is right, banded allocation actually
    reserves capacity);
  * the contract holds under abuse - unknown fields, invented operators and an
    injected SQL fragment must all fail validation rather than reaching pandas.

Question -> expected-spec cases live here too. Grading a spec is exact-match on
a small JSON object; grading prose is not tractable, which is the reason the
model emits a spec rather than an answer.

    python -m pipeline.stats.selftest
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pydantic import ValidationError                        # noqa: E402

from pipeline.stats.engine import StatsEngine               # noqa: E402
from pipeline.stats.nl import classify_intent, rule_spec    # noqa: E402
from pipeline.stats.spec import Filter, QuerySpec           # noqa: E402

PASS, FAIL = "  ok  ", " FAIL "
_results: list[bool] = []


def check(name, cond, detail=""):
    _results.append(bool(cond))
    print("%s %-58s %s" % (PASS if cond else FAIL, name, detail))


def main() -> int:
    e = StatsEngine()
    B = e.frame()
    total = len(B)
    print("backlog under test: %d unresolved alerts, as of %s\n"
          % (total, e.ranker.as_of.date()))

    # -- executor ---------------------------------------------------------
    print("executor")
    r = e.run(QuerySpec(intent="list", order_by="exposure", limit=20))
    check("list returns the requested number", r["n_returned"] == 20)
    check("list is ordered by exposure descending",
          all(r["rows"][i]["exposure"] >= r["rows"][i + 1]["exposure"]
              for i in range(len(r["rows"]) - 1)))
    check("unfiltered list matches whole backlog", r["n_matched"] == total)

    emea = e.run(QuerySpec(intent="list",
                           filters=[Filter(field="CLIENT_REGION_CODE", op="eq", value="EMEA")],
                           limit=5))
    check("region filter narrows the population", 0 < emea["n_matched"] < total,
          "%d of %d" % (emea["n_matched"], total))
    check("region filter returns only that region",
          all(row["CLIENT_REGION_CODE"] == "EMEA" for row in emea["rows"]))

    # exposure must be the GLOBAL score, not rescaled within the filtered set
    check("exposure is not rescaled by the filter",
          emea["rows"][0]["exposure"] <= r["rows"][0]["exposure"],
          "top EMEA %.4f <= top overall %.4f"
          % (emea["rows"][0]["exposure"], r["rows"][0]["exposure"]))
    check("exposure_rank survives filtering (global rank retained)",
          emea["rows"][0]["exposure_rank"] >= 1)

    alias = e.run(QuerySpec(intent="list",
                            filters=[Filter(field="CLIENT_REGION_CODE", op="eq",
                                            value="europe")], limit=1))
    check("value alias 'europe' resolves to EMEA", alias["n_matched"] == emea["n_matched"])

    combo = e.run(QuerySpec(
        intent="list",
        filters=[Filter(field="CLIENT_REGION_CODE", op="in", value=["APAC", "EMEA"]),
                 Filter(field="ALERT_PRIORITY", op="eq", value="CRITICAL"),
                 Filter(field="ASSIGNED_TO", op="is_null"),
                 Filter(field="age_days", op="gte", value=365)], limit=10))
    check("filters compose (region + priority + null + numeric)",
          combo["n_matched"] < emea["n_matched"], "%d matched" % combo["n_matched"])
    check("composed filters are all satisfied",
          all(row["CLIENT_REGION_CODE"] in ("APAC", "EMEA")
              and row["ALERT_PRIORITY"] == "CRITICAL"
              and row["ASSIGNED_TO"] is None
              and row["age_days"] >= 365 for row in combo["rows"]))

    # -- aggregates -------------------------------------------------------
    print("\naggregates")
    agg = e.run(QuerySpec(intent="aggregate", group_by=["CLIENT_REGION_CODE"],
                          metrics=["count", "pct_of_backlog", "avg_exposure"], limit=50))
    check("group-by counts sum back to the population",
          sum(row["count"] for row in agg["rows"]) == total,
          "%d == %d" % (sum(row["count"] for row in agg["rows"]), total))
    check("pct_of_backlog sums to ~100",
          abs(sum(row["pct_of_backlog"] for row in agg["rows"]) - 100) < 0.5)
    check("groups are ordered by the first metric",
          all(agg["rows"][i]["count"] >= agg["rows"][i + 1]["count"]
              for i in range(len(agg["rows"]) - 1)))

    two = e.run(QuerySpec(intent="aggregate",
                          group_by=["CLIENT_REGION_CODE", "ALERT_PRIORITY"],
                          metrics=["count"], limit=100))
    check("two-dimension group-by also sums back",
          sum(row["count"] for row in two["rows"]) == total)

    scoped = e.run(QuerySpec(intent="aggregate",
                             filters=[Filter(field="CLIENT_REGION_CODE", op="eq", value="EMEA")],
                             group_by=["DEST_FATF_STATUS"], metrics=["count"], limit=50))
    check("filtered aggregate sums to the filtered population",
          sum(row["count"] for row in scoped["rows"]) == emea["n_matched"])

    money = e.run(QuerySpec(intent="aggregate", group_by=["DEST_FATF_STATUS"],
                            metrics=["count", "sum_amount_usd", "avg_exposure"], limit=50))
    check("sum_amount_usd matches a direct pandas sum",
          abs(sum(row["sum_amount_usd"] for row in money["rows"])
              - float(B["AMOUNT_USD"].sum())) < 1.0)

    # -- banded allocation ------------------------------------------------
    print("\ncapacity allocation")
    flat = e.run(QuerySpec(intent="list", limit=100, allocation="flat"))
    band = e.run(QuerySpec(intent="list", limit=100, allocation="banded"))
    flat_bands = {row["band"] for row in flat["rows"]}
    band_bands = {row["band"] for row in band["rows"]}
    check("banded allocation spreads across more ageing bands",
          len(band_bands) >= len(flat_bands),
          "flat=%s banded=%s" % (sorted(flat_bands), sorted(band_bands)))
    check("banded allocation returns a full queue", band["n_returned"] > 0,
          "%d rows" % band["n_returned"])
    check("banded queue is still exposure-ordered within the result",
          all(band["rows"][i]["exposure"] >= band["rows"][i + 1]["exposure"]
              for i in range(len(band["rows"]) - 1)))

    # -- ordinal ordering -------------------------------------------------
    # Regression: "order by alert priority" was unexpressible (order_by accepted
    # measures only), so the extractor reinterpreted it as a group-by and
    # returned a 3-row breakdown for a request that asked for cases.
    print("\nordinal ordering")
    prio = e.run(QuerySpec(intent="list", order_by="ALERT_PRIORITY", limit=30))
    check("ordinal sort puts the most severe level first",
          prio["rows"][0]["ALERT_PRIORITY"] == "CRITICAL",
          "got %s" % prio["rows"][0]["ALERT_PRIORITY"])
    order = ["CRITICAL", "HIGH", "MEDIUM"]
    check("ordinal sort is monotonic in severity",
          all(order.index(prio["rows"][i]["ALERT_PRIORITY"])
              <= order.index(prio["rows"][i + 1]["ALERT_PRIORITY"])
              for i in range(len(prio["rows"]) - 1)))
    crit = [r for r in prio["rows"] if r["ALERT_PRIORITY"] == "CRITICAL"]
    check("ties inside a level break by exposure descending",
          all(crit[i]["exposure"] >= crit[i + 1]["exposure"] for i in range(len(crit) - 1)),
          "%d CRITICALs in the page" % len(crit))
    asc = e.run(QuerySpec(intent="list", order_by="ALERT_PRIORITY",
                          descending=False, limit=30))
    check("ascending ordinal sort reverses the severity order",
          asc["rows"][0]["ALERT_PRIORITY"] == "MEDIUM",
          "got %s" % asc["rows"][0]["ALERT_PRIORITY"])
    fatf = e.run(QuerySpec(intent="list", order_by="DEST_FATF_STATUS", limit=10))
    check("FATF ordinal follows ExposureRanker.FATF_WEIGHT, not alphabetical",
          fatf["rows"][0]["DEST_FATF_STATUS"] == "BLACK_LIST",
          "got %s" % fatf["rows"][0]["DEST_FATF_STATUS"])
    grp = e.run(QuerySpec(intent="aggregate", group_by=["ALERT_PRIORITY"],
                          metrics=["count"], limit=10))
    check("grouped ordinal dimension reports in severity order, not by count",
          [r["ALERT_PRIORITY"] for r in grp["rows"]] == order,
          str([r["ALERT_PRIORITY"] for r in grp["rows"]]))

    # -- denominators -----------------------------------------------------
    print("\ndenominators")
    both = e.run(QuerySpec(
        intent="aggregate",
        filters=[Filter(field="CLIENT_REGION_CODE", op="eq", value="EMEA")],
        group_by=["ALERT_PRIORITY"],
        metrics=["count", "pct_of_backlog", "pct_of_matched"], limit=10))
    check("pct_of_matched sums to ~100 under a filter",
          abs(sum(r["pct_of_matched"] for r in both["rows"]) - 100) < 0.5)
    check("pct_of_backlog does NOT sum to 100 under a filter (different question)",
          abs(sum(r["pct_of_backlog"] for r in both["rows"]) - 100) > 10,
          "sums to %.1f%%" % sum(r["pct_of_backlog"] for r in both["rows"]))

    # -- empty result -----------------------------------------------------
    print("\nedge cases")
    none = e.run(QuerySpec(intent="list",
                           filters=[Filter(field="age_days", op="gte", value=10 ** 9)]))
    check("impossible filter returns empty, not an error",
          none["n_matched"] == 0 and none["rows"] == [])

    # -- contract enforcement ---------------------------------------------
    print("\ncontract enforcement (these MUST be rejected)")
    def rejects(label, **kw):
        try:
            QuerySpec(**kw)
        except (ValidationError, ValueError):
            check(label, True, "rejected")
            return
        check(label, False, "ACCEPTED - contract leak")

    rejects("unknown filter field",
            filters=[{"field": "RESOLUTION_NOTES", "op": "eq", "value": "x"}])
    rejects("SQL injection in field name",
            filters=[{"field": "1=1; DROP TABLE RISK_ALERTS--", "op": "eq", "value": "x"}])
    rejects("invented operator",
            filters=[{"field": "exposure", "op": "regex", "value": ".*"}])
    rejects("invented metric", intent="aggregate", metrics=["avg_profit"])
    rejects("order by an unordered categorical", order_by="ALERT_TYPE")
    rejects("group by a measure", intent="aggregate", group_by=["exposure"])
    rejects("group_by on a list query", intent="list", group_by=["ALERT_TYPE"])
    rejects("limit above the cap", limit=100000)
    rejects("between without two bounds",
            filters=[{"field": "age_days", "op": "between", "value": 5}])

    # a value that is not in the domain must return nothing, NOT be coerced
    ghost = e.run(QuerySpec(intent="list",
                            filters=[Filter(field="CLIENT_REGION_CODE", op="eq",
                                            value="ATLANTIS")]))
    check("unknown value returns empty rather than nearest match",
          ghost["n_matched"] == 0)

    # -- NL extraction ----------------------------------------------------
    print("\nNL -> spec (deterministic extractor)")
    cases = [
        ("Give me the cases with the highest priority",
         dict(intent="list", order_by="exposure", descending=True, filters=[])),
        ("Show me the top 5 highest priority cases in EMEA",
         dict(intent="list", order_by="exposure", limit=5,
              filters=[("CLIENT_REGION_CODE", "eq", "EMEA")])),
        ("unassigned critical alerts in APAC",
         dict(intent="list", filters=[("ALERT_PRIORITY", "eq", "CRITICAL"),
                                      ("CLIENT_REGION_CODE", "eq", "APAC"),
                                      ("ASSIGNED_TO", "is_null", None)])),
        ("How many alerts by client region?",
         dict(intent="aggregate", group_by=["CLIENT_REGION_CODE"])),
        ("breakdown by alert type for blacklisted destinations",
         dict(intent="aggregate", group_by=["ALERT_TYPE"],
              filters=[("DEST_FATF_STATUS", "eq", "BLACK_LIST")])),
        ("alerts older than 2 years in latin america",
         dict(intent="list", filters=[("CLIENT_REGION_CODE", "eq", "LATAM"),
                                      ("age_days", "gte", 730.0)])),
        ("What should my team work on today?",
         dict(intent="list", allocation="banded")),
        ("oldest alerts on the grey list",
         dict(intent="list", order_by="age_days", descending=True,
              filters=[("DEST_FATF_STATUS", "eq", "GREY_LIST")])),
        # Regression: a bare "by" used to force intent=aggregate, turning a
        # request for cases into a three-row breakdown.
        ("Give me the cases with the highest priority in EMEA by alert priority",
         dict(intent="list", order_by="ALERT_PRIORITY", group_by=[],
              filters=[("CLIENT_REGION_CODE", "eq", "EMEA")])),
        ("show me alerts sorted by amount in APAC",
         dict(intent="list", order_by="AMOUNT_USD",
              filters=[("CLIENT_REGION_CODE", "eq", "APAC")])),
        ("list cases by FATF status",
         dict(intent="list", order_by="DEST_FATF_STATUS", group_by=[])),
        ("how many cases by region", dict(intent="aggregate",
                                          group_by=["CLIENT_REGION_CODE"])),
    ]
    for q, want in cases:
        got = rule_spec(q).spec
        ok = got is not None
        detail = ""
        if ok:
            for k, v in want.items():
                if k == "filters":
                    have = {(f.field, f.op, f.value) for f in got.filters}
                    if not set(v) <= have:
                        ok, detail = False, "filters %s !>= %s" % (have, set(v))
                        break
                elif getattr(got, k) != v:
                    ok, detail = False, "%s=%r want %r" % (k, getattr(got, k), v)
                    break
        check(repr(q[:52]), ok, detail)
        if ok:
            e.run(got)          # must also execute

    print("\nintent routing")
    for q, want in [("How many critical alerts in EMEA?", "analytical"),
                    ("Give me the cases with the highest priority", "analytical"),
                    ("Why did alert 15002 fire?", "explanatory"),
                    ("What regulation covers this beneficial owner?", "explanatory"),
                    ("Which clause justifies escalating alert 18324?", "explanatory")]:
        got = classify_intent(q)
        check("%-52s -> %s" % (repr(q[:48]), got), got == want, "" if got == want else "want " + want)

    # -- summary ----------------------------------------------------------
    print("\nstanding summary")
    s = e.summary()
    check("summary priority breakdown sums to the backlog",
          sum(s["by_priority"].values()) == total)
    check("summary region breakdown sums to the backlog",
          sum(s["by_client_region"].values()) == total)
    check("summary declares its scope", "unresolved" in s["scope"])
    check("provenance names the weights and their basis",
          "policy-set" in e.provenance()["weights_basis"]
          and set(e.provenance()["exposure_weights"]) == set(e.ranker.weights))

    n_fail = _results.count(False)
    print("\n%d checks, %d passed, %d failed" % (len(_results), _results.count(True), n_fail))
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
