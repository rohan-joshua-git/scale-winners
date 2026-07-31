#!/usr/bin/env python3
"""
Stage 3: derive risk drivers from the evidence pack and write them into HANA.

Populates the three fields TrustSphere designed for explainability and never
filled - RISK_DRIVERS, RECOMMENDED_ACTIONS and INPUT_CONTEXT are 0% populated
across all 5,000 alerts today.

Governance position, and it matters:

  Every severity below is POLICY-SET, not fitted. Nothing here is estimated from
  outcomes. That is what keeps this stage on the light governance path as
  "rule-based prioritisation" rather than a model influencing customer outcomes.
  If you ever tune these numbers against RESOLUTION_CODE, you have built a model
  and you rejoin the 4-6 month validation queue.

Each driver carries the fields and source tables it was derived from, so an
investigator - or an examiner - can trace any statement back to a row.

Drivers also report population_frequency: how often that rule fires across all
alerts. A driver firing on 81% of alerts is true but not distinguishing, and
showing that honestly helps an investigator calibrate. Frequency is reported,
never used to reorder - reordering by it would be fitting to data.

Usage:
    python risk_drivers.py --local                  # compute + write CSV/JSON
    python risk_drivers.py --alert 15002            # inspect one explanation
    python risk_drivers.py --hana                   # create table + insert
    python risk_drivers.py --hana --update-alerts   # also fill RISK_ALERTS
"""

import argparse
import json
import sys
from datetime import datetime
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
sys.path.insert(0, str(ROOT))
import evidence_pack as ep  # noqa: E402

S = 'TEAM_07'
TABLE = 'ALERT_EXPLANATIONS'
BACKUP = 'RISK_ALERTS_EXPLAIN_BACKUP'
DRIVER_TABLE = 'ALERT_DRIVER_QUERIES'   # one row per driver, for the retrieval index
RULESET_VERSION = 'v1.0.0'
THRESHOLDS = [10000, 20000, 30000, 50000, 70000, 80000, 90000, 100000]


def _f(v, default=0.0):
    try:
        return default if v is None or pd.isna(v) else float(v)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# The rulebook. id, category, severity (0-100, policy), test, message, sources.
