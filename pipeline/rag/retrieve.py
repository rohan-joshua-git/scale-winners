"""Phase 2 -- retrieval: given a RiskDriver text, combine HANA vector search over
regulatory chunks with 1-2 hop graph traversal seeded from (a) entities the risk
driver text names directly (a Client/BeneficialOwner/Jurisdiction) and (b) the
regulatory nodes sourced from whichever documents the top vector hits came from.

Entity linking here is a simple case-insensitive label substring match, not NER/
embedding-based entity resolution -- adequate for a hackathon demo, flagged in
the README as the first thing to upgrade at real scale (this is the standard
"entity linking" problem GraphRAG systems solve with NER + a fuzzy index).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import networkx as nx

from pipeline.rag import store
from pipeline.rag.ai_core_client import embed_text

SEED_NODE_TYPES = ["Client", "BeneficialOwner", "Jurisdiction", "SanctionsProgram"]
MIN_LABEL_LEN_FOR_MATCH = 4
DEFAULT_MAX_FANOUT_PER_HOP = 8

# Only these node types propagate "named in the query" priority to their
# neighbors during traversal. Jurisdiction/SanctionsProgram/RegulatoryClause are
# "category" nodes -- a jurisdiction can be a text seed too (e.g. "Hong Kong"
# mentioned alongside a company), but that shouldn't elevate its entire resident
# population above the query's actual named entities. Caught live: naming both
# a person and their companies' jurisdictions in the same risk driver made the
# jurisdictions' capped-but-still-numerous high-relevance neighbors outcompete
# the person's own second company for the final result's remaining slots.
PRIORITY_PROPAGATING_TYPES = {"Client", "BeneficialOwner", "Alert"}


def _node_relevance_score(graph: nx.MultiDiGraph, node_id: str) -> float:
    """Ranks a node's interestingness for traversal, used only to decide which
    neighbors survive when a hub (e.g. a Jurisdiction with hundreds of resident
    beneficial owners) has more neighbors than DEFAULT_MAX_FANOUT_PER_HOP allows.
    Without this, expanding from a hub returns an arbitrary/incidental sample of
    its neighbors instead of the ones actually worth surfacing -- caught live
    when a bare "IRAN" query pulled in 50 unrelated Iran-resident owners instead
    of the one with an actual sanctions match."""
    data = graph.nodes[node_id]
    node_type = data.get("node_type")
    props = data.get("properties", {})
    if node_type == "BeneficialOwner":
        return (2.0 if props.get("sanctions_match") else 0.0) + (2.0 if props.get("is_pep") else 0.0)
    if node_type == "Client":
        score = (2.0 if props.get("sanctions_hit") else 0.0) + (1.5 if props.get("pep_associated") else 0.0)
        score += {"HIGH": 1.0, "MEDIUM": 0.5}.get(props.get("kyc_risk_rating"), 0.0)
        return score
    if node_type == "Alert":
        return {"CRITICAL": 2.0, "HIGH": 1.0, "MEDIUM": 0.5}.get(props.get("priority"), 0.0)
    # Jurisdiction/SanctionsProgram/RegulatoryClause aren't the hub-fanout problem
    # themselves (they're comparatively few and usually the point of the query) --
    # keep them uniformly above the "uninteresting instance" baseline.
    return 1.0


@dataclass
class GraphNode:
    node_id: str
    node_type: str
    label: str
    properties: dict
    hop_distance: int


@dataclass
class GraphEdge:
    source_node: str
    target_node: str
    edge_type: str
    properties: dict


@dataclass
class RetrievalResult:
    risk_driver: str
    vector_matches: list[dict] = field(default_factory=list)
    graph_seeds: list[str] = field(default_factory=list)
    graph_nodes: list[GraphNode] = field(default_factory=list)
    graph_edges: list[GraphEdge] = field(default_factory=list)
    graph_truncated: bool = False

    def to_dict(self) -> dict:
        return {
            "risk_driver": self.risk_driver,
            "vector_matches": self.vector_matches,
            "graph_context": {
                "seed_nodes": self.graph_seeds,
                "nodes": [asdict(n) for n in self.graph_nodes],
                "edges": [asdict(e) for e in self.graph_edges],
                "truncated": self.graph_truncated,
            },
        }


def find_seed_nodes_from_text(graph: nx.MultiDiGraph, text: str) -> list[str]:
    text_lower = text.lower()
    seeds = []
    for node_id, data in graph.nodes(data=True):
        if data.get("node_type") not in SEED_NODE_TYPES:
            continue
        label = data.get("label", "")
        if len(label) < MIN_LABEL_LEN_FOR_MATCH:
            continue
        if label.lower() in text_lower:
            seeds.append(node_id)
    return seeds


def seeds_from_vector_hit_docs(graph: nx.MultiDiGraph, doc_names: set[str]) -> list[str]:
    """Nodes sourced from the same document(s) as the top vector hits -- restricted
    to Jurisdiction/SanctionsProgram (not RegulatoryClause): a document can have
    hundreds of clause nodes, and using all of them as BFS seeds would flood the
    result with noise. Clause nodes reachable within `hops` from these seeds still
    show up naturally via CITES_CLAUSE/SUBJECT_TO/LISTED_UNDER edges."""
    return [node_id for node_id, data in graph.nodes(data=True)
            if data.get("source") in doc_names and data.get("node_type") in ("Jurisdiction", "SanctionsProgram")]


def traverse(graph: nx.MultiDiGraph, text_seed_ids: list[str], doc_seed_ids: list[str], hops: int = 2,
             max_nodes: int = 60,
             max_fanout_per_hop: int = DEFAULT_MAX_FANOUT_PER_HOP) -> tuple[list[GraphNode], list[GraphEdge], bool]:
    """Level-by-level BFS (not nx.single_source_shortest_path_length) so fan-out
    from any single node can be capped: when a node has more unvisited neighbors
    than max_fanout_per_hop, only the highest-_node_relevance_score neighbors are
    kept. This matters for hub nodes (a Jurisdiction can have hundreds of
    SUBJECT_TO edges from resident beneficial owners) -- capping here means hop 1
    from "Iran" surfaces the owner with an actual sanctions match rather than an
    arbitrary sample of everyone who lives there.

    Separately, `text_seed_ids` (entities the risk driver text actually names)
    are tracked through the walk and always outrank `doc_seed_ids` (broad
    "same document as a vector hit" context) when trimming to max_nodes --
    fan-out capping alone isn't enough: a query naming a specific person still
    needs their own companies/alerts to survive even when dozens of doc-seeded
    jurisdictions each contribute their own high-relevance_score neighbors that
    have nothing to do with the query. Caught live: without this, "Erling Koch"
    lost his own OWNS/SHARES_OWNER_WITH/TRIGGERED_BY edges to unrelated flagged
    owners reached only through incidental document co-location.
    """
    undirected = graph.to_undirected(as_view=True)
    seed_ids = list(dict.fromkeys(text_seed_ids + doc_seed_ids))
    hop_distance: dict[str, int] = {}
    from_text: dict[str, bool] = {}
    for s in seed_ids:
        if s in undirected:
            hop_distance[s] = 0
            from_text[s] = s in text_seed_ids
    frontier = set(hop_distance.keys())

    for hop in range(1, hops + 1):
        next_frontier: set[str] = set()
        candidate_origin: dict[str, bool] = {}
        for node_id in frontier:
            neighbors = [n for n in undirected.neighbors(node_id) if n not in hop_distance]
            if len(neighbors) > max_fanout_per_hop:
                neighbors.sort(key=lambda n: _node_relevance_score(graph, n), reverse=True)
                neighbors = neighbors[:max_fanout_per_hop]
            parent_propagates = from_text[node_id] and graph.nodes[node_id]["node_type"] in PRIORITY_PROPAGATING_TYPES
            for n in neighbors:
                next_frontier.add(n)
                candidate_origin[n] = candidate_origin.get(n, False) or parent_propagates
        for n in next_frontier:
            hop_distance[n] = hop
            from_text[n] = candidate_origin[n]
        frontier = next_frontier
        if not frontier:
            break

    truncated = len(hop_distance) > max_nodes
    # Closest hop first; within a hop, entities the query actually named (and
    # anything reached only through them) outrank generic document context;
    # within that, most _node_relevance_score first.
    kept_ids = sorted(
        hop_distance,
        key=lambda n: (hop_distance[n], 0 if from_text[n] else 1, -_node_relevance_score(graph, n)),
    )[:max_nodes]
    node_set = set(kept_ids)

    nodes = [
        GraphNode(node_id=nid, node_type=graph.nodes[nid]["node_type"], label=graph.nodes[nid]["label"],
                  properties=graph.nodes[nid]["properties"], hop_distance=hop_distance[nid])
        for nid in kept_ids
    ]

    edges = []
    seen_edges = set()
    for u, v, data in graph.edges(data=True):
        if u in node_set and v in node_set:
            key = (u, v, data["edge_type"])
            if key in seen_edges:
                continue
            seen_edges.add(key)
            edges.append(GraphEdge(source_node=u, target_node=v, edge_type=data["edge_type"],
                                    properties=data.get("properties", {})))
    return nodes, edges, truncated


def retrieve(conn, risk_driver: str, top_k: int = 5, hops: int = 2,
             graph: nx.MultiDiGraph | None = None) -> RetrievalResult:
    """Given a RiskDriver text, run vector search over regulatory chunks and
    1-2 hop graph traversal seeded from entities the text names + the docs the
    top vector hits came from. Pass a pre-loaded `graph` to avoid re-pulling the
    full node/edge tables on every call (see README on caching at real scale)."""
    embedding = embed_text(risk_driver)
    matches_df = store.vector_search(conn, embedding, top_k=top_k)
    vector_matches = matches_df.to_dict(orient="records")

    if graph is None:
        graph = store.load_networkx_graph(conn)

    text_seeds = find_seed_nodes_from_text(graph, risk_driver)
    doc_names = {m["DOC_NAME"] for m in vector_matches}
    doc_seeds = seeds_from_vector_hit_docs(graph, doc_names)
    all_seeds = list(dict.fromkeys(text_seeds + doc_seeds))  # dedup, keep order, for reporting only

    nodes, edges, truncated = traverse(graph, text_seeds, doc_seeds, hops=hops)

    return RetrievalResult(
        risk_driver=risk_driver,
        vector_matches=vector_matches,
        graph_seeds=all_seeds,
        graph_nodes=nodes,
        graph_edges=edges,
        graph_truncated=truncated,
    )
