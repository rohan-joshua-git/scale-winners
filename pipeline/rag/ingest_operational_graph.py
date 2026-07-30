"""Phase 1 addition -- operational graph ETL: deterministic (non-LLM) mapping
from the already-cached operational tables (data/cache/*.parquet, pulled by
pipeline/ingestion/hana_source.py) into Client/BeneficialOwner/Alert/Jurisdiction
nodes and TRIGGERED_BY/SHARES_OWNER_WITH/SIMILAR_TO/LISTED_UNDER edges.

Why this exists: the 6 regulatory documents can only ever produce RegulatoryClause/
Jurisdiction/SanctionsProgram nodes (see extract.py) -- they don't mention specific
clients or alerts. Without this step, Phase 2's "shared beneficial owners across
cases" and "similar prior alerts" traversal would have nothing to traverse. Not
part of the LLM extraction pass -- this is straight relational-to-graph mapping,
which is more reliable than asking an LLM to re-derive facts already structured.

Two small additions beyond the task's 6 named edge types, both flagged in the
README:
- OWNS (BeneficialOwner -> Client): needed so BeneficialOwner nodes connect to
  anything at all; SHARES_OWNER_WITH is then the derived Client<->Client shortcut
  edge for the common 1-hop case.
- SUBJECT_TO is reused for BeneficialOwner -> Jurisdiction (by residence country),
  in addition to its original Jurisdiction -> RegulatoryClause use -- a beneficial
  owner is "subject to" their residence jurisdiction's regulatory regime.

SIMILAR_TO between alerts is derived categorically (same ALERT_TYPE + ALERT_SUBTYPE,
linked to each alert's most recent 3 same-type predecessors) rather than via
embedding similarity -- cheap, deterministic, and avoids 5,000 extra AI Core calls
for a hackathon prototype. README notes the embedding-based upgrade path.
"""
from __future__ import annotations

import time
from collections import defaultdict

import pandas as pd

from pipeline.ingestion.hana_source import get_connection, load_cached
from pipeline.rag import store
from pipeline.rag.dedup import canonical_label, canonical_node_id
from pipeline.rag.store import EdgeRecord, NodeRecord

SOURCE_TAG = "operational_graph"


def _client_node_id(company_id) -> str:
    return f"Client:{company_id}"


def _owner_node_id(owner_name: str) -> str:
    return f"BeneficialOwner:{canonical_label(owner_name).strip().casefold()}"


def _alert_node_id(alert_id) -> str:
    return f"Alert:{alert_id}"


def build_jurisdiction_nodes(countries: pd.DataFrame) -> tuple[list[NodeRecord], dict[int, str]]:
    nodes = []
    country_id_to_node_id: dict[int, str] = {}
    for _, row in countries.iterrows():
        node_id = canonical_node_id("Jurisdiction", row["COUNTRY_NAME"])
        country_id_to_node_id[row["COUNTRY_ID"]] = node_id
        nodes.append(NodeRecord(
            node_id=node_id, node_type="Jurisdiction", label=canonical_label(row["COUNTRY_NAME"]),
            properties={
                "country_code": row["COUNTRY_CODE"],
                "fatf_status": row["FATF_STATUS"],
                "sanctions_list": bool(row["SANCTIONS_LIST"]),
                "risk_tier": row["RISK_TIER"],
            },
            source=SOURCE_TAG,
        ))
    return nodes, country_id_to_node_id


def build_sanctions_program_nodes(sanctions_lists: pd.DataFrame) -> tuple[list[NodeRecord], list[EdgeRecord]]:
    """SanctionsProgram nodes only -- deliberately no LISTED_UNDER edges from
    SANCTIONS_LISTS.COUNTRY_ID. That column is the nationality/registration
    country of an individually-designated person, not "this country is under
    this program" -- treating it as the latter produced a near-complete bipartite
    graph (almost every jurisdiction connected to almost every program), which
    buried the one LISTED_UNDER signal that's actually real: FATF grey/black
    list membership, extracted from the regulatory text itself. If per-country
    program membership is needed later, derive it from COUNTRIES.SANCTIONS_LIST /
    FATF_STATUS instead of this table.
    """
    programs = sanctions_lists[["LIST_SOURCE", "PROGRAM"]].drop_duplicates()
    nodes = []
    for _, row in programs.iterrows():
        label = f"{row['LIST_SOURCE']} {row['PROGRAM']}"
        node_id = canonical_node_id("SanctionsProgram", label)
        nodes.append(NodeRecord(
            node_id=node_id, node_type="SanctionsProgram", label=label,
            properties={"list_source": row["LIST_SOURCE"], "program": row["PROGRAM"]},
            source=SOURCE_TAG,
        ))
    return nodes, []