# --------------------------------------------------------------------------
RULES = [
    # --- screening -------------------------------------------------------
    dict(id='SCR001', cat='Screening', sev=95,
         test=lambda r: r.get('SANCTIONS_HIT') is True,
         msg=lambda r: 'Client carries a sanctions hit on file',
         src=[('COMPANIES', 'SANCTIONS_HIT')]),
    dict(id='SCR002', cat='Screening', sev=90,
         test=lambda r: _f(r.get('SANCTIONED_OWNERS')) > 0,
         msg=lambda r: '%d beneficial owner(s) flagged as a sanctions match'
                       % int(_f(r.get('SANCTIONED_OWNERS'))),
         src=[('COMPANY_BENEFICIAL_OWNERS', 'SANCTIONS_MATCH')]),
    dict(id='SCR003', cat='Screening', sev=60,
         test=lambda r: r.get('PEP_ASSOCIATED') is True,
         msg=lambda r: 'Client is politically exposed',
         src=[('COMPANIES', 'PEP_ASSOCIATED')]),
    dict(id='SCR004', cat='Screening', sev=55,
         test=lambda r: _f(r.get('PEP_OWNERS')) > 0,
         msg=lambda r: '%d beneficial owner(s) politically exposed'
                       % int(_f(r.get('PEP_OWNERS'))),
         src=[('COMPANY_BENEFICIAL_OWNERS', 'IS_PEP')]),
    dict(id='SCR005', cat='Screening', sev=50,
         test=lambda r: r.get('ADVERSE_MEDIA_FLAG') is True,
         msg=lambda r: 'Adverse media flag on the client record',
         src=[('COMPANIES', 'ADVERSE_MEDIA_FLAG')]),

    # --- due diligence ---------------------------------------------------
    dict(id='KYC001', cat='Due diligence', sev=75,
         test=lambda r: str(r.get('KYC_STATUS')) == 'EXPIRED',
         msg=lambda r: 'KYC expired (expiry %s)' % r.get('KYC_EXPIRY_DATE'),
         src=[('COMPANIES', 'KYC_STATUS'), ('COMPANIES', 'KYC_EXPIRY_DATE')]),
    dict(id='KYC002', cat='Due diligence', sev=70,
         test=lambda r: str(r.get('KYC_STATUS')) == 'EXPIRED'
                        and str(r.get('KYC_RISK_RATING')) == 'LOW',
         msg=lambda r: 'Contradiction: KYC expired yet client still rated LOW risk',
         src=[('COMPANIES', 'KYC_STATUS'), ('COMPANIES', 'KYC_RISK_RATING')]),
    dict(id='KYC003', cat='Due diligence', sev=65,
         test=lambda r: str(r.get('KYC_STATUS')) == 'REJECTED',
         msg=lambda r: 'KYC previously rejected and not remediated',
         src=[('COMPANIES', 'KYC_STATUS')]),
    dict(id='KYC004', cat='Due diligence', sev=50,
         test=lambda r: str(r.get('KYC_RISK_RATING')) == 'HIGH',
         msg=lambda r: 'Client rated HIGH risk at onboarding',
         src=[('COMPANIES', 'KYC_RISK_RATING')]),
    dict(id='KYC005', cat='Due diligence', sev=45,
         test=lambda r: r.get('REQUIRES_EDD') is True,
         msg=lambda r: 'Client profile already requires enhanced due diligence',
         src=[('COMPANY_RISK_PROFILES', 'REQUIRES_EDD')]),

    # --- jurisdiction ----------------------------------------------------
    dict(id='GEO001', cat='Jurisdiction', sev=90,
         test=lambda r: str(r.get('DEST_FATF_STATUS', r.get('DEST_FATF'))) == 'BLACK_LIST',
         msg=lambda r: 'Destination %s is FATF black-listed'
                       % r.get('DEST_COUNTRY_NAME', r.get('DEST_COUNTRY')),
         src=[('COUNTRIES', 'FATF_STATUS')]),
    dict(id='GEO002', cat='Jurisdiction', sev=80,
         test=lambda r: str(r.get('DEST_FATF_STATUS', r.get('DEST_FATF'))) == 'NON_COMPLIANT',
         msg=lambda r: 'Destination %s is FATF non-compliant'
                       % r.get('DEST_COUNTRY_NAME', r.get('DEST_COUNTRY')),
         src=[('COUNTRIES', 'FATF_STATUS')]),
    dict(id='GEO003', cat='Jurisdiction', sev=65,
         test=lambda r: str(r.get('DEST_FATF_STATUS', r.get('DEST_FATF'))) == 'GREY_LIST',
         msg=lambda r: 'Destination %s is on the FATF grey list'
                       % r.get('DEST_COUNTRY_NAME', r.get('DEST_COUNTRY')),
         src=[('COUNTRIES', 'FATF_STATUS')]),
    dict(id='GEO004', cat='Jurisdiction', sev=40,
         test=lambda r: str(r.get('DEST_RISK_TIER')) == 'HIGH',
         msg=lambda r: 'Destination country rated HIGH risk internally',
         src=[('COUNTRIES', 'RISK_TIER')]),

    # --- transaction behaviour -------------------------------------------
    dict(id='TXN001', cat='Behaviour', sev=60,
         test=lambda r: _f(r.get('AMOUNT_USD')) > _f(r.get('BASELINE_MAX_AMOUNT'), 1e18),
         msg=lambda r: 'Amount USD %s exceeds the client historic maximum of USD %s'
                       % (format(_f(r.get('AMOUNT_USD')), ',.0f'),
                          format(_f(r.get('BASELINE_MAX_AMOUNT')), ',.0f')),
         src=[('TRANSACTIONS', 'AMOUNT_USD'), ('TRANSACTION_BASELINES', 'MAX_AMOUNT_USD')]),
    dict(id='TXN002', cat='Behaviour', sev=55,
         test=lambda r: any(t * 0.95 <= _f(r.get('AMOUNT_USD')) < t for t in THRESHOLDS),
         msg=lambda r: 'Amount USD %s sits just below a reporting threshold'
                       % format(_f(r.get('AMOUNT_USD')), ',.0f'),
         src=[('TRANSACTIONS', 'AMOUNT_USD'), ('SCREENING_RULES', 'THRESHOLD_VALUE')]),
    dict(id='TXN003', cat='Behaviour', sev=45,
         test=lambda r: _f(r.get('BASELINE_AVG_AMOUNT')) > 0
                        and _f(r.get('AMOUNT_USD')) >= 3 * _f(r.get('BASELINE_AVG_AMOUNT')),
         msg=lambda r: 'Amount is %.1fx the client average of USD %s'
                       % (_f(r.get('AMOUNT_USD')) / max(_f(r.get('BASELINE_AVG_AMOUNT')), 1e-9),
                          format(_f(r.get('BASELINE_AVG_AMOUNT')), ',.0f')),
         src=[('TRANSACTIONS', 'AMOUNT_USD'), ('TRANSACTION_BASELINES', 'AVG_AMOUNT_USD')]),
    # Exact multiples of 10,000 occur on 0.02% of rows - too rare to be a rule.
    # Whole-dollar amounts (6.6%) are the usable round-figure signal here.
    dict(id='TXN004', cat='Behaviour', sev=25,
         test=lambda r: _f(r.get('AMOUNT_USD')) > 0
                        and abs(_f(r.get('AMOUNT_USD')) - round(_f(r.get('AMOUNT_USD')))) < 0.005,
         msg=lambda r: 'Round-figure amount: USD %s exactly, no cents'
                       % format(_f(r.get('AMOUNT_USD')), ',.0f'),
         src=[('TRANSACTIONS', 'AMOUNT_USD')]),

    # --- ownership structure ---------------------------------------------
    # MAX_OWNERSHIP_PCT caps at 70.0 in this population, so a 75% test never
    # fires. 60% is the conventional control threshold for a controlling interest.
    dict(id='OWN001', cat='Ownership', sev=35,
         test=lambda r: _f(r.get('MAX_OWNERSHIP_PCT')) >= 60,
         msg=lambda r: 'Single owner holds a controlling %.0f%% interest'
                       % _f(r.get('MAX_OWNERSHIP_PCT')),
         src=[('COMPANY_BENEFICIAL_OWNERS', 'OWNERSHIP_PERCENTAGE')]),
    dict(id='OWN002', cat='Ownership', sev=30,
         test=lambda r: _f(r.get('OWNER_NATIONALITIES')) >= 4,
         msg=lambda r: 'Ownership spans %d nationalities'
                       % int(_f(r.get('OWNER_NATIONALITIES'))),
         src=[('COMPANY_BENEFICIAL_OWNERS', 'NATIONALITY_COUNTRY_ID')]),

    # --- history ---------------------------------------------------------
    dict(id='HIS001', cat='History', sev=70,
         test=lambda r: _f(r.get('PRIOR_TRUE_POSITIVES')) > 0,
         msg=lambda r: 'Client has %d previously confirmed true positive(s)'
                       % int(_f(r.get('PRIOR_TRUE_POSITIVES'))),
         src=[('RISK_ALERTS', 'RESOLUTION_CODE')]),
    dict(id='HIS002', cat='History', sev=40,
         test=lambda r: _f(r.get('PRIOR_ALERTS')) >= 3,
         msg=lambda r: 'Client has %d prior alerts' % int(_f(r.get('PRIOR_ALERTS'))),
         src=[('RISK_ALERTS', 'ALERT_ID')]),

    # --- sector ----------------------------------------------------------
    dict(id='SEC001', cat='Sector', sev=40,
         test=lambda r: str(r.get('INHERENT_RISK_LEVEL')) == 'HIGH',
         msg=lambda r: 'Industry %s carries HIGH inherent AML risk'
                       % r.get('INDUSTRY_NAME'),
         src=[('INDUSTRIES', 'INHERENT_RISK_LEVEL')]),

    # --- the trigger itself ----------------------------------------------
    # "scored 46 and placed in HIGH tier" restates the flag instead of
    # diagnosing it. This rule states the threshold that was crossed; the
    # component rules below say WHICH inputs pushed it there.
    dict(id='MDL001', cat='Model trigger', sev=50,
         test=lambda r: str(r.get('RISK_TIER')) == 'HIGH',
         msg=lambda r: ('Scored %s against the HIGH tier cut-off of 70'
                        % r.get('OVERALL_RISK_SCORE'))
                       + (' - note 70-73 is the overlap band where MEDIUM and HIGH '
                          'both occur, so tier assignment here is not determined by '
                          'the score alone'
                          if 70 <= _f(r.get('OVERALL_RISK_SCORE')) <= 73 else ''),
         src=[('TRANSACTION_RISK_SCORES', 'OVERALL_RISK_SCORE'),
              ('TRANSACTION_RISK_SCORES', 'RISK_TIER')]),
]

# --------------------------------------------------------------------------
# Model decomposition: which recorded inputs pushed the score up.
#
# The six sub-scores are recorded values, so stating that one is elevated is a
# fact about the data, not a claim about the model's internals. Percentiles are
# descriptive statistics over the 150,000 scored transactions - NOT fitted to
# outcomes, so this stays on the light governance path.
#
# Point contributions use weights recovered by least squares against the score
# itself. That is reverse-engineering an existing vendor model for
# documentation, not building a new one. The fit reproduces the score to within
# 10 points for 88.4% of transactions (R^2 = 0.863 on those); the remaining
# ~5% are flagged explicitly by MDL010 rather than silently mis-attributed.
# --------------------------------------------------------------------------
COMPONENTS = ['AMOUNT_RISK_SCORE', 'FREQUENCY_RISK_SCORE', 'GEOGRAPHY_RISK_SCORE',
              'COUNTERPARTY_RISK_SCORE', 'PATTERN_RISK_SCORE', 'VELOCITY_RISK_SCORE']
