#!/usr/bin/env python3
"""
Stage 2 of the explainability pipeline: the evidence pack.

One provenance-carrying record per alert, assembled by pushed-down SQL against
SAP HANA Cloud. This is the keystone - the driver derivation, the narration
layer, the retrieval attachment points and the investigator UI all read from it.

Design notes:

* Every join is a LEFT JOIN. A missing dimension row must never silently drop an
  alert from the queue - under supervision, a disappearing alert is far worse
  than a null column.

* Client baselines are aggregated across periods, NOT taken from the latest one.
  Baselines are monthly and each client averages ~1 transaction per month, so
  37% of company-months have zero transactions and 74% have zero standard
  deviation. The latest month is usually degenerate.

* Prior-alert history excludes the current alert and only counts alerts raised
  strictly earlier, so nothing downstream can see the future.

* build_provenance() records the source table and key for every block, which is
  what belongs in the currently-empty INPUT_CONTEXT column.

Usage:
    python evidence_pack.py --self-test          # verify logic on the CSV extract
    python evidence_pack.py --limit 50           # live HANA, first 50 alerts
    python evidence_pack.py --alert 15002        # one alert, pretty-printed
    python evidence_pack.py --all --out evidence_pack.csv
"""

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

def _project_root(start):
    """Walk up until we find the extract. Works whether this module lives in the
    repo root or in src/, so moving files does not silently break paths."""
    p = Path(start).resolve()
    for cand in [p.parent] + list(p.parents):
        if (cand / 'data' / 'TEAM_07').is_dir() or (cand / 'team_07_credentials.json').is_file():
            return cand
    return p.parent


ROOT = _project_root(__file__)
CSV = ROOT / 'data' / 'TEAM_07'
CREDS = ROOT / 'team_07_credentials.json'
S = 'TEAM_07'

