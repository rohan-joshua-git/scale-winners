"""Shared config for the GraphRAG grounding layer: AI Core deployment IDs discovered
live against the team-07 resource group, and HANA object names used by this stage.

Deployment IDs are specific to this hackathon tenant (team-07 resource group) --
see README for how they were found / how to redeploy if they're ever recreated.
"""
from pathlib import Path

CREDENTIALS_PATH = Path(__file__).resolve().parents[2] / "team_07_credentials.json"

AI_CORE_RESOURCE_GROUP = "team-07"

# Orchestration deployment (scenario "orchestration") -- proxies chat/completion
# calls to azure-openai, anthropic, gemini etc. Used for the LLM extraction pass.
ORCHESTRATION_DEPLOYMENT_ID = "d168cee75aadc761"
EXTRACTION_MODEL_NAME = "gpt-4o-mini"

# Embedding deployment (scenario "foundation-models", executable azure-openai,
# modelName text-embedding-3-small) -- created for this task since no embedding
# deployment previously existed in team-07. See README "AI Core setup" section.
EMBEDDING_DEPLOYMENT_ID = "d7f1244c7397cfe0"
EMBEDDING_MODEL_NAME = "text-embedding-3-small"
EMBEDDING_DIM = 1536

# HANA object names (created in the connecting user's own schema, TEAM_07_USER --
# that's the schema with DDL rights; TEAM_07 is read-only reference data)
CHUNKS_TABLE = "RAG_CHUNKS"
NODES_TABLE = "RAG_NODES"
EDGES_TABLE = "RAG_EDGES"
GRAPH_WORKSPACE = "RAG_KNOWLEDGE_GRAPH"

CHUNK_TARGET_CHARS = 1400
CHUNK_OVERLAP_CHARS = 150

REGULATORY_DOCS_DIR = Path(__file__).resolve().parents[2] / "data" / "regulatory_docs"
REGULATORY_DOCS = [
    "fatf_grey_list.txt",
    "fatf_black_list.txt",
    "ofac_sanctions_programs.txt",
    "eu_amld_sanctions.txt",
    "wolfsberg_guidance.txt",
    "mas_notice_626.txt",
]

# Ontology -- see task spec. Regulatory-doc extraction only ever produces the
# subset of node/edge types that can actually appear in regulatory text; the rest
# come from the operational-graph ETL step (ingest_operational_graph.py).
REGULATORY_NODE_TYPES = ["RegulatoryClause", "Jurisdiction", "SanctionsProgram"]
REGULATORY_EDGE_TYPES = ["CITES_CLAUSE", "SUBJECT_TO", "LISTED_UNDER"]

OPERATIONAL_NODE_TYPES = ["Alert", "Client", "BeneficialOwner", "Jurisdiction"]
OPERATIONAL_EDGE_TYPES = ["TRIGGERED_BY", "SHARES_OWNER_WITH", "SIMILAR_TO", "SUBJECT_TO"]

ALL_NODE_TYPES = ["Alert", "RiskDriver", "RegulatoryClause", "Client", "BeneficialOwner",
                  "Jurisdiction", "SanctionsProgram"]
ALL_EDGE_TYPES = ["TRIGGERED_BY", "CITES_CLAUSE", "SHARES_OWNER_WITH", "SUBJECT_TO",
                  "SIMILAR_TO", "LISTED_UNDER"]
