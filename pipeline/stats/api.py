"""Stage 5a - HTTP surface, as an APIRouter mounted by pipeline/rag/service.py.

A router rather than a second app so the chatbot has one base URL and one
process, and so /chat can route between this stage and stage 4's GraphRAG
without a network hop between them.

Three levels of access, deliberately:

    POST /stats/query   structured QuerySpec in, rows out. No LLM anywhere.
                        This is what a UI, a scheduled report or an examiner
                        re-running a figure should call.
    POST /chat          natural language in. Returns the answer AND the spec
                        that produced it, so the structured call above can
                        always reproduce it.
    GET  /stats/summary standing shape of the backlog, no query needed.

/chat always returns `spec`, `assumptions` and `provenance` alongside the prose.
The prose is the least trustworthy part of the response and the easiest to
check against the other three.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from pipeline.stats import nl
from pipeline.stats.engine import StatsEngine
from pipeline.stats.spec import QuerySpec, field_catalogue

router = APIRouter()

_engine: dict[str, StatsEngine] = {}


def get_engine() -> StatsEngine:
    """One cached engine per process. Every answer in a conversation therefore
    comes from the same ranked snapshot; refresh is explicit."""
    if "engine" not in _engine:
        _engine["engine"] = StatsEngine()
        _engine["engine"].frame()          # warm the rank
    return _engine["engine"]


# --------------------------------------------------------------------------
# schemas
# --------------------------------------------------------------------------

class QueryResponse(BaseModel):
    spec: dict
    spec_english: str
    n_matched: int
    n_returned: int
    rows: list[dict]
    provenance: dict


class ChatRequest(BaseModel):
    message: str = Field(..., description="Analyst's question in plain English.")
    use_llm: bool = Field(True, description="Use AI Core for spec extraction and "
                                            "narration; false = deterministic only.")
    narrate: bool = Field(True, description="Return prose as well as rows.")


class ChatResponse(BaseModel):
    route: str                       # 'analytical' | 'explanatory' | 'clarify'
    answer: str
    spec: dict | None = None
    spec_english: str | None = None
    assumptions: list[str] = []
    extraction_method: str | None = None
    n_matched: int | None = None
    rows: list[dict] = []
    provenance: dict | None = None


# --------------------------------------------------------------------------
# endpoints
# --------------------------------------------------------------------------

@router.get("/stats/health")
def stats_health() -> dict:
    e = get_engine()
    return {"status": "ok", "backlog": int(len(e.frame())),
            "as_of": str(e.ranker.as_of.date())}


@router.get("/stats/fields")
def stats_fields() -> dict:
    """The query surface. A UI can build filter controls from this, and it is
    the same catalogue the LLM prompt is generated from."""
    return field_catalogue()


@router.get("/stats/summary")
def stats_summary() -> dict:
    return get_engine().summary()


@router.post("/stats/query", response_model=QueryResponse)
def stats_query(spec: QuerySpec) -> QueryResponse:
    """Deterministic path: no language model is involved at any point."""
    return QueryResponse(**get_engine().run(spec))


@router.post("/stats/refresh")
def stats_refresh() -> dict:
    """Re-rank after new alerts land or resolutions post."""
    e = get_engine()
    e.frame(recompute=True)
    return {"status": "refreshed", "backlog": int(len(e.frame()))}


@router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    """Front door. Routes analytical questions here and explanatory ones to
    stage 4, rather than trying to answer 'how many' with vector search."""
    route = nl.classify_intent(req.message)

    if route == "explanatory":
        return ChatResponse(
            route="explanatory",
            answer="This is a question about why a specific alert fired. Send it "
                   "to POST /ground-risk-driver (stage 4), which returns the "
                   "regulatory citations and graph context for narration.",
            assumptions=["Routed to the GraphRAG grounding layer, not the "
                         "backlog statistics engine."])

    ext = nl.extract_spec(req.message, use_llm=req.use_llm)
    if not ext.ok:
        return ChatResponse(route="clarify", answer=ext.clarify or
                            "I could not turn that into a query over the alert backlog.",
                            extraction_method=ext.method, assumptions=ext.assumptions)

    try:
        result = get_engine().run(ext.spec)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="query execution failed: %s" % exc)

    if req.narrate:
        try:
            answer = nl.narrate(req.message, result, ext.assumptions) if req.use_llm \
                else nl.narrate_fallback(result, ext.assumptions)
        except Exception:
            answer = nl.narrate_fallback(result, ext.assumptions)
    else:
        answer = nl.narrate_fallback(result, ext.assumptions)

    return ChatResponse(
        route="analytical", answer=answer,
        spec=result["spec"], spec_english=result["spec_english"],
        assumptions=ext.assumptions, extraction_method=ext.method,
        n_matched=result["n_matched"], rows=result["rows"],
        provenance=result["provenance"])