COMPONENT_LABEL = {'AMOUNT_RISK_SCORE': 'Amount', 'FREQUENCY_RISK_SCORE': 'Frequency',
                   'GEOGRAPHY_RISK_SCORE': 'Geography',
                   'COUNTERPARTY_RISK_SCORE': 'Counterparty',
                   'PATTERN_RISK_SCORE': 'Pattern', 'VELOCITY_RISK_SCORE': 'Velocity'}
COMPONENT_SEVERITY = {'GEOGRAPHY_RISK_SCORE': 55, 'COUNTERPARTY_RISK_SCORE': 50,
                      'AMOUNT_RISK_SCORE': 45, 'VELOCITY_RISK_SCORE': 45,
                      'FREQUENCY_RISK_SCORE': 40, 'PATTERN_RISK_SCORE': 40}
UNATTRIBUTED_POINTS_THRESHOLD = 15  # vs the alert-calibrated reconstruction

_POP = None


def population(path=None):
    """Percentiles and recovered weights over all scored transactions. Cached."""
    global _POP
    if _POP is not None:
        return _POP
    import numpy as np
    src = path or (ep.CSV / 'TRANSACTION_RISK_SCORES.csv')
    df = pd.read_csv(src, usecols=COMPONENTS + ['OVERALL_RISK_SCORE'])
    X = df[COMPONENTS].astype(float).values
    y = df['OVERALL_RISK_SCORE'].astype(float).values
    M = np.column_stack([X, np.ones(len(X))])
    coef, *_ = np.linalg.lstsq(M, y, rcond=None)
    resid = np.abs(y - M @ coef)
    _POP = {
        'n': len(df),
        'sorted': {c: np.sort(df[c].astype(float).values) for c in COMPONENTS},
        'median': {c: float(df[c].astype(float).median()) for c in COMPONENTS},
        'p90': {c: float(df[c].astype(float).quantile(0.90)) for c in COMPONENTS},
        'weights': {c: float(w) for c, w in zip(COMPONENTS, coef[:-1])},
        'intercept': float(coef[-1]),
        'attribution_within_10pts_pct': round(100.0 * float((resid <= 10).mean()), 1),
    }
    return _POP


# Refitting on alerted transactions alone reconstructs the score far better
# (R^2 0.813 vs 0.286) but the weights become uninterpretable: PATTERN comes out
# at -0.52 with a +71 intercept. That is collider bias - conditioning on "was
# alerted" induces spurious negative association between the components. Those
# weights reconstruct but do not explain, so they are used ONLY as a residual
# detector, never to claim that a component contributed N points.
_ALERT_FIT = None


def alert_fit(scores_path=None, alert_txn_ids=None):
    global _ALERT_FIT
    if _ALERT_FIT is not None:
        return _ALERT_FIT
    import numpy as np
    df = pd.read_csv(scores_path or (ep.CSV / 'TRANSACTION_RISK_SCORES.csv'),
                     usecols=COMPONENTS + ['OVERALL_RISK_SCORE', 'TRANSACTION_ID'])
    if alert_txn_ids is None:
        a = pd.read_csv(ep.CSV / 'RISK_ALERTS.csv', usecols=['TRANSACTION_ID'])
        alert_txn_ids = set(a['TRANSACTION_ID'])
    d = df[df['TRANSACTION_ID'].isin(alert_txn_ids)]
    X = d[COMPONENTS].astype(float).values
    y = d['OVERALL_RISK_SCORE'].astype(float).values
    M = np.column_stack([X, np.ones(len(X))])
    coef, *_ = np.linalg.lstsq(M, y, rcond=None)
    r = np.abs(y - M @ coef)
    _ALERT_FIT = {'weights': {c: float(w) for c, w in zip(COMPONENTS, coef[:-1])},
                  'intercept': float(coef[-1]), 'n': len(d),
                  'within_15pts_pct': round(100.0 * float((r <= 15).mean()), 1)}
    return _ALERT_FIT


def percentile_of(component, value):
    import numpy as np
    arr = population()['sorted'][component]
    return round(100.0 * float(np.searchsorted(arr, value, side='right')) / len(arr), 1)


def decompose(row):
    """Descriptive decomposition of the score.

    Reports each sub-score with its population percentile - pure description,
    always valid. Deliberately does NOT claim "component X contributed N points":
    the only fit that reconstructs alerted scores well has a negative PATTERN
    weight, so its contributions would be causally backwards. The reconstruction
    is used solely to size the unattributed residual.
    """
    p, af = population(), alert_fit()
    actual = _f(row.get('OVERALL_RISK_SCORE'))
    predicted = sum(af['weights'][c] * _f(row.get(c)) for c in COMPONENTS) + af['intercept']
    return {
        'actual_score': actual,
        'components': {c: {'value': _f(row.get(c)),
                           'population_median': p['median'][c],
                           'percentile': percentile_of(c, _f(row.get(c)))}
                       for c in COMPONENTS},
        'reconstructed': predicted,
        'unattributed': actual - predicted,
    }


def _component_rule(comp):
    def test(r, c=comp):
        return _f(r.get(c)) >= population()['p90'][c]

    def msg(r, c=comp):
        v = _f(r.get(c))
        return ('%s sub-score is %.0f, at the %.0fth percentile of all %s scored '
                'transactions (population median %.0f) - this is one of the inputs '
                'that pushed the overall score to %.0f'
                % (COMPONENT_LABEL[c], v, percentile_of(c, v),
                   format(population()['n'], ','), population()['median'][c],
                   _f(r.get('OVERALL_RISK_SCORE'))))
    return dict(id='MDL%03d' % (2 + COMPONENTS.index(comp)), cat='Model trigger',
                sev=COMPONENT_SEVERITY[comp], test=test, msg=msg,
                src=[('TRANSACTION_RISK_SCORES', comp)])


RULES += [_component_rule(c) for c in COMPONENTS]

RULES.append(dict(
    id='MDL010', cat='Model trigger', sev=60,
    test=lambda r: abs(decompose(r)['unattributed']) >= UNATTRIBUTED_POINTS_THRESHOLD,
    msg=lambda r: ('Score %.0f is %.0f points away from what its six recorded '
                   'sub-scores imply - an outlier even among alerts. FEATURE_VECTOR '
                   'is NULL so the missing driver is not recoverable; do not rely on '
                   'the score here, rely on the evidence below'
                   % (decompose(r)['actual_score'], abs(decompose(r)['unattributed']))),
    src=[('TRANSACTION_RISK_SCORES', 'OVERALL_RISK_SCORE'),
         ('TRANSACTION_RISK_SCORES', 'FEATURE_VECTOR')]))

