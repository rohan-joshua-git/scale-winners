"""DDL for the GraphRAG grounding-layer tables, created in the connecting user's
own schema (TEAM_07_USER has DDL rights there; the shared TEAM_07 reference
schema is read-only). Native HANA REAL_VECTOR for chunk embeddings; plain column
tables + a registered GRAPH WORKSPACE for nodes/edges -- see README for why this
stage uses tables + Python traversal instead of the RDF triple store or
GRAPH_TABLE/MATCH SQL (neither is usable on this HANA Cloud instance today).
"""
from pipeline.rag import config

_TABLE_DDL = {
    config.CHUNKS_TABLE: f"""
        CREATE COLUMN TABLE "{config.CHUNKS_TABLE}" (
            CHUNK_ID NVARCHAR(64) PRIMARY KEY,
            DOC_NAME NVARCHAR(100) NOT NULL,
            SECTION NVARCHAR(300),
            CHUNK_INDEX INTEGER,
            CHUNK_TEXT NCLOB,
            EMBEDDING REAL_VECTOR({config.EMBEDDING_DIM})
        )
    """,
    config.NODES_TABLE: f"""
        CREATE COLUMN TABLE "{config.NODES_TABLE}" (
            NODE_ID NVARCHAR(500) PRIMARY KEY,
            NODE_TYPE NVARCHAR(30) NOT NULL,
            LABEL NVARCHAR(1000) NOT NULL,
            PROPERTIES NCLOB,
            SOURCE NVARCHAR(100)
        )
    """,
    config.EDGES_TABLE: f"""
        CREATE COLUMN TABLE "{config.EDGES_TABLE}" (
            EDGE_ID INTEGER PRIMARY KEY,
            SOURCE_NODE NVARCHAR(500) NOT NULL,
            TARGET_NODE NVARCHAR(500) NOT NULL,
            EDGE_TYPE NVARCHAR(30) NOT NULL,
            PROPERTIES NCLOB
        )
    """,
}


def _table_exists(cur, table_name: str) -> bool:
    cur.execute(
        "SELECT COUNT(*) FROM SYS.TABLES WHERE SCHEMA_NAME = CURRENT_SCHEMA AND TABLE_NAME = ?",
        (table_name,),
    )
    return cur.fetchone()[0] > 0


def _workspace_exists(cur, workspace_name: str) -> bool:
    cur.execute(
        "SELECT COUNT(*) FROM SYS.GRAPH_WORKSPACES WHERE SCHEMA_NAME = CURRENT_SCHEMA AND WORKSPACE_NAME = ?",
        (workspace_name,),
    )
    return cur.fetchone()[0] > 0


def create_all(conn, drop_existing: bool = False) -> list[str]:
    """Create chunk/node/edge tables + graph workspace if not already present.
    Returns a list of human-readable actions taken, for the ingestion script's summary."""
    cur = conn.cursor()
    actions = []

    if drop_existing:
        try:
            cur.execute(f'DROP GRAPH WORKSPACE "{config.GRAPH_WORKSPACE}"')
            actions.append(f"Dropped existing graph workspace {config.GRAPH_WORKSPACE}")
        except Exception:
            pass
        for table_name in [config.EDGES_TABLE, config.NODES_TABLE, config.CHUNKS_TABLE]:
            try:
                cur.execute(f'DROP TABLE "{table_name}"')
                actions.append(f"Dropped existing table {table_name}")
            except Exception:
                pass

    for table_name, ddl in _TABLE_DDL.items():
        if _table_exists(cur, table_name):
            actions.append(f"Table {table_name} already exists, left as-is")
            continue
        cur.execute(ddl)
        actions.append(f"Created table {table_name}")

    if not _workspace_exists(cur, config.GRAPH_WORKSPACE):
        cur.execute(f"""
            CREATE GRAPH WORKSPACE "{config.GRAPH_WORKSPACE}"
                EDGE TABLE "{config.EDGES_TABLE}"
                    SOURCE COLUMN "SOURCE_NODE"
                    TARGET COLUMN "TARGET_NODE"
                    KEY COLUMN "EDGE_ID"
                VERTEX TABLE "{config.NODES_TABLE}"
                    KEY COLUMN "NODE_ID"
        """)
        actions.append(f"Created graph workspace {config.GRAPH_WORKSPACE}")
    else:
        actions.append(f"Graph workspace {config.GRAPH_WORKSPACE} already exists, left as-is")

    conn.commit()
    return actions


if __name__ == "__main__":
    from pipeline.ingestion.hana_source import get_connection

    conn = get_connection()
    try:
        for action in create_all(conn):
            print(f"  {action}")
    finally:
        conn.close()
