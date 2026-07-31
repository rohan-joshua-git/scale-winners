# Stage 5a — Backlog Analytics

Structured, natural-language querying of the unresolved alert backlog, ranked by
`src.exposure_ranking.ExposureRanker`. Companion to Stage 4 (`pipeline/rag`),
not a replacement for it:

| Question | Stage | Engine |
|---|---|---|
| "Give me the cases with the highest priority" | **5a** | `StatsEngine` over the ranked backlog |
| "How many unresolved alerts in EMEA, by FATF status?" | **5a** | same |
| "Why did alert 15002 fire?" | 4 | `pipeline.rag.retrieve` (vector + graph) |
| "What clause covers this beneficial owner?" | 4 | same |

`/chat` routes between them. Aggregate questions answered by vector search over
regulatory text return something fluent and wrong, so they are not sent there.

## The governing constraint

**The language model never computes a number and never writes SQL.** It does two
things, both bounded:

1. Fills in a `QuerySpec` — a closed Pydantic schema of whitelisted fields,
   operators and metrics (`spec.py`).
2. Reads aloud rows that `StatsEngine` already computed (`nl.narrate`).

Everything between those two points is deterministic pandas over
`ExposureRanker.rank()`. A hallucinated column, an invented operator or an
injected SQL fragment fails schema validation and returns 422 — it cannot reach
the executor. This is the same separation `risk_drivers.py` keeps between
policy-set severities and narration, and it is what keeps this stage on the
light governance path.

## "by X" is ambiguous, and the fix is in the spec

`"Give me the cases ... by alert priority"` wants a **list ordered by** a label.
`"How many cases by region"` wants a **breakdown grouped by** one. The phrase is
identical; the disambiguator is whether an aggregate verb is present
(`how many`, `count`, `breakdown`, `distribution`, `per`).

The first version got this wrong in a way worth recording, because the cause was
in the schema rather than the parser. `order_by` accepted numeric measures only,
so *"order by alert priority"* had **no valid spec** — and the extractor did what
anything under-specified does: it reinterpreted the request as the nearest thing
it could express, and returned a three-row breakdown to someone who asked for
cases.

The lesson is that an unexpressible request must be able to *clarify*, never
mutate. So ordered categoricals are now sortable — `ALERT_PRIORITY`,
`DEST_FATF_STATUS`, `DEST_RISK_TIER`, `band` carry an explicit `ordinal`:

```python
ALERT_PRIORITY    CRITICAL > HIGH > MEDIUM
DEST_FATF_STATUS  BLACK_LIST > NON_COMPLIANT > GREY_LIST > MEMBER   # = FATF_WEIGHT order
band              AGED (>1yr) > CURRENT (31-365d) > NEW (<=30d)
```

FATF order follows `ExposureRanker.FATF_WEIGHT` (1.00 / 0.80 / 0.60 / 0.15), not
alphabetical and not source-table order.

**Ties inside a level break by exposure.** Three priority levels over ~1,554
alerts means 492 alerts share the label `CRITICAL`; sorting on the label alone
would return an arbitrary 20 of them — stable across runs, entirely meaningless,
and wrong in a way nobody would catch.

## Two denominators, both reported

`pct_of_backlog` is the share of all 1,554 unresolved alerts. `pct_of_matched`
is the share of the filtered set. Under a filter these differ sharply — EMEA
CRITICALs are 12.5% of the backlog but 30.9% of EMEA — and a reader looking at a
breakdown headed "EMEA" will assume the second. Both are emitted whenever
filters are present rather than silently picking one.

## Two ambiguities resolved in code, not in a prompt

**"priority" → `exposure`, not `ALERT_PRIORITY`.** The label is one of five
factors at weight 0.20. Ranking by it alone displaces ~84% of the queue relative
to exposure (`ExposureRanker.displacement`). Every response reports the reading
it took, so the user can override with an explicit `order_by`.

