# Stage 4 — GraphRAG Grounding Layer

Hybrid vector + knowledge-graph retrieval on SAP HANA Cloud for AML alert
explainability (TrustSphere Bank, team_07). Given a risk driver extracted from
an alert, this stage retrieves relevant regulatory text **and** connected
context that plain vector search can't reach — related clauses, prior similar
alerts, entities sharing a beneficial owner across cases.

## What's implemented

### Phase 1 — Ingestion (`ingest_regulatory.py`, `ingest_operational_graph.py`)

**Regulatory documents** (`data/regulatory_docs/*.txt`, fetched from official
public sources — see `raw/build_docs.py` for exact URLs and the cleaning
pipeline): FATF grey list, FATF black list, OFAC sanctions programs (DPRK, Iran,
Russia, Cuba, Venezuela, Syria), EU AML package (AMLD5/6, consolidated
sanctions list), Wolfsberg CBDDQ Guidance, MAS Notice 626.

1. **Chunk** — paragraph-aware packing (`chunking.py`), ~1400 chars/chunk with
   overlap. 493 chunks across the 6 documents.
2. **Embed** — `text-embedding-3-small` (1536-dim) via an AI Core deployment,
   stored as native HANA `REAL_VECTOR`.
3. **Extract** — `gpt-4o-mini` via AI Core orchestration pulls
   RegulatoryClause / Jurisdiction / SanctionsProgram nodes and CITES_CLAUSE /
   SUBJECT_TO / LISTED_UNDER edges from each chunk (`extract.py`). Scoped to
   these 3 node types because Alert/Client/BeneficialOwner/RiskDriver don't
   occur in regulatory text.
4. **Dedup** — same clause/jurisdiction/program mentioned across documents
   collapses to one node via a normalized-label + small alias table
   (`dedup.py`).

**Result:** 493 chunks, 681 nodes (54 Jurisdiction, 520 RegulatoryClause, 107
SanctionsProgram), 639 edges. Zero extraction failures on the final run.

**Operational graph** (`ingest_operational_graph.py`) — an addition beyond the
literal Phase 1 spec, flagged when built: regulatory text alone can only ever
produce Jurisdiction/SanctionsProgram/RegulatoryClause nodes, so without this
step Phase 2's "shared beneficial owners" / "similar prior alerts" traversal
would have nothing to traverse. This is deterministic relational→graph
mapping from the already-cached operational tables
(`pipeline/ingestion/hana_source.py`'s `COMPANIES`, `COMPANY_BENEFICIAL_OWNERS`,
`RISK_ALERTS`, `COUNTRIES`, `SANCTIONS_LISTS`), not LLM extraction.

**Result:** +22,499 nodes (5,000 Client, 12,455 BeneficialOwner, 5,000 Alert,
+12 Jurisdiction, +32 SanctionsProgram), +59,850 edges. **Grand total: 23,180
nodes, 60,132 edges.**

### Phase 2 — Retrieval (`retrieve.py`, `demo_phase2.py`)

`retrieve(conn, risk_driver_text, top_k, hops)`:
1. Embeds the risk driver, runs `COSINE_SIMILARITY` vector search over
   `RAG_CHUNKS` natively in HANA (`store.vector_search`).
2. Finds graph seeds two ways: (a) entities the risk driver text names
   directly (substring match against Client/BeneficialOwner/Jurisdiction/
   SanctionsProgram labels), (b) Jurisdiction/SanctionsProgram nodes sourced
   from the same documents as the top vector hits.
3. Level-by-level BFS (via networkx, undirected for reachability) up to `hops`
   hops from those seeds. Two things keep this from drowning in noise once a
   seed is a high-degree hub (see "Jurisdiction nodes are high-degree hubs"
   below for the full story): expansion from any one node is capped to its 8
   most risk-relevant neighbors, and entities the query names directly always
   outrank generic same-document context. Final result capped at 60 nodes for
   readability.
4. Returns `{vector_matches, graph_context: {seed_nodes, nodes, edges,
   truncated}}`.

**Demo** (`python -m pipeline.rag.demo_phase2`) uses a real example grounded in
the EDA: Erling Koch, a UAE-resident beneficial owner who holds 59.79% of
Biovia PARTNERSHIP (Hong Kong, 2 prior CRITICAL alerts) and 38.14% of Apextech
LLC (Canada) — the two companies share his ownership. The graph traversal
surfaces exactly what vector search alone cannot: `Erling Koch —SUBJECT_TO→
United Arab Emirates`, `Erling Koch —OWNS→` both companies, `Biovia
—SHARES_OWNER_WITH→ Apextech`, and the alerts triggered on Biovia — alongside
the FATF/Wolfsberg/MAS text the vector search retrieves independently.

