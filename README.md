# TrustSphere — AML Alert Explainability & Prioritisation

**Document status:** current prototype documentation, generated from the codebase in this repository.
**Application:** TrustSphere, built for TrustSphere Bank (Team 07, SCALE 2026).
**Narrative perspective:** Group Chief Risk Officer, TrustSphere Bank.
**Purpose:** set out the financial-crime problem, the capability we built, the architecture and SAP integrations behind it, the governance position that keeps it defensible, and the gaps that remain before production.

**Evidence convention** — every claim below is tagged:
- **Case fact** — a value supplied in the SCALE 2026 case brief.
- **Implemented** — exists in this codebase today; verifiable by reading the file cited next to it.
- **Target / illustrative** — a goal or estimate that has not been measured and is not a guaranteed outcome.

Nothing in this document claims a capability that isn't in the repository. Where the prototype is a stack substitution or a scoped-down version of the original ask, that is flagged explicitly rather than glossed over — that distinction is itself part of the deliverable, not an afterthought.

> **Board mandate**, February 2026 minute: *"Management is directed to present a transformation roadmap that delivers demonstrable improvement in financial crime risk management effectiveness and efficiency within 12 to 18 months, within the approved envelope, without compromising our regulatory standing in any jurisdiction. The Board explicitly does not require that every problem be solved in this horizon; it requires that the right problems be solved first." (Case fact)*

---

## 1. Executive summary

TrustSphere Bank processes a high volume of cross-border transactions against a financial-crime control stack that still leans on static rules, fragmented regional data, and manual review. Alerts arrive faster than investigators can build context for them, the queue isn't prioritised by regulatory consequence, and every model that influences a customer outcome sits behind a 4–6 month validation queue with three FTE of Model Risk Management capacity to clear it *(Case fact)*.

TrustSphere is the decision-support layer we built to sit on top of that reality, not replace it:

- **Unifies context.** Sixteen tables — transactions, companies, beneficial owners, risk scores, alerts, cases — pulled from the live TEAM_07 schema on SAP HANA Cloud into one provenance-carrying evidence pack per alert. *(Implemented — `pipeline/ingestion/hana_source.py`, `src/evidence_pack.py`)*
- **Explains, doesn't guess.** 31 rules decompose each alert's existing risk score into plain-English drivers. Every severity is policy-set, never fitted against outcomes — deliberately, so this stays on the light governance path instead of becoming a model that needs sign-off before it can influence a case. *(Implemented — `src/risk_drivers.py`)*
- **Prioritises by consequence, not FIFO.** The unresolved backlog is ranked by regulatory exposure using five policy-weighted, stress-tested factors — an explicit, honest alternative to a predictive model, because on this data no recorded feature predicts case outcome above a shuffled baseline (CV AUC 0.4928 vs. 0.4824 shuffled). *(Implemented — `src/exposure_ranking.py`)*
- **Grounds explanations in regulation and ownership structure.** A GraphRAG layer built directly on SAP HANA Cloud — native vector search plus graph traversal over 23,180 nodes and 60,132 edges spanning FATF/OFAC/EU AML/Wolfsberg/MAS text and the operational ownership graph — answers "why does this matter" with citations. *(Implemented — `pipeline/rag/`)*
- **Answers analyst questions in plain English, safely.** A natural-language chat layer over the backlog where the LLM's only job is to fill a closed, schema-validated query — it never writes SQL or computes a number itself. A deterministic fallback covers the same ground with no network call. *(Implemented — `pipeline/stats/`)*
- **Keeps a human as the decision-maker.** Nothing in this codebase files a SAR, blocks a payment, or closes a case. The pipeline produces evidence, ranking, and narrative for a person to act on.

This document is deliberately precise about what runs today versus what a production rollout would still require — that precision is what makes the governance story credible.

---

## 2. Business context

### 2.1 Our institution

| Case fact | Value |
|---|---|
| FY2025 high-value cross-border transactions | 118,400 |
| FY2023 high-value cross-border transactions | 100,200 |
| FY2025 group revenue | USD 1.9 billion |
| Employees | ~6,100 |
| Financial-crime operations analysts | 210 |
| Corporate and institutional relationships | ~2,400 |
| Operating footprint | NA (New York, Toronto), Europe (Frankfurt HQ, London, Amsterdam), Asia (Singapore regional hub, Ho Chi Minh City, Jakarta, Seoul) |
| Compliance Operations Centre | Singapore |
| Annual alert volume | 12,000 |
| **False positive rate (AML alerts)** | **90–95%** |
| Remediation programme deadline / committed spend | Q3 2027 / USD 3.2M |

The seed dataset this prototype was built and tested against — 5,000 alerts, 1,554 currently unresolved, drawn from the same TEAM_07 schema — is a representative slice of that annual volume, not a separate or synthetic population. *(Case fact + Implemented, reconciled — see §9.4)*

### 2.2 The problem we must solve

