"""
Real SAP HANA Cloud reader. NOT a proxy -- this is the actual TrustSphere hackathon
tenant, connected via hdbcli using the credentials in team_07_credentials.json
(gitignored, never committed).

All source data for this prototype -- transactions, companies, beneficial owners,
risk scores, alerts, cases, and Joule-generated explanations -- is real data seeded
by the case organizers in the TEAM_07 schema. No synthetic transaction generation
is used; Phase 1 is purely a read path from HANA into local pandas/parquet for the
rest of the pipeline to consume.
"""
import json
import os
from pathlib import Path

import pandas as pd
from hdbcli import dbapi

CREDENTIALS_PATH = Path(__file__).resolve().parents[2] / "team_07_credentials.json"
CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache"

SCHEMA = "TEAM_07"

TABLES = [
    "TRANSACTIONS",
    "TRANSACTION_BASELINES",
    "TRANSACTION_RISK_SCORES",
    "COMPANIES",
    "COMPANY_BENEFICIAL_OWNERS",
    "COMPANY_RISK_PROFILES",
    "COMPLIANCE_CASES",
    "CASE_ALERTS",
    "RISK_ALERTS",
    "AUDIT_LOG",
    "JOULE_EXPLANATIONS",
    "COUNTRIES",
    "INDUSTRIES",
    "REGIONS",
    "SANCTIONS_LISTS",
    "SCREENING_RULES",
]


def _load_credentials() -> dict:
    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(
            f"{CREDENTIALS_PATH} not found. This file holds the real HANA/AI Core "
            "credentials for the hackathon tenant and is gitignored -- copy it "
            "back into the repo root if missing."
        )
    with open(CREDENTIALS_PATH) as f:
        return json.load(f)


def get_connection():
    creds = _load_credentials()
    db = creds["database"]
    return dbapi.connect(
        address=db["host"],
        port=db["port"],
        user=db["username"],
        password=db["password"],
        encrypt=True,
        sslValidateCertificate=False,
        sslCryptoProvider="openssl",
    )


def fetch_table(conn, table_name: str, schema: str = SCHEMA, limit: int | None = None) -> pd.DataFrame:
    query = f'SELECT * FROM "{schema}"."{table_name}"'
    if limit is not None:
        query += f" LIMIT {limit}"
    cur = conn.cursor()
    cur.execute(query)
    columns = [d[0] for d in cur.description]
    rows = cur.fetchall()
    return pd.DataFrame(rows, columns=columns)


def load_all(cache: bool = True, limit: int | None = None) -> dict[str, pd.DataFrame]:
    """Pull every real table from the TEAM_07 schema. Returns {table_name: DataFrame}.

    Caches each table as parquet under data/cache/ (gitignored) so downstream phases
    don't need a live HANA connection on every run.
    """
    conn = get_connection()
    frames = {}
    try:
        for table_name in TABLES:
            frames[table_name] = fetch_table(conn, table_name, limit=limit)
    finally:
        conn.close()

    if cache:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        for table_name, df in frames.items():
            df.to_parquet(CACHE_DIR / f"{table_name.lower()}.parquet", index=False)

    return frames


def load_cached() -> dict[str, pd.DataFrame]:
    """Load previously cached parquet tables without touching HANA."""
    frames = {}
    for table_name in TABLES:
        path = CACHE_DIR / f"{table_name.lower()}.parquet"
        if not path.exists():
            raise FileNotFoundError(f"No cache for {table_name} at {path}. Run load_all() first.")
        frames[table_name] = pd.read_parquet(path)
    return frames


if __name__ == "__main__":
    print("=" * 60)
    print("Phase 1 -- Real HANA ingestion (TEAM_07 schema)")
    print("=" * 60)
    frames = load_all(cache=True)
    for name, df in frames.items():
        print(f"  {name:30s} {len(df):>8,} rows  {len(df.columns):>3} cols")
    print(f"\nCached to {CACHE_DIR}")