### Phase 3 — Wire-up (`service.py`)

FastAPI, not literal SAP CAP (Node.js) — matches this project's existing
Python/FastAPI stack; flagged as a stack substitution rather than switched
silently.

```
POST /ground-risk-driver
  { "risk_driver": str, "top_k": int (1-20, default 5), "hops": int (1-3, default 2) }
  → { "risk_driver": str,
      "vector_matches": [{CHUNK_ID, DOC_NAME, SECTION, CHUNK_INDEX, CHUNK_TEXT, SCORE}],
      "graph_context": {"seed_nodes": [...], "nodes": [...], "edges": [...], "truncated": bool} }

GET  /health                 -- graph-loaded status + node count
POST /admin/refresh-graph    -- reload the in-memory graph after re-ingestion
```

Auto-generated interactive docs at `/docs`. This is the interface stage 5
(narration) consumes; stages 1-3 are responsible for producing `risk_driver`.

## What's stubbed / simplified (and why)

- **RDF triple store not used.** Live-probed against this HANA Cloud instance:
  `SYS.SPARQL_EXECUTE` returns `"No active TripleStore found in landscape"` —
  the feature isn't enabled on this tenant. The SQL:2023 `GRAPH_TABLE(...MATCH...)`
  property-graph syntax also failed to parse (workspace *objects* create fine,
  registered in `SYS.GRAPH_WORKSPACES`, but query syntax didn't resolve on this
  HANA build). **Decision** (confirmed with user): store the graph as plain
  HANA column tables (`RAG_NODES`, `RAG_EDGES`) + a registered `GRAPH WORKSPACE`
  for schema fidelity, traverse in Python via networkx. Still one HANA
  instance, no separate graph DB — just not executing the traversal in-SQL.
  **To upgrade:** enable the triple store via BTP cockpit (needs admin access)
  or confirm the exact `GRAPH_TABLE` grammar for this HANA build's QRC.
- **Two ontology additions beyond the 6 named edge types**, both needed for
  the operational graph to be traversable at all:
  - `OWNS` (BeneficialOwner → Client) — without it, BeneficialOwner nodes
    connect to nothing. `SHARES_OWNER_WITH` is the derived Client↔Client
    shortcut edge for the common 1-hop case.
  - `SUBJECT_TO` reused for BeneficialOwner → Jurisdiction (by residence),
    in addition to its original Jurisdiction → RegulatoryClause use.
- **SIMILAR_TO (prior alerts) is categorical, not semantic.** Alerts are
  linked to their most recent 3 same-`(ALERT_TYPE, ALERT_SUBTYPE)` predecessors
  — cheap, deterministic, avoids 5,000 extra AI Core embedding calls for a
  prototype. `RISK_ALERTS.RISK_DRIVERS`/`RECOMMENDED_ACTIONS` are always empty
  placeholder data in this seed set (confirmed by inspection), so there's no
  free-text alert content to embed yet anyway — real driver text is what
  stages 1-3 would produce.
- **No entity linking / NER.** Seeding the graph traversal from risk-driver
  text uses case-insensitive label substring matching. Real entity resolution
  (fuzzy matching, NER) is the standard upgrade path.
- **Dedup is exact-normalized-label + a ~15-entry alias table** (`dedup.py`),
  not fuzzy/embedding-based entity resolution. Caught and fixed live during
  this build: the LLM occasionally emitted the ontology's own type name as an
  entity value (e.g. a node literally labeled `"Jurisdiction"`), which merged
  into a false hub connecting unrelated clauses — now filtered in `extract.py`.
- **Jurisdiction nodes are high-degree hubs — mitigated, not eliminated.**
  A bare jurisdiction query (e.g. "IRAN" with no named company/person) has
  nothing more specific to seed from than the jurisdiction itself, which can
  have hundreds of resident-beneficial-owner edges. `traverse()` in
  `retrieve.py` now handles this two ways: (1) expansion from any single node
  is capped at `DEFAULT_MAX_FANOUT_PER_HOP` (8), keeping only the neighbors
  ranked highest by `_node_relevance_score()` (sanctions match / PEP / high
  KYC risk / critical alert priority) — confirmed live: a bare "IRAN" query
  now surfaces owners with actual sanctions matches instead of an arbitrary
  sample of everyone who lives there; (2) entities the query *names directly*
  (e.g. a specific beneficial owner) always outrank generic same-document
  context in the final result, and that priority only propagates through
  "instance" node types (Client/BeneficialOwner/Alert) — not through
  Jurisdiction/SanctionsProgram, so naming a jurisdiction alongside a person
  (e.g. "Erling Koch ... Hong Kong ... Canada") doesn't let the jurisdiction's
  entire resident population outcompete the person's own second company for
  the display budget. Both edge cases were caught by hand-testing real queries
  against the live service, not anticipated in advance. Remaining known gap:
  this is a heuristic ranking, not a guarantee — a query naming several
  high-degree entities at once can still crowd out lower-`hops` content from
  any single one of them.