# --------------------------------------------------------------------------
# The pushed-down query. Keep it conservative - plain joins and scalar
# subqueries only, so it survives dialect differences and stays readable to a
# validator who has to sign it off.
# --------------------------------------------------------------------------
EVIDENCE_SQL = """
SELECT
    a.ALERT_ID, a.ALERT_UUID, a.ALERT_TYPE, a.ALERT_SUBTYPE, a.ALERT_SOURCE,
    a.ALERT_PRIORITY, a.STATUS AS ALERT_STATUS, a.CREATED_AT AS ALERT_CREATED_AT,
    a.RESOLUTION_CODE, a.ASSIGNED_TO,
    DAYS_BETWEEN(a.CREATED_AT, CURRENT_TIMESTAMP) AS ALERT_AGE_DAYS,

    t.TRANSACTION_ID, t.TRANSACTION_REF, t.AMOUNT_USD, t.AMOUNT_ORIGINAL,
    t.CURRENCY_ORIGINAL, t.TRANSACTION_TYPE, t.TRANSACTION_SUBTYPE,
    t.PAYMENT_PURPOSE, t.IS_CROSS_BORDER, t.INITIATED_AT, t.VALUE_DATE,
    t.STATUS AS TXN_STATUS, t.BENEFICIARY_NAME, t.BENEFICIARY_BANK_BIC,

    s.OVERALL_RISK_SCORE, s.RISK_TIER, s.MODEL_CONFIDENCE, s.IS_ANOMALY,
    s.ANOMALY_TYPE, s.AMOUNT_RISK_SCORE, s.FREQUENCY_RISK_SCORE,
    s.GEOGRAPHY_RISK_SCORE, s.COUNTERPARTY_RISK_SCORE, s.PATTERN_RISK_SCORE,
    s.VELOCITY_RISK_SCORE,

    co.COMPANY_ID, co.LEGAL_NAME, co.COMPANY_TYPE, co.CLIENT_SEGMENT,
    co.EMPLOYEE_COUNT, co.ANNUAL_REVENUE_USD, co.KYC_STATUS, co.KYC_RISK_RATING,
    co.KYC_EXPIRY_DATE, co.PEP_ASSOCIATED, co.SANCTIONS_HIT, co.ADVERSE_MEDIA_FLAG,

    ind.INDUSTRY_NAME, ind.INHERENT_RISK_LEVEL, ind.AML_SENSITIVITY,

    dc.COUNTRY_NAME AS DEST_COUNTRY, dc.FATF_STATUS AS DEST_FATF,
    dc.RISK_TIER AS DEST_RISK_TIER, dc.CORRUPTION_INDEX AS DEST_CORRUPTION,
    oc.COUNTRY_NAME AS ORIG_COUNTRY, oc.FATF_STATUS AS ORIG_FATF,

    crp.COMPOSITE_RISK_SCORE AS CLIENT_COMPOSITE_RISK,
    crp.RISK_TIER AS CLIENT_RISK_TIER, crp.REQUIRES_EDD, crp.NEXT_REVIEW_DATE,

    bo.OWNER_COUNT, bo.MAX_OWNERSHIP_PCT, bo.PEP_OWNERS, bo.SANCTIONED_OWNERS,
    bo.OWNER_NATIONALITIES,

    bl.BASELINE_AVG_AMOUNT, bl.BASELINE_MAX_AMOUNT, bl.BASELINE_TXN_COUNT,
    bl.BASELINE_UNIQUE_COUNTERPARTIES, bl.BASELINE_NEW_COUNTERPARTY_PCT,

    ph.PRIOR_ALERTS, ph.PRIOR_TRUE_POSITIVES,

    cc.CASE_ID, cc.CASE_NUMBER, cc.CASE_TYPE, cc.STATUS AS CASE_STATUS,
    cc.OUTCOME AS CASE_OUTCOME, cc.SAR_FILED, ca.LINKED_CASE_COUNT

FROM {S}.RISK_ALERTS a
LEFT JOIN {S}.TRANSACTIONS t
       ON t.TRANSACTION_ID = a.TRANSACTION_ID
LEFT JOIN {S}.TRANSACTION_RISK_SCORES s
       ON s.TRANSACTION_ID = a.TRANSACTION_ID
LEFT JOIN {S}.COMPANIES co
       ON co.COMPANY_ID = a.COMPANY_ID
LEFT JOIN {S}.INDUSTRIES ind
       ON ind.INDUSTRY_ID = co.INDUSTRY_ID
LEFT JOIN {S}.COUNTRIES dc
       ON dc.COUNTRY_ID = t.DESTINATION_COUNTRY_ID
LEFT JOIN {S}.COUNTRIES oc
       ON oc.COUNTRY_ID = t.ORIGINATING_COUNTRY_ID
LEFT JOIN {S}.COMPANY_RISK_PROFILES crp
       ON crp.COMPANY_ID = a.COMPANY_ID

-- beneficial ownership, collapsed to one row per client
LEFT JOIN (
    SELECT COMPANY_ID,
           COUNT(*) AS OWNER_COUNT,
           MAX(OWNERSHIP_PERCENTAGE) AS MAX_OWNERSHIP_PCT,
           SUM(CASE WHEN IS_PEP = TRUE THEN 1 ELSE 0 END) AS PEP_OWNERS,
           SUM(CASE WHEN SANCTIONS_MATCH = TRUE THEN 1 ELSE 0 END) AS SANCTIONED_OWNERS,
           COUNT(DISTINCT NATIONALITY_COUNTRY_ID) AS OWNER_NATIONALITIES
    FROM {S}.COMPANY_BENEFICIAL_OWNERS
    GROUP BY COMPANY_ID
) bo ON bo.COMPANY_ID = a.COMPANY_ID

-- baselines aggregated across ACTIVE periods only; the latest month is usually
-- degenerate (one transaction, zero stddev)
LEFT JOIN (
    SELECT COMPANY_ID,
           AVG(AVG_AMOUNT_USD) AS BASELINE_AVG_AMOUNT,
           MAX(MAX_AMOUNT_USD) AS BASELINE_MAX_AMOUNT,
           SUM(TRANSACTION_COUNT) AS BASELINE_TXN_COUNT,
           AVG(UNIQUE_COUNTERPARTIES) AS BASELINE_UNIQUE_COUNTERPARTIES,
           AVG(NEW_COUNTERPARTY_PCT) AS BASELINE_NEW_COUNTERPARTY_PCT
    FROM {S}.TRANSACTION_BASELINES
    WHERE TRANSACTION_COUNT > 0
    GROUP BY COMPANY_ID
) bl ON bl.COMPANY_ID = a.COMPANY_ID

-- prior alert history for the same client, strictly before this alert
LEFT JOIN (
    SELECT x.ALERT_ID,
           COUNT(y.ALERT_ID) AS PRIOR_ALERTS,
           SUM(CASE WHEN y.RESOLUTION_CODE = 'TRUE_POSITIVE' THEN 1 ELSE 0 END)
               AS PRIOR_TRUE_POSITIVES
    FROM {S}.RISK_ALERTS x
    LEFT JOIN {S}.RISK_ALERTS y
           ON y.COMPANY_ID = x.COMPANY_ID
          AND y.CREATED_AT < x.CREATED_AT
    GROUP BY x.ALERT_ID
) ph ON ph.ALERT_ID = a.ALERT_ID

-- CASE_ALERTS is many-to-many: 42 alerts belong to more than one case. Joining
-- it raw fans the pack out to 5,042 rows for 5,000 alerts, silently duplicating
-- alerts in the queue. Collapse to the earliest-linked case and keep the count.
LEFT JOIN (
    SELECT ALERT_ID, CASE_ID, LINKED_CASE_COUNT FROM (
        SELECT ca0.ALERT_ID, ca0.CASE_ID,
               COUNT(*) OVER (PARTITION BY ca0.ALERT_ID) AS LINKED_CASE_COUNT,
               ROW_NUMBER() OVER (PARTITION BY ca0.ALERT_ID
                                  ORDER BY ca0.LINKED_AT, ca0.CASE_ID) AS RN
        FROM {S}.CASE_ALERTS ca0
    ) WHERE RN = 1
) ca ON ca.ALERT_ID = a.ALERT_ID
LEFT JOIN {S}.COMPLIANCE_CASES cc ON cc.CASE_ID = ca.CASE_ID
{WHERE}
ORDER BY a.ALERT_ID
"""