def build_client_nodes(companies: pd.DataFrame) -> list[NodeRecord]:
    nodes = []
    for _, row in companies.iterrows():
        nodes.append(NodeRecord(
            node_id=_client_node_id(row["COMPANY_ID"]), node_type="Client",
            label=row["LEGAL_NAME"],
            properties={
                "company_id": int(row["COMPANY_ID"]),
                "company_type": row["COMPANY_TYPE"],
                "kyc_risk_rating": row["KYC_RISK_RATING"],
                "kyc_status": row["KYC_STATUS"],
                "sanctions_hit": bool(row["SANCTIONS_HIT"]),
                "pep_associated": bool(row["PEP_ASSOCIATED"]),
                "incorporation_country_id": int(row["INCORPORATION_COUNTRY_ID"]) if pd.notna(row["INCORPORATION_COUNTRY_ID"]) else None,
                "headquarters_country_id": int(row["HEADQUARTERS_COUNTRY_ID"]) if pd.notna(row["HEADQUARTERS_COUNTRY_ID"]) else None,
            },
            source=SOURCE_TAG,
        ))
    return nodes


def build_owner_nodes_and_edges(owners: pd.DataFrame,
                                 country_id_to_node_id: dict[int, str]) -> tuple[list[NodeRecord], list[EdgeRecord]]:
    nodes_by_id: dict[str, NodeRecord] = {}
    owns_edges = []
    subject_to_edges = []
    shares_owner_edges = []

    company_ids_by_owner: dict[str, set] = defaultdict(set)

    for _, row in owners.iterrows():
        owner_node_id = _owner_node_id(row["OWNER_NAME"])
        client_node_id = _client_node_id(row["COMPANY_ID"])
        company_ids_by_owner[owner_node_id].add(row["COMPANY_ID"])

        if owner_node_id not in nodes_by_id:
            nodes_by_id[owner_node_id] = NodeRecord(
                node_id=owner_node_id, node_type="BeneficialOwner",
                label=canonical_label(row["OWNER_NAME"]),
                properties={
                    "is_pep": bool(row["IS_PEP"]),
                    "sanctions_match": bool(row["SANCTIONS_MATCH"]),
                    "nationality_country_id": int(row["NATIONALITY_COUNTRY_ID"]) if pd.notna(row["NATIONALITY_COUNTRY_ID"]) else None,
                    "residence_country_id": int(row["RESIDENCE_COUNTRY_ID"]) if pd.notna(row["RESIDENCE_COUNTRY_ID"]) else None,
                },
                source=SOURCE_TAG,
            )
            residence_jurisdiction = country_id_to_node_id.get(row["RESIDENCE_COUNTRY_ID"])
            if residence_jurisdiction:
                subject_to_edges.append(EdgeRecord(
                    source_node=owner_node_id, target_node=residence_jurisdiction,
                    edge_type="SUBJECT_TO", properties={"basis": "residence"},
                ))

        owns_edges.append(EdgeRecord(
            source_node=owner_node_id, target_node=client_node_id, edge_type="OWNS",
            properties={"ownership_percentage": float(row["OWNERSHIP_PERCENTAGE"]) if pd.notna(row["OWNERSHIP_PERCENTAGE"]) else None},
        ))

    for owner_node_id, company_ids in company_ids_by_owner.items():
        if len(company_ids) < 2:
            continue
        company_ids = sorted(company_ids)
        for i in range(len(company_ids)):
            for j in range(i + 1, len(company_ids)):
                shares_owner_edges.append(EdgeRecord(
                    source_node=_client_node_id(company_ids[i]), target_node=_client_node_id(company_ids[j]),
                    edge_type="SHARES_OWNER_WITH", properties={"via_owner": owner_node_id},
                ))
                shares_owner_edges.append(EdgeRecord(
                    source_node=_client_node_id(company_ids[j]), target_node=_client_node_id(company_ids[i]),
                    edge_type="SHARES_OWNER_WITH", properties={"via_owner": owner_node_id},
                ))

    return list(nodes_by_id.values()), owns_edges + subject_to_edges + shares_owner_edges