- **Static detection doesn't match evolving typologies.** Fixed thresholds are weak against layered ownership, rapid fund movement, and combinations of weaker signals that are only meaningful together.
- **The false-positive rate is high (90–95%) — but it is the wrong metric to chase.** As the Group CRO framed it directly: *"The real issue is not the false-positive rate. We need to identify which alerts consume investigators and which create the greatest regulatory exposure when they remain unresolved."* We took that instruction literally: TrustSphere does not try to build a better classifier to push 90–95% down. It reprioritises the existing alert population by regulatory consequence instead — a different, achievable objective. *(Case fact — see §2.4)*
- **Alert volume outpaces investigative capacity.** Every one of 12,000 annual alerts needs context assembled from data that lives in different systems before it can be triaged.
- **The queue isn't prioritised by regulatory exposure.** A FIFO or static-severity queue lets aged, sanctions-adjacent, or supervisorily sensitive cases sit behind newer, lower-consequence ones.
- **Manual work is slow and costly.** Escalated reviews take one to three days; high-value payments can be delayed up to three business days.
- **Regulation constrains the fix.** TrustSphere Bank is under heightened regulatory scrutiny in its European home market and one Asian jurisdiction following its 2025 examinations. Anything that influences a customer outcome must be explainable and formally validated; human accountability for SAR filing and payment blocking is non-negotiable, and fully autonomous payment or account blocking is prohibited outright.
- **We can't hire our way out.** A group-wide hiring freeze is in effect; backfilling attrition in Financial Crime Operations requires COO approval.
- **Legacy infrastructure means data arrives imperfect.** Europe's core banking platform is over 15 years old — a viable solution has to tolerate delayed, incomplete, and inconsistently shaped data rather than assume a clean real-time feed everywhere. *(Case facts)*

### 2.3 Quantified baseline

| Pain point | Case baseline | Business consequence |
|---|---|---|
| False positive rate (AML alerts) | 90–95% | The wrong lever to pull directly — see reframe in §2.2 |
| Manual review time per escalated case | 1–3 days | Slow resolution, growing aged-alert backlog |
| Annual alert volume | 12,000 (~1,000/month) | Large, recurring monitoring surface |
| High-value payment approval delay | 3 business days | Customer friction, possible churn |
| Corporate client exits | 14 in FY2025, up from 9 in FY2024 | 55.6% YoY increase |
| Compliance / financial-crime opex growth | +25% over FY2023–FY2025 | Unsustainable cost trajectory |
| Model validation queue | 4–6 months | Hard constraint on how fast AI can influence outcomes |
| Model Risk Management capacity | 3 FTE | Limits how many models can be validated in parallel |

### 2.4 Stakeholder needs

**Elena Marchetti, Group Chief Risk Officer** — *"Explainability is non-negotiable. Every financial crime decision must produce a reason a human investigator can understand, and a human remains accountable for every SAR decision... Model validation is a hard constraint. Our validation team has a four- to six-month backlog, so any proposal involving a model must include that lead time within the 18-month remediation window."* As Group CRO, I require explainable prioritisation, reproducible evidence, preserved human accountability, and a realistic allowance for model validation timelines. TrustSphere must strengthen the control environment, not create a new unvalidated dependency inside it.

**Priya Raghavan, Group Chief Operating Officer** — *"My target is a 30% reduction in cost-per-case within 18 months through smarter automation and better prioritization... We are under a hiring freeze and facing high attrition, so adding headcount is not a sustainable solution."* Needs lower cost per case and a shorter backlog without added headcount, and fewer payment delays feeding client churn.

**Daniel Okafor, Group Chief Technology Officer** — *"The architecture I believe in costs about USD 11.8M and takes 24–30 months... Regional pilots worry me. Temporary solutions often become permanent exceptions... Europe's core banking platform is over 15 years old, so any solution that assumes clean, real-time data everywhere is assuming something that does not exist."* Needs an architecture that tolerates fragmented legacy sources without a rip-and-replace, and that doesn't calcify into a permanent regional exception — see §3.1 for why TrustSphere is scoped as an extension layer rather than a re-platforming.

- **Investigators** need one place to see the alert, transaction, entity, ownership, behavioural baseline, and regulatory context together.
- **Model validators** need bounded outputs, versioned logic, source fingerprints, and a clear line between what's policy-set and what's fitted.
- **Regulators and auditors** need traceable evidence, visible limitations, and no autonomous adverse action.

Two further governance points from the case's regulatory update, both directly relevant to scope: the **German Works Council** must be consulted for tooling that monitors *individual employee performance* (4–6 month process where triggered) — TrustSphere scores transactions and alerts, not analyst activity, so this does not apply to it. And **customer personal data** is subject to regional residency requirements, with SAP BTP already approved for the Singapore region under existing group agreements — both live SAP services this prototype uses (HANA Cloud, AI Core) are already provisioned in that same Singapore/AP11 region (see §6.3).

---