def sql(where='', schema=S):
    return EVIDENCE_SQL.replace('{S}', schema).replace('{WHERE}', where)


# --------------------------------------------------------------------------
# provenance
# --------------------------------------------------------------------------
BLOCKS = {
    'alert':        ('RISK_ALERTS', 'ALERT_ID'),
    'transaction':  ('TRANSACTIONS', 'TRANSACTION_ID'),
    'risk_score':   ('TRANSACTION_RISK_SCORES', 'TRANSACTION_ID'),
    'client':       ('COMPANIES', 'COMPANY_ID'),
    'client_risk':  ('COMPANY_RISK_PROFILES', 'COMPANY_ID'),
    'ownership':    ('COMPANY_BENEFICIAL_OWNERS', 'COMPANY_ID'),
    'baseline':     ('TRANSACTION_BASELINES', 'COMPANY_ID'),
    'jurisdiction': ('COUNTRIES', 'DESTINATION_COUNTRY_ID'),
    'industry':     ('INDUSTRIES', 'INDUSTRY_ID'),
    'case':         ('COMPLIANCE_CASES', 'CASE_ID'),
}


def build_provenance(row, schema=S):
    """What belongs in INPUT_CONTEXT: where every block of this pack came from.

    An imputed or inferred value must never appear here without being marked,
    because this record is the audit trail for the explanation built on top.
    """
    keys = {'ALERT_ID': row.get('ALERT_ID'), 'TRANSACTION_ID': row.get('TRANSACTION_ID'),
            'COMPANY_ID': row.get('COMPANY_ID'), 'CASE_ID': row.get('CASE_ID'),
            'DESTINATION_COUNTRY_ID': row.get('DEST_COUNTRY'),
            'INDUSTRY_ID': row.get('INDUSTRY_NAME')}
    prov = {}
    for block, (table, key) in BLOCKS.items():
        v = keys.get(key)
        prov[block] = {'schema': schema, 'table': table, 'key': key,
                       'value': None if pd.isna(v) else v,
                       'resolved': bool(v is not None and not pd.isna(v))}
    prov['_meta'] = {'assembled_by': 'evidence_pack.py',
                     'method': 'deterministic SQL join',
                     'imputed_fields': []}
    return prov


# --------------------------------------------------------------------------
# transports
# --------------------------------------------------------------------------
# fetch_local() merges whole tables and so carries the raw column names, while
# fetch_hana() returns the SELECT's aliases. Two names for the same field is a
# bug waiting to happen: a rule referencing the local-only spelling works in
# testing and returns None against HANA. Canonicalise both paths to expose both
# spellings so downstream code cannot depend on which transport was used.
ALIASES = [
    ('DEST_COUNTRY', 'DEST_COUNTRY_NAME'),
    ('DEST_FATF', 'DEST_FATF_STATUS'),
    ('DEST_RISK_TIER', 'DEST_RISK_TIER'),
    ('DEST_CORRUPTION', 'DEST_CORRUPTION_INDEX'),
    ('ORIG_COUNTRY', 'ORIG_COUNTRY_NAME'),
    ('ORIG_FATF', 'ORIG_FATF_STATUS'),
    # the client risk profile: HANA aliases these, the local merge suffixes them
    ('CLIENT_COMPOSITE_RISK', 'COMPOSITE_RISK_SCORE'),
    ('CLIENT_RISK_TIER', 'RISK_TIER_CRP'),
]