def build_alert_nodes_and_edges(alerts: pd.DataFrame, max_similar: int = 3) -> tuple[list[NodeRecord], list[EdgeRecord]]:
    nodes = []
    triggered_by_edges = []
    for _, row in alerts.iterrows():
        alert_node_id = _alert_node_id(row["ALERT_ID"])
        nodes.append(NodeRecord(
            node_id=alert_node_id, node_type="Alert", label=row["ALERT_TITLE"],
            properties={
                "alert_type": row["ALERT_TYPE"],
                "alert_subtype": row["ALERT_SUBTYPE"],
                "priority": row["ALERT_PRIORITY"],
                "status": row["STATUS"],
                "description": row["ALERT_DESCRIPTION"],
                "company_id": int(row["COMPANY_ID"]) if pd.notna(row["COMPANY_ID"]) else None,
            },
            source=SOURCE_TAG,
        ))
        if pd.notna(row["COMPANY_ID"]):
            triggered_by_edges.append(EdgeRecord(
                source_node=alert_node_id, target_node=_client_node_id(row["COMPANY_ID"]),
                edge_type="TRIGGERED_BY", properties={},
            ))

    similar_to_edges = []
    sorted_alerts = alerts.sort_values("CREATED_AT")
    for (_, _), group in sorted_alerts.groupby(["ALERT_TYPE", "ALERT_SUBTYPE"]):
        ids = group["ALERT_ID"].tolist()
        for i, alert_id in enumerate(ids):
            for prior_id in ids[max(0, i - max_similar):i]:
                similar_to_edges.append(EdgeRecord(
                    source_node=_alert_node_id(alert_id), target_node=_alert_node_id(prior_id),
                    edge_type="SIMILAR_TO", properties={"basis": "same alert_type + alert_subtype"},
                ))
                similar_to_edges.append(EdgeRecord(
                    source_node=_alert_node_id(prior_id), target_node=_alert_node_id(alert_id),
                    edge_type="SIMILAR_TO", properties={"basis": "same alert_type + alert_subtype"},
                ))

    return nodes, triggered_by_edges + similar_to_edges


def main():
    conn = get_connection()
    try:
        print("Loading cached operational tables...")
        tables = load_cached()
        countries = tables["COUNTRIES"]
        sanctions_lists = tables["SANCTIONS_LISTS"]
        companies = tables["COMPANIES"]
        owners = tables["COMPANY_BENEFICIAL_OWNERS"]
        alerts = tables["RISK_ALERTS"]

        print(f"Clearing prior operational_graph rows (source='{SOURCE_TAG}')...")
        store.delete_nodes_by_source(conn, SOURCE_TAG)

        start = time.time()

        print("Building Jurisdiction nodes...")
        jurisdiction_nodes, country_id_to_node_id = build_jurisdiction_nodes(countries)
        n_jur = store.bulk_insert_nodes(conn, jurisdiction_nodes)

        print("Building SanctionsProgram nodes (no derived LISTED_UNDER edges -- see docstring)...")
        program_nodes, listed_under_edges = build_sanctions_program_nodes(sanctions_lists)
        n_prog = store.bulk_insert_nodes(conn, program_nodes)
        n_listed = store.bulk_insert_edges(conn, listed_under_edges)

        print("Building Client nodes...")
        client_nodes = build_client_nodes(companies)
        n_client = store.bulk_insert_nodes(conn, client_nodes)

        print("Building BeneficialOwner nodes + OWNS/SUBJECT_TO/SHARES_OWNER_WITH edges...")
        owner_nodes, owner_edges = build_owner_nodes_and_edges(owners, country_id_to_node_id)
        n_owner = store.bulk_insert_nodes(conn, owner_nodes)
        n_owner_edges = store.bulk_insert_edges(conn, owner_edges)

        print("Building Alert nodes + TRIGGERED_BY/SIMILAR_TO edges...")
        alert_nodes, alert_edges = build_alert_nodes_and_edges(alerts)
        n_alert = store.bulk_insert_nodes(conn, alert_nodes)
        n_alert_edges = store.bulk_insert_edges(conn, alert_edges)

        conn.commit()
        elapsed = time.time() - start

        print("\n" + "=" * 60)
        print("Phase 1 -- Operational Graph ETL Summary")
        print("=" * 60)
        print(f"  Jurisdiction nodes:      {n_jur}")
        print(f"  SanctionsProgram nodes:  {n_prog}")
        print(f"  Client nodes:            {n_client}")
        print(f"  BeneficialOwner nodes:   {n_owner}")
        print(f"  Alert nodes:             {n_alert}")
        print(f"  LISTED_UNDER edges:      {n_listed}")
        print(f"  OWNS/SUBJECT_TO/SHARES_OWNER_WITH edges: {n_owner_edges}")
        print(f"  TRIGGERED_BY/SIMILAR_TO edges:           {n_alert_edges}")
        print(f"  Elapsed: {elapsed:.1f}s")

        totals = store.counts(conn)
        print(f"\nGrand totals in HANA: {totals[store.config.NODES_TABLE]} nodes, "
              f"{totals[store.config.EDGES_TABLE]} edges")
        print(f"Nodes by type: {totals['nodes_by_type']}")
        print(f"Edges by type: {totals['edges_by_type']}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