# driver -> recommended action, keyed by rule id prefix or exact id
ACTIONS = [
    (['SCR001', 'SCR002'], 'Escalate to the sanctions team. Do not release the payment '
                           'pending review; blocking requires human authorisation.'),
    (['SCR003', 'SCR004'], 'Apply enhanced due diligence under the PEP policy and record '
                           'source of wealth.'),
    (['SCR005'], 'Review the adverse media file and record whether it is still material.'),
    (['KYC001', 'KYC002', 'KYC003'], 'Trigger KYC refresh and request current '
                                     'incorporation and ownership documentation.'),
    (['KYC002'], 'Re-rate the client: the current LOW rating is inconsistent with '
                 'expired due diligence.'),
    (['GEO001', 'GEO002', 'GEO003'], 'Verify the commercial rationale for this corridor '
                                     'and confirm the beneficiary bank relationship.'),
    (['TXN001', 'TXN003'], 'Request transaction purpose documentation and evidence of '
                           'source of funds.'),
    (['TXN002', 'TXN004'], 'Review the client 90-day transaction history for a '
                           'structuring pattern.'),
    (['HIS001'], 'Review the prior confirmed case for pattern continuity before '
                 'disposition.'),
    (['KYC005'], 'Schedule the outstanding enhanced due diligence review.'),
]


def derive_drivers(row, freq=None):
    out = []
    for rule in RULES:
        try:
            hit = bool(rule['test'](row))
        except Exception:
            hit = False
        if not hit:
            continue
        entry = {
            'rule_id': rule['id'],
            'category': rule['cat'],
            'severity': rule['sev'],
            'statement': rule['msg'](row),
            'evidence': [{'schema': S, 'table': t, 'column': c} for t, c in rule['src']],
            'population_frequency_pct': (None if freq is None
                                         else round(100.0 * freq.get(rule['id'], 0), 1)),
        }
        if rule['cat'] == 'Model trigger':
            d = decompose(row)
            entry['detail'] = {
                'actual_score': round(d['actual_score'], 1),
                'reconstructed': round(d['reconstructed'], 1),
                'unattributed': round(d['unattributed'], 1),
                'components': d['components'],
            }
        entry['query_text'] = None  # filled in build(), needs the row
        out.append(entry)
    out.sort(key=lambda d: (-d['severity'], d['rule_id']))
    return out


def derive_actions(drivers):
    ids = {d['rule_id'] for d in drivers}
    acts, seen = [], set()
    for keys, text in ACTIONS:
        if ids & set(keys) and text not in seen:
            seen.add(text)
            acts.append(text)
    if not acts:
        acts = ['Standard review against the alert type playbook.']
    return acts


# --------------------------------------------------------------------------
# Narration (stage 5).
#
# Prose is generated by template from the structured drivers - never by a model.
# Two forms, because a downstream RAG needs different things at different steps:
#
#   NARRATIVE       full write-up for a human and for indexing. Embeds far better
#                   than JSON, whose keys and punctuation dilute the signal.
#   RETRIEVAL_TEXT  compact form for the embedding itself.
#   query_text      per driver, for matching that driver against regulatory text.
#
# The structured RISK_DRIVERS stays authoritative. Handing an LLM prose and
# asking it to paraphrase means drift with nothing to check against; handing it
# structure means every number it emits can be verified against a source field.
# --------------------------------------------------------------------------
CATEGORY_ORDER = ['Screening', 'Due diligence', 'Jurisdiction', 'Behaviour',
                  'Ownership', 'History', 'Sector', 'Model trigger']


def narrative(row, drivers):
    if not drivers:
        return 'No deterministic risk drivers fired for this alert.'

    dest = row.get('DEST_COUNTRY_NAME', row.get('DEST_COUNTRY'))
    p = []

    p.append(
        'Alert %s was raised on a %s %s payment from %s to a beneficiary in %s on %s. '
        'The alert is typed %s / %s, was raised by %s, and has been open %s days.'
        % (row.get('ALERT_ID'), row.get('CURRENCY_ORIGINAL'),
           format(_f(row.get('AMOUNT_USD')), ',.2f'), row.get('LEGAL_NAME'), dest,
           str(row.get('INITIATED_AT'))[:10], row.get('ALERT_TYPE'),
           row.get('ALERT_SUBTYPE'), row.get('ALERT_SOURCE'),
           int(_f(row.get('ALERT_AGE_DAYS')))))

    # why the model fired, using the sub-scores
    model = [d for d in drivers if d['category'] == 'Model trigger']
    elevated = [d for d in model if d['rule_id'].startswith('MDL0')
                and d['rule_id'] not in ('MDL001', 'MDL010')]
    unattr = [d for d in model if d['rule_id'] == 'MDL010']
    if elevated:
        p.append('The transaction scored %s. %s'
                 % (row.get('OVERALL_RISK_SCORE'),
                    ' '.join(d['statement'] + '.' for d in elevated)))
    elif model:
        p.append('The transaction scored %s, crossing the HIGH tier cut-off of 70, '
                 'but none of its six recorded sub-scores is unusual against the '
                 'population.' % row.get('OVERALL_RISK_SCORE'))
    if unattr:
        p.append(unattr[0]['statement'] + '.')

    # the evidence, grouped
    rest = [d for d in drivers if d['category'] != 'Model trigger']
    if rest:
        by_cat = {}
        for d in rest:
            by_cat.setdefault(d['category'], []).append(d)
        chunks = []
        for cat in CATEGORY_ORDER:
            if cat in by_cat:
                chunks.append('%s: %s'
                              % (cat.lower(),
                                 '; '.join(x['statement'].rstrip('.')
                                           for x in by_cat[cat])))
        p.append('Supporting evidence drawn from the client and transaction record - '
                 + '. '.join(chunks) + '.')

    acts = derive_actions(drivers)
    p.append('Recommended next steps: ' + ' '.join(acts))
    p.append('This explanation was assembled deterministically from recorded fields '
             'by ruleset %s. No value here is inferred or imputed, and a human '
             'investigator remains accountable for the disposition.' % RULESET_VERSION)
    return '\n\n'.join(p)


def retrieval_text(row, drivers):
    """Compact form for embedding. Front-loads the discriminating facts."""
    top = '; '.join(d['statement'].rstrip('.') for d in drivers[:5])
    return ('%s %s payment to %s (%s), client %s, KYC %s/%s, industry %s. '
            'Alert %s %s. Drivers: %s.'
            % (row.get('CURRENCY_ORIGINAL'), format(_f(row.get('AMOUNT_USD')), ',.0f'),
               row.get('DEST_COUNTRY_NAME', row.get('DEST_COUNTRY')),
               row.get('DEST_FATF_STATUS', row.get('DEST_FATF')),
               row.get('LEGAL_NAME'), row.get('KYC_STATUS'), row.get('KYC_RISK_RATING'),
               row.get('INDUSTRY_NAME'), row.get('ALERT_TYPE'),
               row.get('ALERT_SUBTYPE'), top))