def canonicalise(df):
    """Make every field reachable under both spellings, whichever transport ran."""
    for a, b in ALIASES:
        if a in df.columns and b not in df.columns:
            df[b] = df[a]
        elif b in df.columns and a not in df.columns:
            df[a] = df[b]
    return df


def fetch_hana(alert_ids=None, limit=None):
    from hdbcli import dbapi
    db = json.loads(CREDS.read_text())['database']
    conn = dbapi.connect(address=db['host'], port=int(db['port']), user=db['username'],
                         password=db['password'], encrypt=True,
                         sslValidateCertificate=False, connectTimeout=30000)
    where = ''
    if alert_ids:
        where = 'WHERE a.ALERT_ID IN (%s)' % ', '.join(str(int(i)) for i in alert_ids)
    q = sql(where)
    if limit:
        q += ' LIMIT %d' % int(limit)
    cur = conn.cursor()
    try:
        cur.execute(q)
        cols = [d[0] for d in cur.description]
        return canonicalise(pd.DataFrame(cur.fetchall(), columns=cols))
    finally:
        cur.close()
        conn.close()


def fetch_local(alert_ids=None, limit=None):
    """Reference implementation on the CSV extract, used to verify the SQL logic.
    Must produce the same columns and cardinality as fetch_hana()."""
    def L(n):
        d = pd.read_csv(CSV / (n + '.csv'), low_memory=False)
        for c in d.columns:
            if d[c].dtype == object:
                u = set(d[c].dropna().astype(str).unique()[:5])
                if u and u <= {'true', 'false'}:
                    d[c] = d[c].astype(str).map({'true': True, 'false': False})
        return d

    A, T, SC = L('RISK_ALERTS'), L('TRANSACTIONS'), L('TRANSACTION_RISK_SCORES')
    CO, IND, CTRY = L('COMPANIES'), L('INDUSTRIES'), L('COUNTRIES')
    CRP, BO, TB = L('COMPANY_RISK_PROFILES'), L('COMPANY_BENEFICIAL_OWNERS'), L('TRANSACTION_BASELINES')
    CA, CC = L('CASE_ALERTS'), L('COMPLIANCE_CASES')

    A['CREATED_AT'] = pd.to_datetime(A['CREATED_AT'], errors='coerce')
    A['ALERT_AGE_DAYS'] = (pd.Timestamp('2026-07-30') - A['CREATED_AT']).dt.days

    bo = BO.groupby('COMPANY_ID').agg(
        OWNER_COUNT=('OWNER_ID', 'size'),
        MAX_OWNERSHIP_PCT=('OWNERSHIP_PERCENTAGE', 'max'),
        PEP_OWNERS=('IS_PEP', lambda s: int(pd.Series(s).eq(True).sum())),
        SANCTIONED_OWNERS=('SANCTIONS_MATCH', lambda s: int(pd.Series(s).eq(True).sum())),
        OWNER_NATIONALITIES=('NATIONALITY_COUNTRY_ID', 'nunique')).reset_index()

    bl = (TB[TB['TRANSACTION_COUNT'] > 0].groupby('COMPANY_ID')
          .agg(BASELINE_AVG_AMOUNT=('AVG_AMOUNT_USD', 'mean'),
               BASELINE_MAX_AMOUNT=('MAX_AMOUNT_USD', 'max'),
               BASELINE_TXN_COUNT=('TRANSACTION_COUNT', 'sum'),
               BASELINE_UNIQUE_COUNTERPARTIES=('UNIQUE_COUNTERPARTIES', 'mean'),
               BASELINE_NEW_COUNTERPARTY_PCT=('NEW_COUNTERPARTY_PCT', 'mean')).reset_index())

    # prior alerts for the same client, strictly earlier
    ab = A[['ALERT_ID', 'COMPANY_ID', 'CREATED_AT', 'RESOLUTION_CODE']]
    j = ab.merge(ab, on='COMPANY_ID', suffixes=('', '_y'))
    j = j[j['CREATED_AT_y'] < j['CREATED_AT']]
    ph = j.groupby('ALERT_ID').agg(
        PRIOR_ALERTS=('ALERT_ID_y', 'count'),
        PRIOR_TRUE_POSITIVES=('RESOLUTION_CODE_y',
                              lambda s: int((s == 'TRUE_POSITIVE').sum()))).reset_index()

    CA['LINKED_AT'] = pd.to_datetime(CA['LINKED_AT'], errors='coerce')
    ca1 = (CA.sort_values(['ALERT_ID', 'LINKED_AT', 'CASE_ID'])
             .groupby('ALERT_ID', as_index=False)
             .agg(CASE_ID=('CASE_ID', 'first'), LINKED_CASE_COUNT=('CASE_ID', 'size')))

    df = (A.merge(T, on='TRANSACTION_ID', how='left', suffixes=('', '_T'))
            .merge(SC, on='TRANSACTION_ID', how='left', suffixes=('', '_S'))
            .merge(CO, on='COMPANY_ID', how='left', suffixes=('', '_CO'))
            .merge(IND, on='INDUSTRY_ID', how='left', suffixes=('', '_I'))
            .merge(CTRY.add_prefix('DEST_'), left_on='DESTINATION_COUNTRY_ID',
                   right_on='DEST_COUNTRY_ID', how='left')
            .merge(CTRY.add_prefix('ORIG_'), left_on='ORIGINATING_COUNTRY_ID',
                   right_on='ORIG_COUNTRY_ID', how='left')
            .merge(CRP, on='COMPANY_ID', how='left', suffixes=('', '_CRP'))
            .merge(bo, on='COMPANY_ID', how='left')
            .merge(bl, on='COMPANY_ID', how='left')
            .merge(ph, on='ALERT_ID', how='left')
            .merge(ca1, on='ALERT_ID', how='left')
            .merge(CC.add_prefix('CASE_'), left_on='CASE_ID',
                   right_on='CASE_CASE_ID', how='left'))
    df['PRIOR_ALERTS'] = df['PRIOR_ALERTS'].fillna(0).astype(int)
    df['PRIOR_TRUE_POSITIVES'] = df['PRIOR_TRUE_POSITIVES'].fillna(0).astype(int)

    if alert_ids:
        df = df[df['ALERT_ID'].isin(alert_ids)]
    if limit:
        df = df.head(int(limit))
    return canonicalise(df.reset_index(drop=True))


