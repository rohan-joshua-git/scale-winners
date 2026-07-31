"""Phase 2 deliverable: call retrieve() with a real risk driver grounded in the
EDA (a UAE-resident beneficial owner, Erling Koch, who owns two companies that
share his ownership -- Biovia PARTNERSHIP in Hong Kong, which has 2 prior
CRITICAL alerts, and Apextech LLC in Canada) and print vector_matches and
graph_context side by side.

Run: python -m pipeline.rag.demo_phase2
"""
from __future__ import annotations

from pipeline.ingestion.hana_source import get_connection
from pipeline.rag.retrieve import retrieve

SAMPLE_RISK_DRIVER = (
    "Beneficial ownership structure flagged: UAE-resident owner Erling Koch holds a 59.79% "
    "controlling stake in Biovia PARTNERSHIP (Hong Kong), which has prior CRITICAL alerts, "
    "and co-owns 38.14% of Apextech LLC (Canada) -- a beneficial ownership structure spanning "
    "a FATF grey-list jurisdiction."
)


def print_result(result) -> None:
    d = result.to_dict()
    print("=" * 70)
    print("RISK DRIVER")
    print("=" * 70)
    print(d["risk_driver"])

    print("\n" + "=" * 70)
    print(f"VECTOR MATCHES (top {len(d['vector_matches'])} regulatory chunks by cosine similarity)")
    print("=" * 70)
    for m in d["vector_matches"]:
        print(f"\n[{m['SCORE']:.3f}] {m['DOC_NAME']} chunk#{m['CHUNK_INDEX']}")
        print(f"  {m['CHUNK_TEXT'][:220].strip()}...")

    print("\n" + "=" * 70)
    gc = d["graph_context"]
    print(f"GRAPH CONTEXT ({len(gc['nodes'])} nodes / {len(gc['edges'])} edges within 2 hops"
          f"{' -- TRUNCATED for readability' if gc['truncated'] else ''})")
    print("=" * 70)
    print(f"\nSeed nodes ({len(gc['seed_nodes'])}): entities named in the risk driver text, "
          f"plus Jurisdiction/SanctionsProgram nodes from the top vector-hit documents")

    print("\n-- Nodes by hop distance --")
    for hop in sorted({n["hop_distance"] for n in gc["nodes"]}):
        at_hop = [n for n in gc["nodes"] if n["hop_distance"] == hop]
        print(f"  hop {hop} ({len(at_hop)}): " + ", ".join(f"[{n['node_type']}] {n['label']}" for n in at_hop[:12])
              + (" ..." if len(at_hop) > 12 else ""))

    print("\n-- Edges connecting the risk driver's named entities to the rest of the graph --")
    named_ids = {n["node_id"] for n in gc["nodes"] if n["hop_distance"] == 0}
    for e in gc["edges"]:
        if e["source_node"] in named_ids or e["target_node"] in named_ids:
            print(f"  {e['source_node']} -{e['edge_type']}-> {e['target_node']}")


def main():
    conn = get_connection()
    try:
        result = retrieve(conn, SAMPLE_RISK_DRIVER, top_k=5, hops=2)
    finally:
        conn.close()
    print_result(result)


if __name__ == "__main__":
    main()
