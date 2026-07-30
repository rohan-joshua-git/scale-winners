"""Phase 1 -- regulatory ingestion: chunk the 6 source documents, embed each chunk
via AI Core (text-embedding-3-small) and store as HANA REAL_VECTOR, then run an
LLM extraction pass (gpt-4o-mini via AI Core orchestration) over each chunk to
pull RegulatoryClause/Jurisdiction/SanctionsProgram nodes and CITES_CLAUSE/
SUBJECT_TO/LISTED_UNDER edges, deduping across documents by canonical label.

Run: python -m pipeline.rag.ingest_regulatory
Prints a node/edge/chunk count summary at the end so extraction quality can be
sanity-checked before moving on to Phase 2.
"""
from __future__ import annotations

import time

from pipeline.ingestion.hana_source import get_connection
from pipeline.rag import config, hana_schema, store
from pipeline.rag.ai_core_client import embed_texts
from pipeline.rag.chunking import chunk_file
from pipeline.rag.dedup import canonical_label, canonical_node_id
from pipeline.rag.extract import extract_from_chunk
from pipeline.rag.store import EdgeRecord, NodeRecord


def _chunk_id(doc_name: str, index: int) -> str:
    return f"{doc_name}#{index}"


def ingest_document(conn, doc_name: str) -> dict:
    path = config.REGULATORY_DOCS_DIR / doc_name
    if not path.exists():
        return {"doc": doc_name, "error": f"file not found: {path}"}

    chunks = chunk_file(path, doc_name=doc_name)
    print(f"\n{doc_name}: {len(chunks)} chunks")

    embeddings = embed_texts([c.text for c in chunks])

    extraction_failures = 0
    nodes_created = 0
    edges_created = 0

    for chunk, embedding in zip(chunks, embeddings):
        chunk_id = _chunk_id(doc_name, chunk.index)
        store.upsert_chunk(conn, chunk_id=chunk_id, doc_name=doc_name, section=chunk.section,
                            chunk_index=chunk.index, text=chunk.text, embedding=embedding)

        result = extract_from_chunk(chunk.text, doc_name=doc_name, section=chunk.section)
        if result.error:
            extraction_failures += 1
            print(f"  [chunk {chunk.index}] extraction failed, skipping: {result.error}")
            continue

        try:
            label_to_id: dict[str, str] = {}
            for n in result.nodes:
                label = canonical_label(n["label"])
                node_id = canonical_node_id(n["type"], label)
                label_to_id[n["label"]] = node_id
                is_new = not store.node_exists(conn, node_id)
                store.upsert_node(conn, NodeRecord(
                    node_id=node_id, node_type=n["type"], label=label,
                    properties=n.get("properties", {}), source=doc_name,
                ))
                if is_new:
                    nodes_created += 1

            for e in result.edges:
                src_label = e["source_label"]
                tgt_label = e["target_label"]
                # Edge endpoints must resolve to node types declared for this pass;
                # infer type from whichever node in this chunk's extraction matches,
                # falling back to Jurisdiction (the most common untyped reference).
                src_type = next((n["type"] for n in result.nodes if n["label"] == src_label), "Jurisdiction")
                tgt_type = next((n["type"] for n in result.nodes if n["label"] == tgt_label), "SanctionsProgram")
                src_id = label_to_id.get(src_label) or canonical_node_id(src_type, src_label)
                tgt_id = label_to_id.get(tgt_label) or canonical_node_id(tgt_type, tgt_label)

                # LLMs sometimes reference an edge endpoint (e.g. the jurisdiction a
                # whole notice implicitly applies to) without also listing it in
                # "nodes". Auto-create a minimal node rather than silently losing a
                # real relationship -- inferred type/label, no invented properties.
                for node_id, node_type, label in [(src_id, src_type, src_label), (tgt_id, tgt_type, tgt_label)]:
                    if not store.node_exists(conn, node_id):
                        store.upsert_node(conn, NodeRecord(
                            node_id=node_id, node_type=node_type,
                            label=canonical_label(label), properties={}, source=doc_name,
                        ))
                        nodes_created += 1

                if store.edge_exists(conn, src_id, tgt_id, e["type"]):
                    continue
                store.insert_edge(conn, EdgeRecord(
                    source_node=src_id, target_node=tgt_id, edge_type=e["type"],
                    properties=e.get("properties", {}),
                ))
                edges_created += 1
            conn.commit()
        except Exception as ex:
            conn.rollback()
            extraction_failures += 1
            print(f"  [chunk {chunk.index}] storing extracted nodes/edges failed, "
                  f"skipping chunk, continuing: {type(ex).__name__}: {ex}")
            continue

    conn.commit()
    print(f"  -> {nodes_created} nodes, {edges_created} edges, "
          f"{extraction_failures}/{len(chunks)} chunks failed extraction")
    return {"doc": doc_name, "chunks": len(chunks), "nodes": nodes_created,
             "edges": edges_created, "extraction_failures": extraction_failures}


def main():
    conn = get_connection()
    try:
        print("Creating schema (chunks/nodes/edges tables + graph workspace)...")
        for action in hana_schema.create_all(conn):
            print(f"  {action}")

        results = []
        start = time.time()
        for doc_name in config.REGULATORY_DOCS:
            try:
                results.append(ingest_document(conn, doc_name))
            except Exception as e:
                print(f"  DOCUMENT FAILED, continuing with the rest: {doc_name}: {type(e).__name__}: {e}")
                results.append({"doc": doc_name, "error": str(e)})
        elapsed = time.time() - start

        print("\n" + "=" * 60)
        print("Phase 1 -- Regulatory Ingestion Summary")
        print("=" * 60)
        for r in results:
            if "error" in r:
                print(f"  {r['doc']}: FAILED -- {r['error']}")
            else:
                print(f"  {r['doc']}: {r['chunks']} chunks, {r['nodes']} nodes, {r['edges']} edges, "
                      f"{r['extraction_failures']} extraction failures")

        totals = store.counts(conn)
        print(f"\nTotals in HANA: {totals[config.CHUNKS_TABLE]} chunks, "
              f"{totals[config.NODES_TABLE]} nodes, {totals[config.EDGES_TABLE]} edges")
        print(f"Nodes by type: {totals['nodes_by_type']}")
        print(f"Edges by type: {totals['edges_by_type']}")
        print(f"Elapsed: {elapsed:.1f}s")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