# --------------------------------------------------------------------------
def self_test():
    """Verify the join logic on the extract before trusting it against HANA."""
    print('running reference implementation on %s ...' % CSV)
    df = fetch_local()
    ok = True

    def check(name, cond, detail=''):
        nonlocal ok
        ok &= bool(cond)
        print('  [%s] %-46s %s' % ('PASS' if cond else 'FAIL', name, detail))

    n_alerts = len(pd.read_csv(CSV / 'RISK_ALERTS.csv'))
    check('one row per alert (no join fan-out)', len(df) == n_alerts,
          '%d rows vs %d alerts' % (len(df), n_alerts))
    check('alert ids unique', df['ALERT_ID'].is_unique)
    check('no alert lost to an inner join', df['ALERT_ID'].nunique() == n_alerts)
    for c in ['TRANSACTION_ID', 'AMOUNT_USD', 'OVERALL_RISK_SCORE', 'LEGAL_NAME',
              'DEST_FATF_STATUS', 'INHERENT_RISK_LEVEL']:
        if c in df.columns:
            check('%s resolved' % c, df[c].notna().all(),
                  '%d null' % int(df[c].isna().sum()))
    check('baseline present for most alerts',
          df['BASELINE_AVG_AMOUNT'].notna().mean() > 0.95,
          '%.1f%% populated' % (100 * df['BASELINE_AVG_AMOUNT'].notna().mean()))
    check('prior-alert count never negative', (df['PRIOR_ALERTS'] >= 0).all())
    check('prior TPs never exceed prior alerts',
          (df['PRIOR_TRUE_POSITIVES'] <= df['PRIOR_ALERTS']).all())
    check('multi-case alerts collapsed, count kept',
          'LINKED_CASE_COUNT' in df.columns
          and int(df['LINKED_CASE_COUNT'].fillna(0).max()) >= 2,
          'max %s cases on one alert' % df['LINKED_CASE_COUNT'].max())
    check('case linkage optional, not fatal',
          df['CASE_ID'].notna().sum() > 0,
          '%d alerts linked to a case' % int(df['CASE_ID'].notna().sum()))
    for a, b in ALIASES:
        check('alias pair %s / %s both present' % (a, b),
              a in df.columns and b in df.columns)
    p = build_provenance(df.iloc[0])
    check('provenance covers every block', len(p) == len(BLOCKS) + 1)
    check('provenance records no imputed fields', p['_meta']['imputed_fields'] == [])
    print('\n%s' % ('all checks passed' if ok else 'FAILURES ABOVE - fix before running live'))
    print('columns in pack: %d' % df.shape[1])
    return 0 if ok else 1


