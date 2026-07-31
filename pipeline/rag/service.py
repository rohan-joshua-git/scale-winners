"""Phase 3 -- wire-up: expose Phase 2's retrieve() as a callable HTTP endpoint so
stage 5 (narration) can consume this stage's output without importing pipeline
internals directly. FastAPI rather than a literal SAP CAP (Node.js) service --
matches the project's Python stack (fastapi/uvicorn already in requirements.txt);
flagged per the task's "open to swapping stacks if faster, don't switch
silently" instruction.

Run: uvicorn pipeline.rag.service:app --reload --port 8010
Docs: http://127.0.0.1:8010/docs (FastAPI auto-generates this from the schemas below)
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import networkx as nx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from pipeline.ingestion.hana_source import get_connection
from pipeline.rag import store
from pipeline.rag.retrieve import retrieve
from pipeline.stats import api as stats_api

_graph_cache: dict[str, nx.MultiDiGraph] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Stage 4 needs HANA; stage 5a (pipeline.stats) does not - it reads the
    # parquet cache. Failing the whole process because the tenant is unreachable
    # would take the backlog analytics down with it, so the graph load is
    # allowed to fail and /health reports graph_loaded=false.
    try:
        conn = get_connection()
        try:
            _graph_cache["graph"] = store.load_networkx_graph(conn)
        finally:
            conn.close()
    except Exception as exc:            # noqa: BLE001 - reported, not swallowed
        _graph_cache["error"] = "%s: %s" % (type(exc).__name__, exc)
    try:
        stats_api.get_engine()          # warm the ranked backlog
    except Exception as exc:            # noqa: BLE001
        _graph_cache["stats_error"] = "%s: %s" % (type(exc).__name__, exc)
    yield
    _graph_cache.clear()


app = FastAPI(
    title="TrustSphere Explainability Service",
    description="Stage 4 (GraphRAG grounding: why did this alert fire) and "
                "stage 5a (backlog analytics: which alerts, how many, how bad) "
                "behind one base URL, with /chat routing between them.",
    version="1.1.0",
    lifespan=lifespan,
)

# The demo site (demo/) is static HTML served from its own port (see
# `python main.py serve` and `python -m http.server` in demo/) -- a different
# origin than this API, so the browser needs CORS to call /chat from it.
# Regex rather than a fixed origin list because the static server's port is
# whatever the demo happens to be served on.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Stage 5a: /chat, /stats/query, /stats/summary, /stats/fields
app.include_router(stats_api.router, tags=["stage5a-backlog-analytics"])


class GroundRequest(BaseModel):
    risk_driver: str = Field(..., description="Risk driver text extracted from an alert by stages 1-3, "
                                               "e.g. 'beneficial ownership structure flagged: UAE-resident "
                                               "owner ... controlling stake in ...'")
    top_k: int = Field(5, ge=1, le=20, description="Number of regulatory chunks to retrieve by vector similarity")
    hops: int = Field(2, ge=1, le=3, description="Max graph traversal distance from seed entities")


class VectorMatch(BaseModel):
    CHUNK_ID: str
    DOC_NAME: str
    SECTION: str
    CHUNK_INDEX: int
    CHUNK_TEXT: str
    SCORE: float


class GraphNodeOut(BaseModel):
    node_id: str
    node_type: str
    label: str
    properties: dict
    hop_distance: int


class GraphEdgeOut(BaseModel):
    source_node: str
    target_node: str
    edge_type: str
    properties: dict


class GraphContext(BaseModel):
    seed_nodes: list[str]
    nodes: list[GraphNodeOut]
    edges: list[GraphEdgeOut]
    truncated: bool


class GroundResponse(BaseModel):
    risk_driver: str
    vector_matches: list[VectorMatch]
    graph_context: GraphContext


@app.get("/health")
def health() -> dict:
    out = {"status": "ok", "graph_loaded": "graph" in _graph_cache,
           "graph_nodes": _graph_cache["graph"].number_of_nodes() if "graph" in _graph_cache else 0}
    if "error" in _graph_cache:
        out["graph_error"] = _graph_cache["error"]
    if "stats_error" in _graph_cache:
        out["stats_error"] = _graph_cache["stats_error"]
    return out


@app.post("/ground-risk-driver", response_model=GroundResponse)
def ground_risk_driver(req: GroundRequest) -> GroundResponse:
    """Stage 4 entry point. Stages 1-3 produce `risk_driver`; this returns
    {vector_matches, graph_context} for stage 5 to narrate into plain English."""
    conn = get_connection()
    try:
        result = retrieve(conn, req.risk_driver, top_k=req.top_k, hops=req.hops, graph=_graph_cache.get("graph"))
    finally:
        conn.close()
    return GroundResponse(**result.to_dict())


@app.post("/admin/refresh-graph")
def refresh_graph() -> dict:
    """Reload the in-memory graph after re-running ingestion -- the node/edge
    tables can change (re-ingestion, new alerts) without restarting the process."""
    conn = get_connection()
    try:
        _graph_cache["graph"] = store.load_networkx_graph(conn)
    finally:
        conn.close()
    return {"status": "refreshed", "graph_nodes": _graph_cache["graph"].number_of_nodes(),
            "graph_edges": _graph_cache["graph"].number_of_edges()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("pipeline.rag.service:app", host="127.0.0.1", port=8010, reload=False)