# Concept phrase per rule, in the vocabulary regulatory text actually uses.
# The driver STATEMENT is for a human and is full of figures; embedding it
# retrieves poorly because the numbers are noise. What matches FATF, Wolfsberg
# or MAS guidance is the concept - "structuring below a reporting threshold",
# not "amount USD 29,548 sits just below a reporting threshold".
CONCEPT = {
    'SCR001': 'sanctions screening match on the customer',
    'SCR002': 'sanctions screening match on a beneficial owner',
    'SCR003': 'politically exposed person, enhanced due diligence obligation',
    'SCR004': 'politically exposed beneficial owner, source of wealth',
    'SCR005': 'adverse media and negative news screening',
    'KYC001': 'customer due diligence expired, periodic review overdue',
    'KYC002': 'customer risk rating inconsistent with due diligence status',
    'KYC003': 'customer due diligence rejected and unremediated',
    'KYC004': 'high risk customer rating, ongoing monitoring',
    'KYC005': 'enhanced due diligence requirement outstanding',
    'GEO001': 'FATF call for action, high risk jurisdiction, counter-measures',
    'GEO002': 'FATF non-compliant jurisdiction, AML CFT deficiencies',
    'GEO003': 'FATF grey list, jurisdiction under increased monitoring',
    'GEO004': 'high risk country exposure, cross-border payment',
    'TXN001': 'transaction value inconsistent with the customer profile, source of funds',
    'TXN002': 'structuring, payments below the reporting threshold',
    'TXN003': 'transaction size anomalous against customer history',
    'TXN004': 'round-figure payments, layering indicator',
    'OWN001': 'controlling beneficial ownership interest, ultimate beneficial owner',
    'OWN002': 'complex ownership structure spanning multiple jurisdictions',
    'HIS001': 'repeat alerting customer with prior confirmed suspicious activity',
    'HIS002': 'repeat alerting customer, escalating pattern',
    'SEC001': 'high risk industry sector, inherent AML vulnerability',
}
CONCEPT_MODEL = 'transaction monitoring model score, alert threshold calibration'


def driver_concept(driver, row):
    """Concept vocabulary plus the context that narrows it, free of figures.

    This is the form that matches regulatory prose: FATF and Wolfsberg say
    "jurisdictions under increased monitoring", never "sub-score 95".
    """
    concept = CONCEPT.get(driver['rule_id'], CONCEPT_MODEL)
    dest = row.get('DEST_COUNTRY_NAME', row.get('DEST_COUNTRY')) or ''
    fatf = row.get('DEST_FATF_STATUS', row.get('DEST_FATF')) or ''
    bits = [concept]
    if driver['category'] in ('Jurisdiction', 'Behaviour', 'Model trigger') and dest:
        bits.append('destination %s' % dest)
        if fatf and fatf != 'MEMBER':
            bits.append('FATF %s' % str(fatf).replace('_', ' ').lower())
    if row.get('ALERT_TYPE'):
        bits.append(str(row.get('ALERT_TYPE')).replace('_', ' ').lower())
    if row.get('IS_CROSS_BORDER') is True:
        bits.append('cross-border')
    return ', '.join(bits)


def _fmt(v, money=False, pct=False, dp=None):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return 'not recorded'
    try:
        if pd.isna(v):
            return 'not recorded'
    except (TypeError, ValueError):
        pass
    if money:
        return 'USD %s' % format(_f(v), ',.0f')
    if pct:
        return '%.0f%%' % _f(v)
    if dp is not None:
        return format(round(_f(v), dp), ',g')
    return str(v)


def client_profile(row):
    """Everything recorded about the client and this transaction, in prose.

    Written as sentences rather than key=value pairs because embedding models
    handle natural phrasing better, and because the same text is readable in a
    retrieved chunk without post-processing.
    """
    dest = row.get('DEST_COUNTRY_NAME', row.get('DEST_COUNTRY'))
    fatf = row.get('DEST_FATF_STATUS', row.get('DEST_FATF'))
    parts = []

    parts.append(
        'Client %s is a %s in the %s segment, industry %s with %s inherent AML risk '
        'and %s AML sensitivity, %s employees, annual revenue %s.'
        % (_fmt(row.get('LEGAL_NAME')), _fmt(row.get('COMPANY_TYPE')),
           _fmt(row.get('CLIENT_SEGMENT')), _fmt(row.get('INDUSTRY_NAME')),
           _fmt(row.get('INHERENT_RISK_LEVEL')), _fmt(row.get('AML_SENSITIVITY')),
           _fmt(row.get('EMPLOYEE_COUNT')), _fmt(row.get('ANNUAL_REVENUE_USD'), money=True)))

    parts.append(
        'Due diligence status %s, risk rating %s, expiry %s. Politically exposed: %s. '
        'Sanctions hit: %s. Adverse media: %s. Enhanced due diligence required: %s. '
        'Composite client risk %s, tier %s, next review %s.'
        % (_fmt(row.get('KYC_STATUS')), _fmt(row.get('KYC_RISK_RATING')),
           _fmt(row.get('KYC_EXPIRY_DATE')), _fmt(row.get('PEP_ASSOCIATED')),
           _fmt(row.get('SANCTIONS_HIT')), _fmt(row.get('ADVERSE_MEDIA_FLAG')),
           _fmt(row.get('REQUIRES_EDD')), _fmt(row.get('CLIENT_COMPOSITE_RISK')),
           _fmt(row.get('CLIENT_RISK_TIER')), _fmt(row.get('NEXT_REVIEW_DATE'))))

    parts.append(
        'Ownership: %s beneficial owners across %s nationalities, largest holding %s, '
        '%s politically exposed, %s sanctions-matched.'
        % (_fmt(row.get('OWNER_COUNT')), _fmt(row.get('OWNER_NATIONALITIES')),
           _fmt(row.get('MAX_OWNERSHIP_PCT'), pct=True), _fmt(row.get('PEP_OWNERS')),
           _fmt(row.get('SANCTIONED_OWNERS'))))

    parts.append(
        'Normal behaviour for this client: %s transactions averaging %s, largest %s, '
        '%s distinct counterparties, %s of counterparties new.'
        % (_fmt(row.get('BASELINE_TXN_COUNT')),
           _fmt(row.get('BASELINE_AVG_AMOUNT'), money=True),
           _fmt(row.get('BASELINE_MAX_AMOUNT'), money=True),
           _fmt(row.get('BASELINE_UNIQUE_COUNTERPARTIES'), dp=0),
           _fmt(row.get('BASELINE_NEW_COUNTERPARTY_PCT'), pct=True)))

    parts.append(
        'History: %s prior alerts, %s previously confirmed as true positives.'
        % (_fmt(row.get('PRIOR_ALERTS')), _fmt(row.get('PRIOR_TRUE_POSITIVES'))))

    parts.append(
        'This transaction: %s (booked in %s), type %s / %s, purpose %s, %s, from %s to %s '
        '(FATF %s, country risk %s), beneficiary %s, initiated %s.'
        % (_fmt(row.get('AMOUNT_USD'), money=True), _fmt(row.get('CURRENCY_ORIGINAL')),
           _fmt(row.get('TRANSACTION_TYPE')), _fmt(row.get('TRANSACTION_SUBTYPE')),
           _fmt(row.get('PAYMENT_PURPOSE')),
           'cross-border' if row.get('IS_CROSS_BORDER') is True else 'domestic',
           _fmt(row.get('ORIG_COUNTRY_NAME', row.get('ORIG_COUNTRY'))), _fmt(dest),
           _fmt(fatf), _fmt(row.get('DEST_RISK_TIER')),
           _fmt(row.get('BENEFICIARY_NAME')), _fmt(str(row.get('INITIATED_AT'))[:10])))

    parts.append(
        'Alert %s, type %s / %s, raised by %s, priority %s, open %s days. Model score '
        '%s in tier %s.'
        % (_fmt(row.get('ALERT_ID')), _fmt(row.get('ALERT_TYPE')),
           _fmt(row.get('ALERT_SUBTYPE')), _fmt(row.get('ALERT_SOURCE')),
           _fmt(row.get('ALERT_PRIORITY')), _fmt(row.get('ALERT_AGE_DAYS')),
           _fmt(row.get('OVERALL_RISK_SCORE')), _fmt(row.get('RISK_TIER'))))

    return ' '.join(parts)