## 3. Solution scope

### 3.1 What we built first

We prioritised **alert explainability, backlog prioritisation, regulatory grounding, and analyst self-service query** because this combination:

- directly targets manual context-assembly effort and aged-alert exposure;
- sits above existing source systems and monitoring rules rather than replacing them;
- stays on the **light governance path** — every scoring and ranking decision is policy-set, not fitted, so it doesn't enter the 4–6 month validation queue;
- works against a batch/cached extract as well as live HANA, so it tolerates the legacy-infrastructure constraint by design; and
- produces the audit trail and evidence a heavier, AI-influenced rollout would need to justify itself later.

This is deliberately **not** the USD 11.8M, 24–30 month platform the CTO described as the "right" architecture. That timeline alone exceeds the Board's 12–18 month window before a single case is triaged differently. TrustSphere is the extension layer that delivers inside that window using the infrastructure that exists today — evidence for whether the larger rebuild is warranted, not a substitute for the decision.

Every non-SAP dependency in this codebase (pandas, numpy, FastAPI, Pydantic, networkx, GSAP) is open-source, per the case's requirement that non-SAP tooling be open-source or free-tier.

### 3.2 Explicitly out of scope (today)

- Autonomous payment or account blocking.
- Autonomous SAR filing.
- Autonomous alert closure or customer exit.
- Any declaration of guilt or criminality.
- Replacement of upstream transaction-monitoring rules or `TRANSACTION_RISK_SCORES` itself — TrustSphere **explains and reprioritises** an existing score; it does not generate one from scratch.
- Application-level authentication, RBAC, or multi-tenant deployment.
- Production deployment to SAP BTP (see §14).

TrustSphere changes which case an investigator opens next and how quickly they understand it. It does not claim to eliminate false positives at the source.

---

## 4. How TrustSphere addresses the pain points

| Pain point | TrustSphere response | Where |
|---|---|---|
| Fragmented data | One provenance-carrying evidence pack per alert, LEFT-JOIN-only so no alert silently disappears from the queue | `src/evidence_pack.py` |
| Unprioritised backlog | Deterministic, policy-weighted exposure score; banded capacity allocation (10/20/70% across new/current/aged) so critical-but-new alerts aren't starved by aged ones | `src/exposure_ranking.py` |
| Static, opaque scoring | 31 policy-set rules with per-rule severity, source fields, and population frequency reported for calibration | `src/risk_drivers.py` |
| Slow investigation | Regulatory + ownership-graph grounding for any risk driver, retrievable by natural language | `pipeline/rag/` |
| Analyst self-service | Plain-English backlog questions answered by a schema-validated query engine, not free-form LLM guessing | `pipeline/stats/` |
| Regulatory scrutiny | Every score, driver, and ranking is reproducible from a `QuerySpec`/rule ID back to a source row; nothing acts autonomously | throughout |
| Legacy/unreliable integrations | Every stage has a local-extract or fallback path with no live HANA/AI Core dependency | see §11 |

---

## 5. What is actually built today

TrustSphere is organised as a sequence of independently runnable stages, sequenced behind one CLI (`main.py`) so nobody has to remember five different invocations.

### Stage 1 — Ingestion
`pipeline/ingestion/hana_source.py` reads 16 tables (`TRANSACTIONS`, `TRANSACTION_BASELINES`, `TRANSACTION_RISK_SCORES`, `COMPANIES`, `COMPANY_BENEFICIAL_OWNERS`, `COMPANY_RISK_PROFILES`, `COMPLIANCE_CASES`, `CASE_ALERTS`, `RISK_ALERTS`, `AUDIT_LOG`, `JOULE_EXPLANATIONS`, `COUNTRIES`, `INDUSTRIES`, `REGIONS`, `SANCTIONS_LISTS`, `SCREENING_RULES`) from the real TEAM_07 schema via `hdbcli` into a local parquet cache. This is real seed data from the case organisers — no synthetic transaction generation is used.

### Stage 2 — Evidence pack
`src/evidence_pack.py` assembles one record per alert via pushed-down SQL. Design choices that matter for auditability:
- **Every join is a LEFT JOIN** — a missing dimension row must never silently drop an alert from the queue.
- **Client baselines are aggregated across periods, not read from the latest month** — baselines are monthly and 37% of company-months have zero transactions, so the latest month alone is usually degenerate.
- **Prior-alert history only counts alerts raised strictly earlier** than the current one, so nothing downstream can see the future.
- `build_provenance()` records the source table and key for every block.

### Stage 3 — Risk drivers
`src/risk_drivers.py` turns the evidence pack into 31 rules across 8 categories (Screening, Due diligence, Jurisdiction, Behaviour, Ownership, History, Sector, Model trigger). **Every severity is policy-set, never fitted against `RESOLUTION_CODE`** — the code comments are explicit that tuning these against outcomes would turn this into a model and forfeit the light governance path. A regression recovers implicit weights on the existing `OVERALL_RISK_SCORE`'s components (amount, frequency, geography, counterparty, pattern, velocity — R² = 0.863 on the ~88% of transactions it explains; the remainder is flagged as unattributed rather than silently mis-attributed). Each driver reports `population_frequency` — how often that rule fires across the whole population — purely so an investigator can calibrate; it is never used to reorder results.