**"region" → `CLIENT_REGION_CODE`** (client HQ) by default — the geography
supervision attaches to, and what the optional supervisory factor keys on.
`DEST_REGION_CODE` (counterparty geography) is a separate addressable field.
`ExposureRanker`'s backlog carries both under names that don't distinguish them
(`REGION_CODE` is the client's, `COUNTRY_NAME`/`FATF_STATUS` are the
destination's); `StatsEngine._enrich` renames them so the confusion can't
propagate.

## Rank first, then filter

Two of the five factors are population-relative — `age` is a percentile rank,
`value` a percentile of log amount. Scoring a filtered subset would rescale them
against that subset: the top EMEA alert would score 1.00 on age simply for being
the oldest EMEA alert, and `exposure` would mean something different in two
answers in the same conversation.

So `StatsEngine` ranks the **whole** unresolved backlog once, caches it, and
filters the ranked frame. `exposure` always reads "relative to the entire
backlog", and `exposure_rank` stays global — a filtered result showing rank 4 is
telling you it's 4th overall, not 1st in EMEA.

## Scope

`ExposureRanker.backlog()` is **unresolved alerts only** (`RESOLUTION_CODE is
null`) — 1,554 of 5,000. Every response repeats this in `provenance.scope`;
"628 alerts" must never be read as 628 alerts ever raised.

## Capacity allocation

`allocation: "banded"` reserves 10/20/70% of the queue across
NEW / CURRENT / AGED instead of taking a flat top-N. A flat ranking starves new
critical alerts — 1,011 aged alerts compete for the same slots, so any weight on
age fills the queue with them. The deterministic extractor selects `banded`
when the question is about what to *work on* ("what should my team do today",
"the queue"), and says so in `assumptions`.

## Endpoints

Mounted into `pipeline/rag/service.py`, so one base URL and one process.

```
GET  /stats/health      backlog size, as-of date
GET  /stats/fields      the full query surface; UI filter controls build from this
GET  /stats/summary     standing breakdown — priority, type, region, FATF, band, ageing
POST /stats/query       QuerySpec in, rows out. No LLM involved at any point.
POST /stats/refresh     re-rank after new alerts land
POST /chat              natural language in; returns answer + spec + assumptions + provenance
```

`/chat` always returns the `spec` that produced the answer, so `/stats/query`
can reproduce any figure exactly. The prose is the least trustworthy part of the
payload and the easiest to check against the other three.

```bash
uvicorn pipeline.rag.service:app --reload --port 8010
# docs: http://127.0.0.1:8010/docs
```

Stage 5a reads the parquet cache, not HANA, so it stays up when the tenant is
unreachable — `/health` reports `graph_loaded: false` and the backlog analytics
keep serving.

## Examples

```bash
curl -s localhost:8010/chat -H 'content-type: application/json' -d '{
  "message": "Give me the cases with the highest priority in EMEA"}'
```

```json
{"route": "analytical",
 "spec_english": "top 20 of CLIENT_REGION_CODE = EMEA, ordered by exposure descending",
 "n_matched": 628,
 "assumptions": ["Read as the composite exposure score, not the ALERT_PRIORITY label."],
 "provenance": {"exposure_weights": {"age": 0.30, "jurisdiction": 0.25, "priority": 0.20,
                                     "value": 0.15, "unassigned": 0.10},
                "weights_basis": "policy-set, not fitted against outcomes",
                "ranked_over": "the full unresolved backlog, before any filter"}}
```

Deterministic equivalent, no model anywhere:

```bash
curl -s localhost:8010/stats/query -H 'content-type: application/json' -d '{
  "intent": "aggregate",
  "filters": [{"field": "CLIENT_REGION_CODE", "op": "eq", "value": "EMEA"}],
  "group_by": ["DEST_FATF_STATUS"],
  "metrics": ["count", "pct_of_backlog", "avg_exposure"]}'
```

## Fallbacks

`nl.extract_spec` prefers the AI Core LLM and falls back to `rule_spec`, a
deterministic regex/registry extractor that needs no network call and no tenant
credentials. It covers the demo path outright and reports which route it took in
`extraction_method` (`llm`, `rules`, `llm->rules`). Narration falls back the
same way to a composed sentence built from the computed rows.

The third outcome is **clarify**. "Highest priority cases" is answerable with a
stated assumption; "show me the bad ones" is not, and the LLM path returns a
question rather than a guess.

## Tests

```bash
python -m pipeline.stats.selftest      # 59 checks, no network, real backlog
```

Covers executor correctness (filters compose, aggregates sum back to the
population, exposure is not rescaled by filtering, banded allocation reaches all
three ageing bands), ordinal ordering (severity-monotonic, exposure tiebreak,
reversible, groups report in severity order), denominators, contract enforcement
(unknown fields, invented operators, SQL injection and out-of-range limits must
all be rejected), and question → expected-spec pairs.

That last set is the point of the spec design: exact-match grading on a small
JSON object is a tractable eval, whereas grading prose is not. Extending
coverage means adding cases to `selftest.cases`, not writing a rubric.

## Known limits

- `ASSIGNED_TO` and `ALERT_SUBTYPE` are open-domain, so the deterministic
  extractor cannot recognise a specific analyst by name — the LLM path or an
  explicit `QuerySpec` handles those.
- Multi-turn context ("now just the critical ones") isn't implemented. The spec
  is a natural carrier for it: hold the previous `QuerySpec` and let the model
  emit a patch rather than a fresh spec.
- The engine caches one ranked snapshot per process. `POST /stats/refresh` is
  explicit rather than time-based, so a long conversation cannot have its
  numbers shift underneath it mid-thread.