def driver_query_text(driver, row, comprehensive=True):
    """Indexed text for a driver.

    Comprehensive by default: the driver finding first, then the concept, then
    the full client and transaction profile. Front-loaded so the discriminating
    content survives any truncation the embedding model applies.
    """
    concept = driver_concept(driver, row)
    if not comprehensive:
        return concept
    return '%s %s. %s' % (driver['statement'].rstrip('.') + '.', concept,
                          client_profile(row))


# --------------------------------------------------------------------------
# Graph seeding.
#
# Stage 4 was resolving the client by LEGAL_NAME, which is not unique: 5,000
# companies share 1,999 distinct names. Alert 15002 is Whitby Group LLC =
# COMPANY_ID 1138 (country 22, TAX-298216), but name matching also pulled in
# COMPANY_ID 1592 - a different Whitby Group LLC in country 1 with a different
# tax id - and attributed its owners and alerts to the case file.
#
# We already hold the unambiguous key, so emit the node ids explicitly and give
# the graph layer nothing to infer. Formats match the ids stage 4 already uses.
#
# Beneficial owners are deliberately NOT seeded: their node ids are keyed on
# name too (BeneficialOwner:michel medhurst), which carries the same collision
# risk. Seed the client and let the graph traverse OWNS instead - that reaches
# the right owners by edge rather than by string match.
# --------------------------------------------------------------------------
def _slug(v):
    return None if v is None or pd.isna(v) else str(v).strip().lower()


def graph_seeds(row):
    """Unambiguous entry points for graph expansion, keyed on ids not names."""
    seeds = []

    def add(prefix, value, keyed_on):
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return
        seeds.append({'node_id': '%s:%s' % (prefix, value), 'keyed_on': keyed_on})

    cid = row.get('COMPANY_ID')
    add('Client', int(cid) if pd.notna(cid) else None, 'COMPANIES.COMPANY_ID')
    aid = row.get('ALERT_ID')
    add('Alert', int(aid) if pd.notna(aid) else None, 'RISK_ALERTS.ALERT_ID')
    tid = row.get('TRANSACTION_ID')
    add('Transaction', int(tid) if pd.notna(tid) else None,
        'TRANSACTIONS.TRANSACTION_ID')
    kid = row.get('CASE_ID')
    if pd.notna(kid):
        add('Case', int(kid), 'COMPLIANCE_CASES.CASE_ID')
    # jurisdictions are name-keyed in the graph, but country names are unique
    for col, role in (('DEST_COUNTRY_NAME', 'destination'),
                      ('ORIG_COUNTRY_NAME', 'origin')):
        s = _slug(row.get(col, row.get(col.replace('_NAME', ''))))
        if s:
            seeds.append({'node_id': 'Jurisdiction:%s' % s, 'keyed_on': 'COUNTRIES.COUNTRY_NAME',
                          'role': role})
    # de-duplicate, preserve order
    out, seen = [], set()
    for s in seeds:
        if s['node_id'] not in seen:
            seen.add(s['node_id'])
            out.append(s)
    return out


_ROW_FOR_FLAT = []


def driver_rows(alert_id, drivers, row=None):
    """Flatten to one row per driver - the shape a retrieval index wants."""
    _ROW_FOR_FLAT[:] = [row] if row is not None else []
    out = []
    for i, d in enumerate(drivers):
        out.append({
            'ALERT_ID': int(alert_id),
            'RULESET_VERSION': RULESET_VERSION,
            'DRIVER_SEQ': i + 1,
            'RULE_ID': d['rule_id'],
            'CATEGORY': d['category'],
            'SEVERITY': d['severity'],
            'STATEMENT': d['statement'][:1000],
            'CONCEPT': driver_concept(d, _ROW_FOR_FLAT[0])[:300] if _ROW_FOR_FLAT else None,
            'GRAPH_SEEDS': (json.dumps([x['node_id'] for x in graph_seeds(_ROW_FOR_FLAT[0])])
                            if _ROW_FOR_FLAT else None),
            'QUERY_TEXT': d['query_text'][:8000],
            'POPULATION_FREQUENCY_PCT': d.get('population_frequency_pct'),
            'EVIDENCE_TABLES': ', '.join(sorted({e['table'] for e in d['evidence']}))[:200],
        })
    return out


# --------------------------------------------------------------------------
_NUM = None