**Narrative text is generated by template, not by an LLM.** The structured drivers stay authoritative; a model paraphrasing them freely would drift with nothing to check against. `verify_narrative()` checks that no number in the generated prose is absent from the source record.

### Backlog exposure ranking
`src/exposure_ranking.py` ranks the **unresolved** backlog (`RESOLUTION_CODE is null` — 1,554 of 5,000 alerts) by regulatory exposure, not predicted outcome:

```
exposure = 0.30·age_percentile + 0.25·jurisdiction(FATF) + 0.20·priority
         + 0.15·value_percentile + 0.10·unassigned
```

Weights are policy-set and stress-tested, not fitted. The honest reason: alert outcomes are independent of every recorded feature on this data (5-fold CV AUC 0.4928 against a 0.4824 shuffled-label floor) — so this ranks *consequence of leaving an alert unresolved*, a different and defensible objective from predicting resolution. An optional supervisory factor (EMEA/APAC) lifts top-decile coverage of supervised regions from 71% to 96% at a cost of 25% queue displacement — reported, not hidden. Capacity is allocated in bands (10% new / 20% current / 70% aged) rather than a flat top-N, so a large aged cohort can't crowd out newly-arrived critical alerts.

### Stage 4 — GraphRAG grounding
`pipeline/rag/` — hybrid vector + knowledge-graph retrieval on SAP HANA Cloud, in three phases:

