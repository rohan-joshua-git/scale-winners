#!/usr/bin/env python3
"""
TrustSphere AML explainability pipeline -- single entrypoint.

Each stage below already has its own tested script and CLI; this file just
sequences them behind one consistent command instead of five different
`python path/to/script.py ...` invocations to remember. It runs each stage as
a subprocess of the interpreter currently running main.py, so it is a thin
orchestrator, not a reimplementation -- the real logic always stays in the
stage's own file, and flags map 1:1 onto that file's existing argparse.

Stages:
  check         environment sanity check (data extract, credentials, deps)
  ingest        stage 1: pull TEAM_07 tables from HANA into a local cache
                (pipeline/ingestion/hana_source.py)
  sync-local    materialise data/TEAM_07/*.csv from data/cache/*.parquet, no
                HANA call -- what `evidence`/`drivers --local`/`rank` read
  evidence      stage 2: assemble / self-test the per-alert evidence pack
                (src/evidence_pack.py)
  drivers       stage 3: derive risk drivers, narratives, recommended actions
                (src/risk_drivers.py)
  rank          backlog prioritisation: score unresolved alerts by exposure
                (src/exposure_ranking.py)
  graph-ingest  stage 4 phase 1: build the GraphRAG grounding layer in HANA
                (pipeline/rag/ingest_regulatory.py, ingest_operational_graph.py)
  serve         stage 4 phase 3: run the GraphRAG grounding API
                (pipeline/rag/service.py, via uvicorn)
  explain       everything about one alert, stages 2+3, printed for a demo
                (src/risk_drivers.py --alert)
  all           the local, no-HANA-write happy path: evidence self-test ->
                drivers -> backlog ranking, all against the CSV extract

Examples:
    python main.py check
    python main.py all --limit 200
    python main.py explain 15002
    python main.py ingest
    python main.py drivers --hana --update-alerts
    python main.py rank --supervisory
    python main.py graph-ingest
    python main.py serve --port 8010

HANA / AI Core stages (ingest, drivers --hana, graph-ingest, serve) need
hdbcli (and, for graph-ingest, the AI Core SDK) importable under whichever
python runs this file. If more than one Python install exists on this
machine, invoke main.py with the one that has them -- `python main.py check`
tells you which packages are missing under the current interpreter.
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable


def run(args, cwd=ROOT):
    """Run a pipeline stage as a subprocess, streaming its output directly."""
    print('\n$ %s' % ' '.join(str(a) for a in args))
    result = subprocess.run(args, cwd=cwd)
    if result.returncode != 0:
        sys.exit(result.returncode)


# --------------------------------------------------------------------------
def cmd_check(a):
    print('root                 %s' % ROOT)
    print('python               %s' % PY)

    data_dir = ROOT / 'data' / 'TEAM_07'
    print('data extract         %s%s' % (data_dir,
          ' (found)' if data_dir.is_dir() else ' -- MISSING, --local commands will fail'))

    creds = ROOT / 'team_07_credentials.json'
    print('HANA credentials     %s%s' % (creds,
          ' (found)' if creds.is_file() else ' -- MISSING, HANA/AI Core commands will fail'))

    print('\ndependency check (under this interpreter):')
    for mod, needed_for in [
        ('pandas', 'stages 2-4 (core)'),
        ('numpy', 'stage 3 model decomposition, backlog ranking'),
        ('hdbcli', 'any --hana / ingest / graph-ingest command'),
        ('networkx', 'graph-ingest, serve (graph traversal)'),
        ('fastapi', 'serve'),
        ('uvicorn', 'serve'),
        ('ai_core_sdk', 'graph-ingest (AI Core deployment management)'),
    ]:
        try:
            __import__(mod)
            print('  [ok]      %-14s needed for %s' % (mod, needed_for))
        except ImportError:
            print('  [MISSING] %-14s needed for %s' % (mod, needed_for))


def cmd_ingest(a):
    run([PY, '-m', 'pipeline.ingestion.hana_source'])


# same 16 tables pipeline/ingestion/hana_source.py pulls from HANA
_TABLES = ['TRANSACTIONS', 'TRANSACTION_BASELINES', 'TRANSACTION_RISK_SCORES', 'COMPANIES',
           'COMPANY_BENEFICIAL_OWNERS', 'COMPANY_RISK_PROFILES', 'COMPLIANCE_CASES', 'CASE_ALERTS',
           'RISK_ALERTS', 'AUDIT_LOG', 'JOULE_EXPLANATIONS', 'COUNTRIES', 'INDUSTRIES', 'REGIONS',
           'SANCTIONS_LISTS', 'SCREENING_RULES']


def cmd_sync_local(a):
    """evidence_pack.py / exposure_ranking.py read per-table CSVs from data/TEAM_07/,
    a different location and format than the data/cache/*.parquet 'ingest' writes.
    This materialises one from the other with no HANA call, so --local commands
    (and stages with no HANA path at all, like `rank`) have something to read."""
    import pandas as pd
    cache_dir = ROOT / 'data' / 'cache'
    out_dir = ROOT / 'data' / 'TEAM_07'
    out_dir.mkdir(parents=True, exist_ok=True)
    missing = []
    for t in _TABLES:
        src = cache_dir / (t.lower() + '.parquet')
        if not src.is_file():
            missing.append(t)
            continue
        df = pd.read_parquet(src)
        dest = out_dir / (t + '.csv')
        df.to_csv(dest, index=False)
        print('  %-28s %8d rows -> %s' % (t, len(df), dest.relative_to(ROOT)))
    if missing:
        print('\nnot in data/cache, not converted: %s' % ', '.join(missing))
        print("run 'python main.py ingest' first to pull them from HANA.")


def cmd_evidence(a):
    args = [PY, str(ROOT / 'src' / 'evidence_pack.py')]
    if a.self_test:
        args.append('--self-test')
        run(args)
        return
    if a.source == 'local':
        args.append('--local')
    if a.alert:
        for x in a.alert:
            args += ['--alert', str(x)]
    if a.limit:
        args += ['--limit', str(a.limit)]
    if a.all:
        args.append('--all')
    if a.out:
        args += ['--out', a.out]
    run(args)


def cmd_drivers(a):
    args = [PY, str(ROOT / 'src' / 'risk_drivers.py')]
    if a.local:
        args.append('--local')
    if a.hana:
        args.append('--hana')
    if a.update_alerts:
        args.append('--update-alerts')
    if a.replace:
        args.append('--replace')
    if a.alert:
        args += ['--alert', str(a.alert)]
    if a.limit:
        args += ['--limit', str(a.limit)]
    run(args)


def cmd_rank(a):
    args = [PY, str(ROOT / 'src' / 'exposure_ranking.py')]
    if a.supervisory:
        args.append('--supervisory')
    if a.out:
        args += ['--out', a.out]
    run(args)


def cmd_graph_ingest(a):
    if not a.operational_only:
        run([PY, '-m', 'pipeline.rag.ingest_regulatory'])
    if not a.regulatory_only:
        run([PY, '-m', 'pipeline.rag.ingest_operational_graph'])


def cmd_serve(a):
    args = [PY, '-m', 'uvicorn', 'pipeline.rag.service:app',
            '--host', a.host, '--port', str(a.port)]
    if a.reload:
        args.append('--reload')
    run(args)


def cmd_explain(a):
    args = [PY, str(ROOT / 'src' / 'risk_drivers.py'), '--alert', str(a.alert_id)]
    args.append('--hana' if a.hana else '--local')
    run(args)


def cmd_all(a):
    print('=' * 70)
    print('STAGE 2 -- evidence pack self-test')
    print('=' * 70)
    run([PY, str(ROOT / 'src' / 'evidence_pack.py'), '--self-test'])

    print('\n' + '=' * 70)
    print('STAGE 3 -- risk drivers (local extract, writes CSV)')
    print('=' * 70)
    args = [PY, str(ROOT / 'src' / 'risk_drivers.py'), '--local']
    if a.limit:
        args += ['--limit', str(a.limit)]
    run(args)

    print('\n' + '=' * 70)
    print('BACKLOG -- exposure ranking')
    print('=' * 70)
    run([PY, str(ROOT / 'src' / 'exposure_ranking.py')])

    print('\nAll local stages complete.')
    print("Next: 'python main.py ingest' / 'graph-ingest' for the live HANA "
          "stages, or 'python main.py serve' for the grounding API.")


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description='TrustSphere AML explainability pipeline.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    sub = ap.add_subparsers(dest='command', required=True)

    p = sub.add_parser('check', help='environment sanity check (data, credentials, deps)')
    p.set_defaults(func=cmd_check)

    p = sub.add_parser('ingest', help='stage 1: pull TEAM_07 tables from HANA into a local cache')
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser('sync-local', help='materialise data/TEAM_07/*.csv from data/cache/*.parquet (no HANA call)')
    p.set_defaults(func=cmd_sync_local)

    p = sub.add_parser('evidence', help='stage 2: assemble / self-test the evidence pack')
    p.add_argument('--source', choices=['hana', 'local'], default='local',
                   help='read from the CSV extract or live HANA (default: local)')
    p.add_argument('--self-test', action='store_true', help='verify join logic on the extract')
    p.add_argument('--alert', type=int, action='append', help='repeatable')
    p.add_argument('--limit', type=int)
    p.add_argument('--all', action='store_true')
    p.add_argument('--out')
    p.set_defaults(func=cmd_evidence)

    p = sub.add_parser('drivers', help='stage 3: derive risk drivers + narratives, optionally persist')
    p.add_argument('--local', action='store_true', help='read from the CSV extract (default if --hana absent)')
    p.add_argument('--hana', action='store_true', help='read from and write to HANA')
    p.add_argument('--update-alerts', action='store_true',
                   help='also fill RISK_ALERTS.RISK_DRIVERS (snapshots originals first)')
    p.add_argument('--replace', action='store_true', help='drop and recreate the explanations table')
    p.add_argument('--alert', type=int)
    p.add_argument('--limit', type=int)
    p.set_defaults(func=cmd_drivers)

    p = sub.add_parser('rank', help='score the unresolved backlog by regulatory exposure')
    p.add_argument('--supervisory', action='store_true',
                   help='add the EMEA/APAC supervised-jurisdiction factor')
    p.add_argument('--out')
    p.set_defaults(func=cmd_rank)

    p = sub.add_parser('graph-ingest', help='stage 4 phase 1: build the GraphRAG layer in HANA')
    p.add_argument('--regulatory-only', action='store_true', help='skip the operational graph ETL')
    p.add_argument('--operational-only', action='store_true', help='skip regulatory document ingestion')
    p.set_defaults(func=cmd_graph_ingest)

    p = sub.add_parser('serve', help='stage 4 phase 3: run the GraphRAG grounding API')
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=8010)
    p.add_argument('--reload', action='store_true')
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser('explain', help='everything about one alert, stages 2+3, printed')
    p.add_argument('alert_id', type=int)
    p.add_argument('--hana', action='store_true', help='read this alert from live HANA instead of the extract')
    p.set_defaults(func=cmd_explain)

    p = sub.add_parser('all', help='local happy path: evidence self-test -> drivers -> ranking')
    p.add_argument('--limit', type=int, help='cap the number of alerts processed by stage 3')
    p.set_defaults(func=cmd_all)

    a = ap.parse_args()
    a.func(a)


if __name__ == '__main__':
    main()
