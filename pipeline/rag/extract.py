"""LLM extraction pass: given a regulatory text chunk, prompt gpt-4o-mini (via the
orchestration deployment) to pull out ontology-conformant nodes/edges. Scoped to
RegulatoryClause / Jurisdiction / SanctionsProgram nodes and CITES_CLAUSE /
SUBJECT_TO / LISTED_UNDER edges -- the node/edge types that can actually appear
in regulatory text (see config.py for the full ontology and why the rest of it
is populated by ingest_operational_graph.py instead).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from pipeline.rag import config
from pipeline.rag.ai_core_client import complete

_SYSTEM_PROMPT = f"""You extract structured knowledge-graph entities and relationships from AML/CFT regulatory text, for a bank's alert-investigation system.

Only extract these node types: {", ".join(config.REGULATORY_NODE_TYPES)}
- RegulatoryClause: a specific rule, statement, or numbered provision in the text (label = a short descriptive title, NOT the full clause text)
- Jurisdiction: a country or territory
- SanctionsProgram: a named sanctions regime/list (e.g. "FATF Increased Monitoring", "OFAC Iran Sanctions", "EU Consolidated Sanctions List")

Only extract these edge types: {", ".join(config.REGULATORY_EDGE_TYPES)}
- CITES_CLAUSE: one clause references another
- LISTED_UNDER: a Jurisdiction (or entity) appears on a named list/program -- FATF grey/black list, an OFAC program, the EU consolidated list. Use this for "country X is on list Y", not SUBJECT_TO.
- SUBJECT_TO: a Jurisdiction is subject to a specific numbered regulatory clause's due-diligence requirement (e.g. a MAS Notice 626 clause on beneficial ownership CDD). Only use this when a specific clause is being applied, not for list membership.

Respond with ONLY a JSON object, no markdown fences, no commentary:
{{"nodes": [{{"type": "...", "label": "...", "properties": {{}}}}],
  "edges": [{{"type": "...", "source_label": "...", "target_label": "...", "properties": {{}}}}]}}

If nothing relevant is in the text, return {{"nodes": [], "edges": []}}. Do not invent facts not present in the text."""


@dataclass
class ExtractionResult:
    nodes: list[dict] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)
    error: str | None = None


# The model occasionally emits the ontology's own type name as if it were an
# entity value (e.g. a node {"type": "Jurisdiction", "label": "Jurisdiction"}).
# Caught live in the first full run -- these merge into one node via
# canonical_node_id() and become a false hub connecting unrelated clauses.
_PLACEHOLDER_LABELS = {t.casefold() for t in config.ALL_NODE_TYPES + config.ALL_EDGE_TYPES} | {
    "jurisdiction", "sanctionsprogram", "sanctions program", "regulatoryclause", "regulatory clause",
    "many jurisdictions", "entity", "country", "clause",
}


def _is_placeholder_label(label: str) -> bool:
    return label.strip().casefold() in _PLACEHOLDER_LABELS


def _parse_json_response(content: str) -> dict:
    content = content.strip()
    content = re.sub(r"^```(json)?", "", content).strip()
    content = re.sub(r"```$", "", content).strip()
    # Some responses append stray trailing text after the JSON object -- parse
    # just the first valid JSON value and ignore anything after it.
    obj, _ = json.JSONDecoder().raw_decode(content)
    return obj


def extract_from_chunk(chunk_text: str, doc_name: str, section: str) -> ExtractionResult:
    user_prompt = f"Source document: {doc_name}\nSection: {section}\n\nText:\n{chunk_text}"
    try:
        result = complete(
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            model_name=config.EXTRACTION_MODEL_NAME,
            max_tokens=3000,
        )
        parsed = _parse_json_response(result.content)
    except Exception as e:
        return ExtractionResult(error=f"{type(e).__name__}: {e}")

    nodes = [n for n in parsed.get("nodes", [])
             if n.get("type") in config.REGULATORY_NODE_TYPES and n.get("label")
             and not _is_placeholder_label(n["label"])]
    edges = [e for e in parsed.get("edges", [])
             if e.get("type") in config.REGULATORY_EDGE_TYPES and e.get("source_label") and e.get("target_label")
             and not _is_placeholder_label(e["source_label"]) and not _is_placeholder_label(e["target_label"])]
    return ExtractionResult(nodes=nodes, edges=edges)