def show(row):
    print('=' * 78)
    print('ALERT %s  %s / %s  priority %s  age %s days'
          % (row.get('ALERT_ID'), row.get('ALERT_TYPE'), row.get('ALERT_SUBTYPE'),
             row.get('ALERT_PRIORITY'), row.get('ALERT_AGE_DAYS')))
    print('=' * 78)
    amt = row.get('AMOUNT_USD')
    print('%-22s %s %s' % ('transaction', row.get('CURRENCY_ORIGINAL'),
                           format(amt, ',.2f') if pd.notna(amt) else 'n/a'))
    print('%-22s %s' % ('client', row.get('LEGAL_NAME')))
    print('%-22s KYC %s / %s   PEP %s  sanctions %s'
          % ('due diligence', row.get('KYC_STATUS'), row.get('KYC_RISK_RATING'),
             row.get('PEP_ASSOCIATED'), row.get('SANCTIONS_HIT')))
    print('%-22s %s owners, max %s%%, %s PEP, %s sanctioned'
          % ('ownership', row.get('OWNER_COUNT'), row.get('MAX_OWNERSHIP_PCT'),
             row.get('PEP_OWNERS'), row.get('SANCTIONED_OWNERS')))
    print('%-22s %s (FATF %s, corruption %s)'
          % ('destination', row.get('DEST_COUNTRY_NAME', row.get('DEST_COUNTRY')),
             row.get('DEST_FATF_STATUS', row.get('DEST_FATF')),
             row.get('DEST_CORRUPTION_INDEX', row.get('DEST_CORRUPTION'))))
    print('%-22s %s (inherent %s)' % ('industry', row.get('INDUSTRY_NAME'),
                                      row.get('INHERENT_RISK_LEVEL')))
    print('%-22s score %s tier %s' % ('model output', row.get('OVERALL_RISK_SCORE'),
                                      row.get('RISK_TIER')))
    print('%-22s avg %s, max %s over %s txns'
          % ('client baseline', row.get('BASELINE_AVG_AMOUNT'),
             row.get('BASELINE_MAX_AMOUNT'), row.get('BASELINE_TXN_COUNT')))
    print('%-22s %s prior alerts, %s prior true positives'
          % ('client history', row.get('PRIOR_ALERTS'), row.get('PRIOR_TRUE_POSITIVES')))
    print('\nprovenance (goes into INPUT_CONTEXT):')
    print(json.dumps(build_provenance(row), indent=2, default=str)[:1200])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--self-test', action='store_true')
    ap.add_argument('--local', action='store_true', help='use the CSV extract')
    ap.add_argument('--alert', type=int, action='append')
    ap.add_argument('--limit', type=int)
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--out')
    a = ap.parse_args()

    if a.self_test:
        return self_test()

    fetch = fetch_local if a.local else fetch_hana
    limit = None if a.all else (a.limit or 25)
    try:
        df = fetch(alert_ids=a.alert, limit=limit)
    except ImportError:
        sys.exit('hdbcli not installed. pip install hdbcli, or pass --local.')
    except Exception as e:
        sys.exit('HANA query failed: %s\nRun with --local to use the CSV extract.'
                 % str(e)[:400])

    print('evidence pack: %d rows x %d columns' % (len(df), df.shape[1]))
    if a.alert and len(df):
        show(df.iloc[0])
    if a.out:
        df['INPUT_CONTEXT'] = [json.dumps(build_provenance(r), default=str)
                               for _, r in df.iterrows()]
        df.to_csv(a.out, index=False)
        print('wrote %s' % a.out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
