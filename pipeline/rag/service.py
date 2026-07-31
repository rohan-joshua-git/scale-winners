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
from pydantic import BaseModel, Field

from pipeline.ingestion.hana_source import get_connection
from pipeline.rag import store
from pipeline.rag.retrieve import retrieve

_graph_cache: dict[str, nx.MultiDiGraph] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = get_connection()
    try:
        _graph_cache["graph"] = store.load_networkx_graph(conn)
    finally:
        conn.close()
    yield
    _graph_cache.clear()


app = FastAPI(
    title="TrustSphere GraphRAG Grounding Service",
    description="Stage 4 of the explainability pipeline: given a risk driver, "
                "return regulatory vector matches + connected graph context.",
    version="1.0.0",
    lifespan=lifespan,
)


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
    return {"status": "ok", "graph_loaded": "graph" in _graph_cache,
            "graph_nodes": _graph_cache["graph"].number_of_nodes() if "graph" in _graph_cache else 0}


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