1. **Ingestion.** FATF grey/black lists, OFAC sanctions programmes, EU AML package, Wolfsberg CBDDQ guidance, and MAS Notice 626 are chunked (493 chunks), embedded (`text-embedding-3-small` via AI Core, stored as native HANA `REAL_VECTOR`), and LLM-extracted (`gpt-4o-mini` via AI Core orchestration) into `RegulatoryClause`/`Jurisdiction`/`SanctionsProgram` nodes. A separate **deterministic** ETL (no LLM) adds `Client`/`BeneficialOwner`/`Alert` nodes from the same cached operational tables — without it, "shared beneficial owner" traversal would have nothing to walk. **Result: 23,180 nodes, 60,132 edges.**
2. **Retrieval.** `COSINE_SIMILARITY` vector search runs natively in HANA alongside a degree-capped `networkx` BFS (max 8 highest-relevance neighbours per hop) so high-degree jurisdiction hubs don't drown out specific entities.
3. **Wire-up.** A FastAPI service (not literal SAP CAP/Node.js — a flagged stack substitution matching this project's existing Python stack) exposes `POST /ground-risk-driver`.

**Known, documented limitations:** the RDF triple store isn't enabled on this HANA tenant and native `GRAPH_TABLE`/`MATCH` syntax didn't parse, so the graph is stored as plain HANA column tables plus a registered `GRAPH WORKSPACE`, traversed in Python — still one HANA instance, just not an in-SQL traversal. `SIMILAR_TO` (prior-alert linkage) is categorical (same alert type/subtype), not semantic, because the seed data's alert free-text fields are empty. Dedup is exact-normalized-label plus a small alias table, not fuzzy entity resolution. All flagged in `pipeline/rag/README.md`, not discovered later.

### Stage 5a — Backlog analytics & natural-language chat
`pipeline/stats/` answers "how many / which cases / breakdown by" questions over the ranked backlog, mounted into the same FastAPI service. **The governing constraint: the language model never computes a number and never writes SQL.** It has exactly one job — fill a closed, Pydantic-validated `QuerySpec` (whitelisted fields, operators, metrics). A hallucinated column, invented operator, or injected SQL fragment fails validation (422) before it can reach the executor. A deterministic regex/registry extractor (`nl.rule_spec`) covers the same ground with no network call and no tenant credentials, and is the automatic fallback when AI Core is unreachable. `/chat` routes explanatory questions ("why did this fire") to Stage 4 instead of guessing at them with vector search over regulatory text.

Design details worth knowing: the ranked frame is computed **once over the whole backlog, then filtered** — two of the five exposure factors are population-relative percentiles, so ranking a pre-filtered subset would silently rescale them and make "exposure" mean something different across two answers in the same conversation. Both `pct_of_backlog` and `pct_of_matched` are reported under any filter, because they diverge sharply and a reader will assume the wrong one. Covered by **59 self-test checks** (`pipeline/stats/selftest.py`) with no network dependency.

### Demo frontend
`demo/` — static HTML/CSS/vanilla JS with GSAP/ScrollTrigger animation, no build step or framework:
- **Overview** (`index.html`) — the pipeline walkthrough and a mocked scoring/rationale hero panel (explicitly labelled as mocking the `/score` response in the footer).
- **Query console** (`query.html`) — a live chat UI. Backlog questions ("how many", "highest priority", "by region") call the real Stage 5a service at `localhost:8010` when reachable (`python main.py serve`). Per-case explanation replies are mocked against the same three demo cases as the Overview dashboard — flagged in the page itself rather than presented as live.

---

## 6. Technical architecture

### 6.1 Stack

| Layer | Technology |
|---|---|
| Data platform | SAP HANA Cloud (TEAM_07 schema), via `hdbcli` |
| AI platform | SAP AI Core — orchestration deployment (`gpt-4o-mini`) for regulatory extraction and NL query parsing/narration; embeddings deployment (`text-embedding-3-small`, Azure OpenAI executor) for regulatory chunk embeddings |
| Retrieval | Native HANA `REAL_VECTOR` + `COSINE_SIMILARITY`; `networkx` in-process graph traversal over HANA-backed node/edge tables + a registered `GRAPH WORKSPACE` |
| Backend service | Python, FastAPI, Pydantic, Uvicorn (`pipeline/rag/service.py`) |
| Pipeline / CLI | Python, pandas, numpy, orchestrated by `main.py` over independently runnable stage scripts |
| Frontend (demo) | Static HTML/CSS/vanilla JS, GSAP + ScrollTrigger — no framework, no build step |
| Local cache | Parquet (`data/cache/`), mirrored to CSV (`data/TEAM_07/`) for HANA-free `--local` runs |
| Tests | `pipeline/stats/selftest.py` (59 checks), `evidence_pack.py --self-test` |

Both AI Core deployments route through SAP's resource-group, credential, and deployment lifecycle, but the underlying model execution is Azure OpenAI — AI Core brokers access rather than hosting the model. Flagged rather than glossed over: the only fully SAP-native model in this tenant's catalogue is `sap-rpt-1.5`, a tabular model not usable for text embedding or generation.

### 6.2 Request flow

```mermaid
sequenceDiagram
    actor Analyst
    participant Demo as Demo site (static)
    participant API as FastAPI (service.py)
    participant Stats as Stage 5a (StatsEngine)
    participant Rank as ExposureRanker
    participant RAG as Stage 4 (retrieve.py)
    participant HANA as SAP HANA Cloud
    participant AIC as SAP AI Core

    Analyst->>Demo: Ask a backlog question
    Demo->>API: POST /chat
    API->>Stats: classify_intent(message)
    Stats->>AIC: extract_spec (LLM, optional)
    AIC-->>Stats: candidate QuerySpec
    Note over Stats: Pydantic validates the spec;<br/>invalid field/operator/SQL -> 422 or fallback
    alt AI Core unreachable or invalid
        Stats->>Stats: rule_spec (deterministic fallback)
    end
    Stats->>Rank: rank() the whole unresolved backlog
    Rank-->>Stats: ranked frame (cached)
    Stats->>Stats: filter, aggregate, narrate
    Stats-->>API: rows + spec + provenance
    API-->>Demo: answer + spec_english + assumptions

    Analyst->>Demo: Ask why an alert fired
    Demo->>API: POST /ground-risk-driver
    API->>RAG: retrieve(risk_driver, top_k, hops)
    RAG->>HANA: COSINE_SIMILARITY vector search
    RAG->>RAG: degree-capped graph traversal (networkx)
    RAG-->>API: vector_matches + graph_context
    API-->>Demo: citations + connected entities
```

### 6.3 Deployment context

The repository currently supports local development plus connection to the **real, live** SAP HANA Cloud and SAP AI Core tenant provisioned for this case — it does not yet contain a deployed SAP BTP runtime. Both AI Core deployments (orchestration `d168cee75aadc761`, embeddings `d7f1244c7397cfe0`, resource group `team-07`) and the HANA Cloud instance are already provisioned in the **Singapore / AP11 region**, which satisfies the case's Singapore-residency framing even though a full BTP deployment (identity, networking, secrets, regional runtime) is still outstanding — see §14.

---

## 7. API & CLI catalogue

### HTTP (via `uvicorn pipeline.rag.service:app --port 8010`, docs at `/docs`)

| Method | Route | Purpose |
|---|---|---|
| GET | `/health` | Graph-load status and node count |
| POST | `/ground-risk-driver` | Stage 4 — vector matches + graph context for a risk driver |
| POST | `/admin/refresh-graph` | Reload the in-memory graph after re-ingestion |
| GET | `/stats/health` | Backlog size, as-of date |
| GET | `/stats/fields` | Full query surface — the same catalogue the LLM prompt is generated from |
| GET | `/stats/summary` | Standing backlog breakdown (priority, type, region, FATF, band, ageing) |
| GET | `/stats/alert/{id}` | Direct case lookup in the unresolved backlog, no LLM |
| POST | `/stats/query` | Deterministic `QuerySpec` in, rows out — no LLM anywhere |
| POST | `/stats/refresh` | Re-rank after new alerts land |
| POST | `/chat` | Natural language in; returns answer, spec, assumptions, and provenance |

### CLI (`python main.py <stage>`)

| Stage | Purpose |
|---|---|
| `check` | Environment sanity check — data, credentials, dependencies |
| `ingest` | Stage 1 — pull TEAM_07 tables from HANA |
| `sync-local` | Materialise `data/TEAM_07/*.csv` from the parquet cache, no HANA call |
| `evidence` | Stage 2 — assemble / self-test the evidence pack |
| `drivers` | Stage 3 — derive risk drivers + narratives, optionally persist to HANA |
| `rank` | Score the unresolved backlog by regulatory exposure |
| `graph-ingest` | Stage 4 phase 1 — build the GraphRAG layer |
| `serve` | Stage 4/5a — run the FastAPI service |
| `explain` | Everything about one alert, stages 2+3, printed for a demo |
| `all` | The local, HANA-write-free happy path: evidence self-test → drivers → ranking |

---

## 8. SAP services & integration

| Service | Status |
|---|---|
| **SAP HANA Cloud** | **Implemented.** Live tenant, TEAM_07 reference schema, plus `RAG_CHUNKS`/`RAG_NODES`/`RAG_EDGES` and a registered `GRAPH WORKSPACE` built during this project. |
| **SAP AI Core** | **Implemented.** OAuth client-credentials auth, automatic discovery of the running orchestration deployment, `gpt-4o-mini` completion, structured JSON output at temperature 0, plus an embeddings deployment created during this build (none previously existed in this resource group). |
| **SAP BTP** | **Targeted, not deployed.** Approved as the Singapore-region host; both live SAP services already sit in that region, but this repository contains local/Docker-free dev assets, not a deployed BTP runtime. |
| **SAP Joule / Joule Studio** | **Not integrated.** No Joule skill or `.mtar` in this codebase — narrative generation is a Python template, and NL query parsing/narration runs through SAP AI Core directly. |
| **SAP Datasphere** | **Not used.** Ingestion reads a single schema (TEAM_07) via `hdbcli`; there is no cross-regional federation layer. |
| **SAP Analytics Cloud** | **Not used.** The demo's charts are hand-built SVG/GSAP, not SAC. |
| **SAP CAP (Node.js)** | **Substituted.** The grounding/analytics service is FastAPI, matching this project's existing Python stack — flagged as a deliberate stack substitution, not a silent swap. |

We keep this distinction explicit so the SAP footprint isn't overstated in review.

---

## 9. Data architecture

### 9.1 Financial risk framework — factors, weights, rationale

TrustSphere runs two policy-set frameworks side by side, deliberately kept separate because they answer different questions.

**Why an alert matters (Stage 3 — 31 rules, 8 categories: Screening, Due diligence, Jurisdiction, Behaviour, Ownership, History, Sector, Model trigger).** Each rule carries a severity (0–100) set by policy — e.g. a sanctions-list hit (`SCR001`) is severity 95, an unattributed model-score spike (`MDL010`) is severity 60. These are reference points for narrative weight, not summed into a score. *(`src/risk_drivers.py`)*

**Which unresolved alert to work next (backlog exposure ranking) — five factors, weighted:**

| Factor | Weight | Rationale |
|---|---:|---|
| Age (percentile) | 0.30 | The case's own remediation programme names aged-alert backlogs explicitly as a supervisory concern |
| Jurisdiction (FATF status) | 0.25 | Black list 1.00 → non-compliant 0.80 → grey list 0.60 → member 0.15 |
| Alert priority | 0.20 | Critical 1.00 → High 0.65 → Medium 0.30 |
| Value (percentile of log amount) | 0.15 | Larger exposure carries larger regulatory consequence if left unresolved |
| Unassigned | 0.10 | An alert nobody owns is a process risk independent of its content |
| *(optional)* Supervisory region (EMEA/APAC) | +0.15, rescaling the rest by ×0.85 | Lifts supervised-region coverage in the top decile from 71% to 96%, at a reported cost of 25% queue displacement |

**Why weights, not a fitted model:** we tested whether any recorded feature predicts case resolution outcome — it doesn't, above a shuffled-label floor (5-fold CV AUC 0.4928 vs. 0.4824 shuffled). A model built on this data would be fitting noise while claiming predictive power, which is precisely the failure mode the CRO's validation requirement exists to catch. Ranking by *regulatory consequence of non-resolution* is the framework that is both honest about what the data supports and fast to deploy — it never leaves the light governance path described in §10. *(`src/exposure_ranking.py`)*

### 9.2 Ontology (Stage 4)

- **Regulatory-text extraction** produces only `RegulatoryClause` / `Jurisdiction` / `SanctionsProgram` nodes and `CITES_CLAUSE` / `SUBJECT_TO` / `LISTED_UNDER` edges — `Alert`/`Client`/`BeneficialOwner` simply don't occur in regulatory text.
- **Deterministic operational ETL** adds `Client`, `BeneficialOwner`, `Alert` nodes and `OWNS` (BeneficialOwner→Client) and `SHARES_OWNER_WITH` (derived Client↔Client shortcut) edges, plus a second use of `SUBJECT_TO` for BeneficialOwner→Jurisdiction by residence. Both additions beyond the original ontology are flagged in `pipeline/rag/README.md`, not silent.
- `SANCTIONS_LISTS.COUNTRY_ID` is a designated individual's **nationality**, not "this country is under this programme" — an early version treated it as the latter and produced a near-complete noise graph. Removed; only regulatory-text-derived `LISTED_UNDER` edges remain.

### 9.3 Backlog ranking design

`ExposureRanker.backlog()` covers **unresolved alerts only** — every API response repeats this scope so "1,554 alerts" is never misread as "1,554 alerts ever raised." Two of five factors (age, value) are population-relative percentiles, which is why ranking happens once over the whole backlog before any filter is applied — filtering first would rescale those percentiles and make the same word ("exposure") mean different things across two questions in one conversation.

### 9.4 Reconciling seed data with the case's annual figures

The case states 12,000 alerts/year; the seed dataset used to build and test this prototype contains 5,000 `RISK_ALERTS` rows (1,554 unresolved at the time of writing). This is expected — case organisers seeded a representative operating slice, not a full year of production volume — but it's worth stating explicitly rather than letting the two numbers sit unreconciled.

---

## 10. Governance & model risk

This is the section that determines whether TrustSphere can ship at month 3 instead of waiting behind the model-validation queue, so precision here matters more than anywhere else in this document.

- **Every rule severity in Stage 3 is policy-set, not fitted.** Nothing is estimated from `RESOLUTION_CODE`. The moment any of these numbers gets tuned against outcomes, it becomes a model influencing a customer decision and re-enters the 4–6 month validation queue — the code comments say this explicitly, as a constraint on future changes, not just current ones.
- **Exposure-ranking weights are policy, not a fitted model.** We checked: no recorded feature predicts case resolution above a shuffled-label floor on this data (CV AUC 0.4928 vs. 0.4824). Ranking by *regulatory consequence of non-resolution* is therefore the honest objective, not a fallback dressed up as a prediction.
- **Narrative text is templated, never LLM-paraphrased.** `verify_narrative()` checks every generated sentence against the source record and flags any number that isn't traceable back to a field.
- **The one place an LLM's output is trusted to act, its authority is capped by schema, not by prompt.** Stage 5a's natural-language chat lets the model choose a closed `QuerySpec` — nothing else. A hallucinated field, an invented operator, or an injected SQL fragment is rejected by Pydantic validation before it reaches the query executor. This is enforced in code, verified by 59 automated checks, not asserted in a prompt.
- **Population frequency is reported, never used to reorder.** A driver that fires on 80% of alerts is true but not distinguishing — showing that honestly, without letting it silently deprioritise the driver, is a deliberate choice documented in the code.
- **Human accountability is structural, not a UI convention.** There is no code path in this repository that files a SAR, blocks a payment, or closes a case. Every stage stops at producing evidence, a score, a ranking, or a narrative for a person to act on.

---

## 11. Resilience & degraded operation

| Failure | Current behaviour |
|---|---|
| HANA unreachable at service startup | Stage 4's graph fails to load; `/health` reports `graph_loaded: false`; the process stays up |
| HANA unreachable for Stage 5a | Doesn't matter — Stage 5a reads the parquet cache, not HANA, so backlog analytics keep serving |
| AI Core unreachable / times out / invalid JSON | NL extraction and narration fall back to deterministic regex/registry logic (`rule_spec`, `narrate_fallback`) — no broken workflow |
| Model proposes an out-of-range value | Pydantic/bound validation rejects it; deterministic path is used |
| Hallucinated field, invented operator, injected SQL | Rejected with a 422 before reaching the executor |
| No live HANA credentials at all | `main.py all` runs the entire evidence → drivers → ranking path against the local CSV/parquet extract with zero live dependencies |

---

## 12. Testing & verification

- **59 automated checks** in `pipeline/stats/selftest.py` — executor correctness (filters compose, aggregates sum back to the population, exposure isn't rescaled by filtering), ordinal ordering, denominators, and contract enforcement (unknown fields, invented operators, SQL injection, out-of-range limits all rejected).
- **`evidence_pack.py --self-test`** verifies join logic against the CSV extract.
- No frontend automated test suite — `demo/` is a static, framework-free site; verification there has been manual, in-browser.
- Not yet done, and required before production: integration, load, failover, penetration, accessibility, model-validation, and adversarial-prompt testing.

---

## 13. Prototype scale (concrete, not projected)

| Metric | Value |
|---|---|
| Source tables ingested | 16 |
| Alerts in seed data / unresolved | 5,000 / 1,554 |
| Risk-driver rules | 31, across 8 categories |
| Regulatory documents / chunks | 6 / 493 |
| Graph nodes / edges | 23,180 / 60,132 |
| Automated backlog-analytics checks | 59 |
| Regulatory-extraction LLM failures on final run | 0 |

These numbers describe what was actually built and can be re-derived by running the pipeline — deliberately in place of a projected ROI table we have no basis to stand behind yet. Cost-per-case and cycle-time improvements are real goals (see §2.3's 30% target) but are **targets for a controlled pilot**, not measured results of this prototype.

---

## 14. Production-readiness gaps

| Gap | Risk | Required treatment |
|---|---|---|
| No application authentication / RBAC | Unauthenticated access to the API and CLI | SAP Identity Authentication/XSUAA or bank IAM |
| Not deployed to SAP BTP | Prototype runs locally only | Complete BTP deployment, networking, and secret management |
| Per-request HANA connections, no pooling | Won't scale past a demo | Connection pool before any real load |
| Single in-process graph cache | Not safe for multi-instance deployment | Shared cache (Redis) or push traversal server-side |
| RDF triple store / native `GRAPH_TABLE` unavailable on this tenant | Traversal runs in Python, not in-SQL | Enable the triple store via BTP cockpit, or confirm `GRAPH_TABLE` grammar for this HANA QRC |
| `SIMILAR_TO` is categorical, not semantic | Weaker prior-alert linkage than embeddings would give | Move to embedding similarity once alert free-text fields are populated |
| Dedup is exact-normalized-label + alias table | Misses near-duplicate entity mentions at scale | Embedding-based or LLM-assisted entity resolution |
| Local credential file (`team_07_credentials.json`) | Secret-leakage risk if mishandled — **already gitignored** | BTP secret service, rotation, no filesystem credentials |
| No production persistence for computed drivers outside the HANA write path | Session-scoped state can be lost | Durable, transactional persistence in approved infrastructure |

---

## 15. Roadmap

**Phase 0 — Governance & baseline.** Confirm the Singapore-region architecture end to end, define pilot population and SLA/KPI formulas, baseline current review time and cost per case.

**Phase 1 — Deterministic pilot.** Deploy to BTP with authentication and durable audit storage; run Stages 1–4 and the backlog ranking as the primary workflow. AI-assisted scoring stays available for explanation and query, but nothing AI-influenced determines queue order without a human in the loop.

**Phase 2 — Validated AI-assisted investigation.** Complete formal Model Risk Management validation for any path where AI Core output could influence prioritisation; run in shadow mode before it affects the live queue.

**Phase 3 — Scale.** Additional regions, durable event-driven ingestion, precomputed graph neighbourhoods instead of live traversal, enterprise IAM integration.

---

## 16. How to run it

```bash
pip install -r requirements.txt

python main.py check                 # environment sanity check
python main.py all --limit 200       # local happy path: evidence -> drivers -> ranking, no HANA writes
python main.py explain 15002         # everything about one alert, printed

python main.py ingest                # stage 1, live HANA
python main.py graph-ingest          # stage 4 phase 1, live HANA + AI Core
python main.py serve --port 8010     # stage 4 + 5a service, docs at /docs

python -m pipeline.stats.selftest    # 59 checks, no network
```

To view the demo site (serves relative assets, needs a static server rather than a raw `file://` open):
```bash
cd demo && python -m http.server 8000
# http://localhost:8000/index.html   -- overview + pipeline walkthrough
# http://localhost:8000/query.html   -- live query console (talks to `serve` above when running)
```

---

## 17. Source-of-truth files

**Orchestration**
`main.py`

**Pipeline (stages 1–3, backlog ranking)**
`pipeline/ingestion/hana_source.py`, `src/evidence_pack.py`, `src/risk_drivers.py`, `src/exposure_ranking.py`, `src/pipeline.py`

**GraphRAG (stage 4)**
`pipeline/rag/ingest_regulatory.py`, `ingest_operational_graph.py`, `chunking.py`, `extract.py`, `dedup.py`, `store.py`, `retrieve.py`, `service.py`, `pipeline/rag/README.md`

**Backlog analytics (stage 5a)**
`pipeline/stats/spec.py`, `engine.py`, `nl.py`, `api.py`, `selftest.py`, `pipeline/stats/README.md`

**Demo frontend**
`demo/index.html`, `demo/query.html`, `demo/js/`, `demo/css/`

**Data**
`data/regulatory_docs/` (source text), `data/cache/` (parquet), `data/TEAM_07/` (CSV extract, gitignored)

---

## 18. One-sentence value proposition

TrustSphere gives investigators an explainable, SAP HANA Cloud- and AI Core-powered priority queue and evidence pack — every score policy-set, every narrative source-verified, every ranking reproducible — so the highest-consequence cases get looked at first, while a human stays the one who decides.
