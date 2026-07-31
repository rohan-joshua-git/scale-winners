"""Read/write helpers for the RAG_CHUNKS / RAG_NODES / RAG_EDGES tables.
hdbcli binds a Python list directly to a REAL_VECTOR column parameter (verified
live against this instance), so no TO_REAL_VECTOR(json_string) round-tripping.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import networkx as nx
import pandas as pd

from pipeline.rag import config


@dataclass
class NodeRecord:
    node_id: str
    node_type: str
    label: str
    properties: dict = field(default_factory=dict)
    source: str = ""


@dataclass
class EdgeRecord:
    source_node: str
    target_node: str
    edge_type: str
    properties: dict = field(default_factory=dict)


def upsert_chunk(conn, chunk_id: str, doc_name: str, section: str, chunk_index: int,
                  text: str, embedding: list[float]) -> None:
    cur = conn.cursor()
    cur.execute(f'DELETE FROM "{config.CHUNKS_TABLE}" WHERE CHUNK_ID = ?', (chunk_id,))
    cur.execute(
        f'INSERT INTO "{config.CHUNKS_TABLE}" '
        f'(CHUNK_ID, DOC_NAME, SECTION, CHUNK_INDEX, CHUNK_TEXT, EMBEDDING) '
        f'VALUES (?, ?, ?, ?, ?, ?)',
        (chunk_id, doc_name, section, chunk_index, text, embedding),
    )


def upsert_node(conn, node: NodeRecord) -> None:
    cur = conn.cursor()
    cur.execute(f'DELETE FROM "{config.NODES_TABLE}" WHERE NODE_ID = ?', (node.node_id,))
    cur.execute(
        f'INSERT INTO "{config.NODES_TABLE}" (NODE_ID, NODE_TYPE, LABEL, PROPERTIES, SOURCE) '
        f'VALUES (?, ?, ?, ?, ?)',
        (node.node_id, node.node_type, node.label, json.dumps(node.properties), node.source),
    )


_edge_id_counter = None


def _next_edge_id(cur) -> int:
    global _edge_id_counter
    if _edge_id_counter is None:
        cur.execute(f'SELECT COALESCE(MAX(EDGE_ID), 0) FROM "{config.EDGES_TABLE}"')
        _edge_id_counter = cur.fetchone()[0]
    _edge_id_counter += 1
    return _edge_id_counter


def insert_edge(conn, edge: EdgeRecord) -> None:
    cur = conn.cursor()
    edge_id = _next_edge_id(cur)
    cur.execute(
        f'INSERT INTO "{config.EDGES_TABLE}" (EDGE_ID, SOURCE_NODE, TARGET_NODE, EDGE_TYPE, PROPERTIES) '
        f'VALUES (?, ?, ?, ?, ?)',
        (edge_id, edge.source_node, edge.target_node, edge.edge_type, json.dumps(edge.properties)),
    )


def bulk_insert_nodes(conn, nodes: list[NodeRecord]) -> int:
    """Batch insert for the operational-graph ETL (thousands of rows). Some node
    types (Jurisdiction, SanctionsProgram) use the same canonical_node_id() scheme
    as the regulatory-extraction pass, so a node the LLM pass already created can
    collide here -- first writer wins (existing row kept, not merged); everything
    else is a plain batch insert since those IDs are guaranteed fresh."""
    if not nodes:
        return 0
    dedup: dict[str, NodeRecord] = {n.node_id: n for n in nodes}  # last write in this batch wins per id
    cur = conn.cursor()

    ids = list(dedup.keys())
    existing: set[str] = set()
    chunk = 1000
    for i in range(0, len(ids), chunk):
        batch = ids[i:i + chunk]
        placeholders = ",".join("?" for _ in batch)
        cur.execute(f'SELECT NODE_ID FROM "{config.NODES_TABLE}" WHERE NODE_ID IN ({placeholders})', batch)
        existing.update(r[0] for r in cur.fetchall())

    to_insert = [n for nid, n in dedup.items() if nid not in existing]
    if to_insert:
        cur.executemany(
            f'INSERT INTO "{config.NODES_TABLE}" (NODE_ID, NODE_TYPE, LABEL, PROPERTIES, SOURCE) '
            f'VALUES (?, ?, ?, ?, ?)',
            [(n.node_id, n.node_type, n.label, json.dumps(n.properties), n.source) for n in to_insert],
        )
    return len(to_insert)


def bulk_insert_edges(conn, edges: list[EdgeRecord]) -> int:
    if not edges:
        return 0
    cur = conn.cursor()
    start_id = _next_edge_id_batch(cur, len(edges))
    cur.executemany(
        f'INSERT INTO "{config.EDGES_TABLE}" (EDGE_ID, SOURCE_NODE, TARGET_NODE, EDGE_TYPE, PROPERTIES) '
        f'VALUES (?, ?, ?, ?, ?)',
        [(start_id + i, e.source_node, e.target_node, e.edge_type, json.dumps(e.properties))
         for i, e in enumerate(edges)],
    )
    return len(edges)


def _next_edge_id_batch(cur, count: int) -> int:
    global _edge_id_counter
    if _edge_id_counter is None:
        cur.execute(f'SELECT COALESCE(MAX(EDGE_ID), 0) FROM "{config.EDGES_TABLE}"')
        _edge_id_counter = cur.fetchone()[0]
    start = _edge_id_counter + 1
    _edge_id_counter += count
    return start


def delete_nodes_by_source(conn, source: str) -> None:
    cur = conn.cursor()
    cur.execute(f'SELECT NODE_ID FROM "{config.NODES_TABLE}" WHERE SOURCE = ?', (source,))
    ids = [r[0] for r in cur.fetchall()]
    if not ids:
        return
    cur.execute(f'DELETE FROM "{config.EDGES_TABLE}" WHERE SOURCE_NODE IN '
                f'(SELECT NODE_ID FROM "{config.NODES_TABLE}" WHERE SOURCE = ?)', (source,))
    cur.execute(f'DELETE FROM "{config.EDGES_TABLE}" WHERE TARGET_NODE IN '
                f'(SELECT NODE_ID FROM "{config.NODES_TABLE}" WHERE SOURCE = ?)', (source,))
    cur.execute(f'DELETE FROM "{config.NODES_TABLE}" WHERE SOURCE = ?', (source,))
    conn.commit()


def node_exists(conn, node_id: str) -> bool:
    cur = conn.cursor()
    cur.execute(f'SELECT COUNT(*) FROM "{config.NODES_TABLE}" WHERE NODE_ID = ?', (node_id,))
    return cur.fetchone()[0] > 0


def edge_exists(conn, source_node: str, target_node: str, edge_type: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        f'SELECT COUNT(*) FROM "{config.EDGES_TABLE}" '
        f'WHERE SOURCE_NODE = ? AND TARGET_NODE = ? AND EDGE_TYPE = ?',
        (source_node, target_node, edge_type),
    )
    return cur.fetchone()[0] > 0


def counts(conn) -> dict:
    cur = conn.cursor()
    out = {}
    for table in [config.CHUNKS_TABLE, config.NODES_TABLE, config.EDGES_TABLE]:
        cur.execute(f'SELECT COUNT(*) FROM "{table}"')
        out[table] = cur.fetchone()[0]
    cur.execute(f'SELECT NODE_TYPE, COUNT(*) FROM "{config.NODES_TABLE}" GROUP BY NODE_TYPE')
    out["nodes_by_type"] = dict(cur.fetchall())
    cur.execute(f'SELECT EDGE_TYPE, COUNT(*) FROM "{config.EDGES_TABLE}" GROUP BY EDGE_TYPE')
    out["edges_by_type"] = dict(cur.fetchall())
    return out


def vector_search(conn, query_embedding: list[float], top_k: int = 5,
                   doc_name: str | None = None) -> pd.DataFrame:
    """Top-k chunks by cosine similarity to query_embedding, using HANA's native
    COSINE_SIMILARITY over the REAL_VECTOR column (computed in-database, not in
    Python). COSINE_SIMILARITY(vector_col, TO_REAL_VECTOR(?)) only resolves when
    the vector literal is inlined -- binding it as a query parameter makes hdbcli
    send it as NSTRING and HANA rejects the overload (verified live). The literal
    is a comma-joined list of floats we generated ourselves (not user input), so
    there's no injection surface -- still, values are formatted via repr(), never
    raw string interpolation of untrusted text.
    """
    cur = conn.cursor()
    vector_literal = "[" + ",".join(repr(float(x)) for x in query_embedding) + "]"
    where = "WHERE DOC_NAME = ?" if doc_name else ""
    params: tuple = (doc_name, top_k) if doc_name else (top_k,)
    cur.execute(
        f"SELECT CHUNK_ID, DOC_NAME, SECTION, CHUNK_INDEX, CHUNK_TEXT, "
        f"COSINE_SIMILARITY(EMBEDDING, TO_REAL_VECTOR('{vector_literal}')) AS SCORE "
        f'FROM "{config.CHUNKS_TABLE}" {where} '
        f"ORDER BY SCORE DESC "
        f"LIMIT ?",
        params,
    )
    cols = [d[0] for d in cur.description]
    return pd.DataFrame(cur.fetchall(), columns=cols)


def load_networkx_graph(conn) -> nx.MultiDiGraph:
    """Pull the full node/edge tables into an in-memory networkx graph for
    multi-hop traversal. Fine at this data volume (thousands of nodes/edges);
    see README for how this would need to change at real scale."""
    cur = conn.cursor()
    g = nx.MultiDiGraph()

    cur.execute(f'SELECT NODE_ID, NODE_TYPE, LABEL, PROPERTIES, SOURCE FROM "{config.NODES_TABLE}"')
    for node_id, node_type, label, properties, source in cur.fetchall():
        g.add_node(node_id, node_type=node_type, label=label,
                    properties=json.loads(properties) if properties else {}, source=source)

    cur.execute(f'SELECT SOURCE_NODE, TARGET_NODE, EDGE_TYPE, PROPERTIES FROM "{config.EDGES_TABLE}"')
    for source_node, target_node, edge_type, properties in cur.fetchall():
        g.add_edge(source_node, target_node, edge_type=edge_type,
                   properties=json.loads(properties) if properties else {})

    return g