- **`SANCTIONS_LISTS.COUNTRY_ID` is not used for jurisdiction-level
  `LISTED_UNDER` edges** — it's the nationality/registration country of an
  *individually*-designated person, not "this country is under this sanctions
  program." Treating it as the latter produced a near-complete noise graph in
  an earlier run; removed. The only `LISTED_UNDER` edges are the real ones
  extracted from regulatory text (FATF grey/black list membership etc.).
- **Per-request HANA connections, no pooling.** Fine for a demo; add a
  connection pool before any real load.

## How to run it

```bash
# from repo root, using the Python env with hdbcli + AI Core deps installed
pip install -r requirements.txt

# Phase 1 -- regulatory ingestion (chunk, embed, extract; ~13 min, ~500 LLM calls)
python -m pipeline.rag.ingest_regulatory

# Phase 1 -- operational graph ETL (~5s, no LLM calls)
python -m pipeline.rag.ingest_operational_graph

# Phase 2 -- demo: vector matches + graph context side by side
python -m pipeline.rag.demo_phase2

# Phase 3 -- service
uvicorn pipeline.rag.service:app --port 8010
# then: POST http://127.0.0.1:8010/ground-risk-driver
#       docs at http://127.0.0.1:8010/docs
```

Re-running `ingest_regulatory` / `ingest_operational_graph` is idempotent —
regulatory extraction upserts by canonical node ID; the operational ETL clears
its own rows (`SOURCE = 'operational_graph'`) before rebuilding.

### AI Core deployments (this tenant, resource group `team-07`)

- **Orchestration** (`d168cee75aadc761`, scenario `orchestration`) — already
  existed, used for LLM extraction (`gpt-4o-mini`) via `POST .../completion`.
- **Embeddings** (`d7f1244c7397cfe0`, scenario `foundation-models`, executable
  `azure-openai`, model `text-embedding-3-small`) — **created during this
  build**, since no embedding deployment previously existed in this resource
  group. Confirmed live via `POST .../embeddings?api-version=2023-05-15`.

Both route through SAP AI Core (SAP's resource group, credentials, deployment
lifecycle), but the underlying model execution is Azure OpenAI — AI Core
brokers access rather than hosting these models itself. If a fully SAP-hosted
path is required later, the only SAP-native model in this tenant's catalog is
`sap-rpt-1.5`, a tabular/relational model, not usable for text embedding or
generation.

## How to test it (step by step)

The pipeline has already been run once (data is live in HANA — 493 chunks,
23,180 graph nodes), so testing means **calling the retrieval endpoint**, not
re-running ingestion.

**1. Make sure the service is running.**
```bash
uvicorn pipeline.rag.service:app --host 127.0.0.1 --port 8010
```
Leave that terminal open — it's the server. In a second terminal (or browser),
confirm it's up:
```bash
curl http://127.0.0.1:8010/health
# {"status":"ok","graph_loaded":true,"graph_nodes":23174}
```
If `graph_loaded` isn't `true`, the server failed to connect to HANA at
startup — check the server terminal's output for the error.

**2. Open the interactive page:** http://127.0.0.1:8010/docs in a browser.
This is auto-generated by FastAPI from `service.py` — no separate UI to build
or run.

**3. Send a test request.**
- Click on **`POST /ground-risk-driver`** to expand it.
- Click the **"Try it out"** button (top right of the expanded section).
- A JSON text box appears, pre-filled with a template. Replace it with e.g.:
  ```json
  {
    "risk_driver": "Beneficial ownership structure flagged: UAE-resident owner Erling Koch holds a 59.79% controlling stake in Biovia PARTNERSHIP (Hong Kong), which has prior CRITICAL alerts, and co-owns 38.14% of Apextech LLC (Canada).",
    "top_k": 5,
    "hops": 2
  }
  ```
- Click **"Execute"**.
- Scroll down to **"Server response"** — the actual JSON comes back there.

**4. Read the response.** It has two parts:
- `vector_matches` — regulatory text chunks, most-similar first (`SCORE` is
  cosine similarity, 0-1). This is "what does the regulation say."