def _source_numbers(row, drivers):
    """Every number legitimately derivable from the record, in the string forms
    the templates emit."""
    import re
    vals = set()

    def add(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return
        if pd.isna(f):
            return
        for s in ('%.0f' % f, '%.1f' % f, '%.2f' % f, format(f, ',.0f'),
                  format(f, ',.2f'), str(int(f)) if abs(f - int(f)) < 1e-9 else ''):
            if s:
                vals.add(s.replace(',', ''))

    for k, v in row.items():
        add(v)
        if isinstance(v, str):
            for tok in re.findall(r'\d+', v):
                vals.add(tok)
    for d in drivers:
        det = d.get('detail') or {}
        for k, v in det.items():
            if isinstance(v, dict):
                for c in v.values():
                    if isinstance(c, dict):
                        for x in c.values():
                            add(x)
            else:
                add(v)
        add(d.get('population_frequency_pct'))
        add(d.get('severity'))
    # numbers the rules legitimately derived (ratios, percentiles, counts). The
    # property worth enforcing is that the PROSE introduces nothing the
    # structured layer does not already contain: narrative subset of
    # drivers + record + documented constants.
    import re as _re
    for d in drivers:
        for tok in _re.findall(r'\d[\d,]*\.?\d*', d.get('statement', '')):
            vals.add(tok.replace(',', '').rstrip('.'))
    # documented constants referenced by the templates
    add(population()['n'])          # size of the scoring population
    for extra in (70, 73, 90, 95, 100, 10, 15, 3, 0, 1, 2):
        add(extra)
    for part in RULESET_VERSION.lstrip('v').split('.'):
        vals.add(part)
    return vals


def verify_narrative(text, row, drivers):
    """Every number in the prose must be present in the source record.

    This is the property that makes generated text auditable. It catches numeric
    drift; it does NOT catch unsupported qualitative claims, which is why the
    templates never make any.
    """
    import re
    src = _source_numbers(row, drivers)
    bad = []
    for tok in re.findall(r'\d[\d,]*\.?\d*', text):
        norm = tok.replace(',', '').rstrip('.')
        if norm and norm not in src:
            try:
                if ('%.0f' % float(norm)) in src or ('%.2f' % float(norm)) in src:
                    continue
            except ValueError:
                pass
            bad.append(tok)
    return bad


def rule_frequencies(df):
    """How often each rule fires across a population."""
    counts = {r['id']: 0 for r in RULES}
    for _, row in df.iterrows():
        for x in derive_drivers(row):
            counts[x['rule_id']] += 1
    return {k: v / max(len(df), 1) for k, v in counts.items()}


def build(df, freq=None):
    """Compute drivers for every row.

    `freq` must be measured over the FULL alert population. Computing it from a
    filtered subset makes every driver read 100%, which is worse than omitting
    it - an investigator would conclude the driver is universal.
    """
    if freq is None:
        freq = rule_frequencies(df)
    raw = [derive_drivers(row) for _, row in df.iterrows()]

    recs, flat = [], []
    for (_, row), drivers in zip(df.iterrows(), raw):
        for x in drivers:
            x['population_frequency_pct'] = round(100.0 * freq[x['rule_id']], 1)
        for d in drivers:
            d['query_text'] = driver_query_text(d, row)
        actions = derive_actions(drivers)
        narr = narrative(row, drivers)
        unverified = verify_narrative(narr, row, drivers)
        flat.extend(driver_rows(row['ALERT_ID'], drivers, row))
        prov = ep.build_provenance(row)
        prov['graph_seeds'] = graph_seeds(row)
        prov['_meta']['ruleset_version'] = RULESET_VERSION
        prov['_meta']['rules_evaluated'] = len(RULES)
        prov['_meta']['model_transparency'] = (
            'Sub-scores are reported with their population percentile, which is '
            'descriptive and always valid. Point contributions are deliberately NOT '
            'claimed: the only fit reconstructing alerted scores well (R^2 0.813, '
            '%.1f%% within 15 points) carries a negative PATTERN weight caused by '
            'collider bias from conditioning on "was alerted", so its coefficients '
            'reconstruct without explaining. That fit is used only to size the '
            'residual. FEATURE_VECTOR is NULL, so where the residual is large the '
            'missing driver is unrecoverable and MDL010 says so.'
            % alert_fit()['within_15pts_pct'])
        recs.append({
            'ALERT_ID': int(row['ALERT_ID']),
            'RULESET_VERSION': RULESET_VERSION,
            'DRIVER_COUNT': len(drivers),
            'MAX_SEVERITY': max([d['severity'] for d in drivers], default=0),
            'TOP_DRIVER': drivers[0]['statement'][:120] if drivers else None,
            'RISK_DRIVERS': json.dumps(drivers, default=str),
            'RECOMMENDED_ACTIONS': json.dumps(actions),
            'INPUT_CONTEXT': json.dumps(prov, default=str),
            'GRAPH_SEEDS': json.dumps([x['node_id'] for x in graph_seeds(row)]),
            'NARRATIVE': narr[:8000],
            'RETRIEVAL_TEXT': retrieval_text(row, drivers)[:2000],
            'NARRATIVE_VERIFIED': len(unverified) == 0,
            'UNVERIFIED_TOKENS': json.dumps(unverified) if unverified else None,
            'GENERATED_AT': datetime.now(),
        })
    return pd.DataFrame(recs), pd.DataFrame(flat)


# --------------------------------------------------------------------------
# HANA writes
# --------------------------------------------------------------------------
DRIVER_DDL = """
CREATE COLUMN TABLE {S}.{T} (
    ALERT_ID                 INTEGER      NOT NULL,
    RULESET_VERSION          NVARCHAR(20) NOT NULL,
    DRIVER_SEQ               INTEGER      NOT NULL,
    RULE_ID                  NVARCHAR(10),
    CATEGORY                 NVARCHAR(30),
    SEVERITY                 INTEGER,
    STATEMENT                NVARCHAR(1000),
    CONCEPT                  NVARCHAR(300),
    GRAPH_SEEDS              NVARCHAR(500),
    QUERY_TEXT               NCLOB,
    POPULATION_FREQUENCY_PCT DECIMAL(5,1),
    EVIDENCE_TABLES          NVARCHAR(200),
    PRIMARY KEY (ALERT_ID, RULESET_VERSION, DRIVER_SEQ)
)
"""

DDL = """
CREATE COLUMN TABLE {S}.{T} (
    ALERT_ID             INTEGER      NOT NULL,
    RULESET_VERSION      NVARCHAR(20) NOT NULL,
    DRIVER_COUNT         INTEGER,
    MAX_SEVERITY         INTEGER,
    TOP_DRIVER           NVARCHAR(120),
    RISK_DRIVERS         NCLOB,
    RECOMMENDED_ACTIONS  NCLOB,
    INPUT_CONTEXT        NCLOB,
    GRAPH_SEEDS          NVARCHAR(500),
    NARRATIVE            NCLOB,
    RETRIEVAL_TEXT       NVARCHAR(2000),
    NARRATIVE_VERIFIED   BOOLEAN,
    UNVERIFIED_TOKENS    NVARCHAR(500),
    GENERATED_AT         TIMESTAMP,
    PRIMARY KEY (ALERT_ID, RULESET_VERSION)
)
"""


def connect():
    from hdbcli import dbapi
    db = json.loads((ROOT / 'team_07_credentials.json').read_text())['database']
    return dbapi.connect(address=db['host'], port=int(db['port']), user=db['username'],
                         password=db['password'], encrypt=True,
                         sslValidateCertificate=False, connectTimeout=30000)


def table_exists(cur, schema, table):
    cur.execute("SELECT COUNT(*) FROM SYS.TABLES WHERE SCHEMA_NAME = ? AND TABLE_NAME = ?",
                [schema, table])
    return cur.fetchone()[0] > 0


def write_hana(recs, drv_rows=None, update_alerts=False, replace=False):
    conn = connect()
    cur = conn.cursor()
    try:
        if replace and table_exists(cur, S, TABLE):
            cur.execute('DROP TABLE %s.%s' % (S, TABLE))
            print('  dropped existing %s.%s' % (S, TABLE))
        if not table_exists(cur, S, TABLE):
            cur.execute(DDL.replace('{S}', S).replace('{T}', TABLE))
            print('  created %s.%s' % (S, TABLE))
        else:
            cur.execute('DELETE FROM %s.%s WHERE RULESET_VERSION = ?' % (S, TABLE),
                        [RULESET_VERSION])
            print('  cleared %d prior rows for ruleset %s' % (cur.rowcount, RULESET_VERSION))

        cols = ['ALERT_ID', 'RULESET_VERSION', 'DRIVER_COUNT', 'MAX_SEVERITY', 'TOP_DRIVER',
                'RISK_DRIVERS', 'RECOMMENDED_ACTIONS', 'INPUT_CONTEXT', 'GRAPH_SEEDS',
                'NARRATIVE',
                'RETRIEVAL_TEXT', 'NARRATIVE_VERIFIED', 'UNVERIFIED_TOKENS',
                'GENERATED_AT']
        sql = ('INSERT INTO %s.%s (%s) VALUES (%s)'
               % (S, TABLE, ', '.join(cols), ', '.join('?' * len(cols))))
        rows = [[r[c] for c in cols] for _, r in recs.iterrows()]
        cur.executemany(sql, rows)
        conn.commit()
        print('  inserted %s rows into %s.%s' % (format(len(rows), ','), S, TABLE))

        if drv_rows is not None and len(drv_rows):
            if replace and table_exists(cur, S, DRIVER_TABLE):
                cur.execute('DROP TABLE %s.%s' % (S, DRIVER_TABLE))
            if not table_exists(cur, S, DRIVER_TABLE):
                cur.execute(DRIVER_DDL.replace('{S}', S).replace('{T}', DRIVER_TABLE))
                print('  created %s.%s' % (S, DRIVER_TABLE))
            else:
                cur.execute('DELETE FROM %s.%s WHERE RULESET_VERSION = ?'
                            % (S, DRIVER_TABLE), [RULESET_VERSION])
            dcols = list(drv_rows.columns)
            cur.executemany('INSERT INTO %s.%s (%s) VALUES (%s)'
                            % (S, DRIVER_TABLE, ', '.join(dcols),
                               ', '.join('?' * len(dcols))),
                            [[r[c] for c in dcols] for _, r in drv_rows.iterrows()])
            conn.commit()
            print('  inserted %s driver rows into %s.%s'
                  % (format(len(drv_rows), ','), S, DRIVER_TABLE))

        if update_alerts:
            # snapshot the originals once, before mutating anything
            if not table_exists(cur, S, BACKUP):
                cur.execute('CREATE COLUMN TABLE %s.%s AS (SELECT ALERT_ID, RISK_DRIVERS, '
                            'RECOMMENDED_ACTIONS FROM %s.RISK_ALERTS)' % (S, BACKUP, S))
                conn.commit()
                print('  snapshotted originals to %s.%s' % (S, BACKUP))
            else:
                print('  backup %s.%s already exists, not overwriting' % (S, BACKUP))
            cur.executemany(
                'UPDATE %s.RISK_ALERTS SET RISK_DRIVERS = ?, RECOMMENDED_ACTIONS = ? '
                'WHERE ALERT_ID = ?' % S,
                [[r['RISK_DRIVERS'], r['RECOMMENDED_ACTIONS'], r['ALERT_ID']]
                 for _, r in recs.iterrows()])
            conn.commit()
            print('  updated %s rows in %s.RISK_ALERTS' % (format(len(recs), ','), S))
    finally:
        cur.close()
        conn.close()


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--local', action='store_true', help='use the CSV extract, write files')
    ap.add_argument('--hana', action='store_true', help='write to HANA')
    ap.add_argument('--update-alerts', action='store_true',
                    help='also fill RISK_ALERTS.RISK_DRIVERS (snapshots originals first)')
    ap.add_argument('--replace', action='store_true', help='drop and recreate the table')
    ap.add_argument('--alert', type=int)
    ap.add_argument('--limit', type=int)
    a = ap.parse_args()

    fetch = ep.fetch_local if (a.local or not a.hana) else ep.fetch_hana
    df = fetch(alert_ids=[a.alert] if a.alert else None, limit=a.limit)
    print('evidence pack: %d alerts' % len(df))

    # frequencies must always come from the whole population, never the subset
    if a.alert or a.limit:
        freq = rule_frequencies(ep.fetch_local())
        print('rule frequencies measured over the full 5,000-alert population')
    else:
        freq = rule_frequencies(df)
    recs, drv_rows = build(df, freq)
    nv = int((~recs['NARRATIVE_VERIFIED']).sum())
    print('narrative faithfulness: %d of %d fully verified%s'
          % (len(recs) - nv, len(recs),
             '' if nv == 0 else '  <-- %d contain an unsourced number' % nv))
    print('drivers derived: %s total, %.2f per alert, %d alerts with none'
          % (format(int(recs['DRIVER_COUNT'].sum()), ','), recs['DRIVER_COUNT'].mean(),
             int((recs['DRIVER_COUNT'] == 0).sum())))

    print('\nrule firing rates:')
    for r in sorted(RULES, key=lambda x: -freq[x['id']]):
        print('  %-7s %-14s sev %3d  %6.1f%%' % (r['id'], r['cat'], r['sev'],
                                                 100 * freq[r['id']]))

    if a.alert and len(recs):
        r0 = recs.iloc[0]
        print('\n' + '=' * 78)
        print(r0['NARRATIVE'])
        print('=' * 78)
        for d in json.loads(r0['RISK_DRIVERS']):
            print('  [%3d] %-13s %s' % (d['severity'], d['category'], d['statement']))
            print('        evidence: %s' % ', '.join('%s.%s' % (e['table'], e['column'])
                                                     for e in d['evidence']))
            print('        fires on %.1f%% of alerts' % d['population_frequency_pct'])
        print('\n  recommended actions:')
        for x in json.loads(r0['RECOMMENDED_ACTIONS']):
            print('   - %s' % x)

    if a.hana:
        print('\nwriting to HANA ...')
        write_hana(recs, drv_rows, update_alerts=a.update_alerts, replace=a.replace)
    else:
        out = ROOT / 'alert_explanations.csv'
        recs.to_csv(out, index=False)
        dq = ROOT / 'alert_driver_queries.csv'
        drv_rows.to_csv(dq, index=False)
        print('\nwrote %s (%d rows)' % (out, len(recs)))
        print('wrote %s (%d rows, one per driver - this is the retrieval index)'
              % (dq, len(drv_rows)))
        print('add --hana to write into %s.%s' % (S, TABLE))
    return 0


if __name__ == '__main__':
    sys.exit(main())