- `graph_context.nodes` / `graph_context.edges` — the connected graph, "what
  else is connected to what I asked about." Each node has an `hop_distance`
  (0 = something the query named directly, 1/2 = reached by walking edges).
  Read the `edges` list for the actual relationships — e.g.
  `"BeneficialOwner:erling koch" -OWNS-> "Client:3756"` means exactly what it
  looks like: that beneficial owner owns that company.

**5. Try your own query.** Swap the `risk_driver` text for anything you want
and hit Execute again — no restart needed. A few concrete things to try, to
see different behavior:
- **A query naming a real person/company** (like the Erling Koch example
  above) → rich, specific `graph_context` centered on that entity. You can
  find other real names to try in `data/cache/companies.parquet` or
  `company_beneficial_owners.parquet`.
- **A bare country name** (e.g. `"IRAN"`) → `vector_matches` will be strong
  (FATF/OFAC text about Iran specifically), and `graph_context` will show
  beneficial owners flagged with `sanctions_match`/`is_pep` — check the
  `properties` field on each `BeneficialOwner` node in the response to confirm.
- **Generic regulatory language with no named entity** (e.g. `"correspondent
  banking due diligence gap for a shell company in a high-risk corridor"`) →
  `vector_matches` still works (it's pure text similarity), but
  `graph_context` will mostly be broad document-level context rather than
  anything centered on a specific entity, since there's nothing specific for
  it to seed from.
- **Change `hops` to `1`** on the same query and compare — fewer, more
  tightly-related nodes.

**If you'd rather skip the browser and just run a script:**
```bash
python -m pipeline.rag.demo_phase2
```
prints the same two-part result (vector matches + graph context) straight to
the terminal for the Erling Koch example, formatted for reading rather than as
raw JSON.

## Scaling beyond 6 documents / current data volume

**Ingestion:**
- Chunking/embedding/extraction is already parallelizable per-document; at
  scale, batch chunks into concurrent AI Core requests (rate-limit aware)
  instead of the current sequential loop — cuts the ~13-minute run
  proportionally.
- LLM extraction cost scales linearly with chunk count. At real corpus size
  (hundreds of documents vs. 6), consider a cheaper first-pass classifier to
  skip chunks unlikely to contain extractable entities (e.g. boilerplate,
  tables of contents) before spending an LLM call on them.
- Entity dedup will need to move from exact-normalized-label matching to
  embedding-based or LLM-assisted entity resolution once the corpus has
  enough near-duplicate phrasing that exact match starts missing real merges.

**Graph storage & traversal:**
- The current tables-plus-Python-traversal approach loads the full node/edge
  set into memory per graph load (23K nodes today). This is fine into the low
  hundreds of thousands; beyond that, traversal needs to move server-side —
  either by getting the RDF triple store enabled on the HANA instance (native
  SPARQL) or confirming the `GRAPH_TABLE`/`MATCH` SQL grammar for the target
  HANA Cloud QRC (native openCypher-style traversal, no data round-trip).
  Both keep the "one HANA instance, no separate graph DB" property; this
  prototype's tables + registered `GRAPH WORKSPACE` schema doesn't need to
  change to adopt either — only the query path does.
- `SIMILAR_TO` should move from categorical (same alert type/subtype) to
  embedding similarity once real risk-driver/alert-description text exists
  (the current `RISK_ALERTS` seed data has this field empty) — the same
  `REAL_VECTOR` + `COSINE_SIMILARITY` infrastructure built for regulatory
  chunks applies directly, just against an alert-embeddings table instead.
- Jurisdiction-hub fan-out is already degree-capped and relevance-ranked (see
  above), but the ranking itself (`_node_relevance_score()`) is a small
  hand-written heuristic over a handful of known fields (sanctions match, PEP,
  KYC rating, alert priority). At real scale this should become a precomputed
  "risk-relevant neighborhood" index per entity, refreshed on ingestion rather
  than ranked live on every request, and the scoring function itself should be
  learned/configurable rather than hardcoded.

**Service:**
- Add HANA connection pooling (currently one connection per request).
- The in-memory graph cache is single-process; a multi-instance deployment
  needs either a shared cache (Redis) or to push traversal into HANA per the
  point above, so cache consistency isn't a per-instance concern.
- Vector search already runs in-database (`COSINE_SIMILARITY` over
  `REAL_VECTOR`), so it scales with HANA itself rather than the service layer
  — no client-side vector index (e.g. FAISS) needed even at larger chunk
  counts.

**Data residency:** HANA Cloud instance and both AI Core deployments used here
are already in the Singapore/AP11 region, satisfying the stated constraint;
this doesn't change with scale.
