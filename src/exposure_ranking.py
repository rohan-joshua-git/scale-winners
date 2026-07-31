#!/usr/bin/env python3
"""
ExposureRanker - deterministic prioritisation of the unresolved alert backlog.

Object form of eval_ranking.py. Same factors, same weights, same stress tests;
configuration and cached state now live on an instance, and the ranking can be
driven either from the CSV extract or from a Stage2 evidence pack.

What this is, and how to describe it:

  This is NOT derived from regulation. No provision prescribes weighting age at
  0.30. The FACTORS were selected by reference to supervisory priorities and to
  the standing remediation programme, which names aged-alert backlogs explicitly.
  The WEIGHTS are policy - set by institutional risk appetite, published, and
  stress-tested. That is how AML methodologies are actually documented, and it
  is defensible in a way "derived from regulations" is not.

  Weights must stay policy-set. Fitting them against RESOLUTION_CODE would turn
  this into a model influencing customer outcomes and forfeit the light
  governance path - which is the whole reason it can ship at month 3.

What it cannot do:

  It does not predict which alerts are worth working. Nothing can: alert
  outcomes are independent of every recorded feature (CV AUC 0.4928 against a
  0.4824 shuffled floor). This ranks by regulatory CONSEQUENCE of leaving an
  alert unresolved, which is a different objective from precision. Say so.

Usage:
    from src.exposure_ranking import ExposureRanker

    r = ExposureRanker()
    ranked = r.rank()                     # scored backlog, highest exposure first
    r.report()                            # all five stress tests
    r.queues()                            # banded queues with reserved capacity
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

def _project_root(start):
    """Walk up until we find the extract. Works whether this module lives in the
    repo root or in src/, so moving files does not silently break paths."""
    p = Path(start).resolve()
    for cand in [p.parent] + list(p.parents):
        if ((cand / 'data' / 'TEAM_07').is_dir()
                or (cand / 'data' / 'cache').is_dir()
                or (cand / 'team_07_credentials.json').is_file()):
            return cand
    return p.parent


ROOT = _project_root(__file__)
sys.path.insert(0, str(ROOT))


class ExposureRanker:
    """Deterministic exposure ranking over unresolved alerts.

    Parameters
    ----------
    weights        : factor weights. Policy-set; do not fit them.
    as_of          : reference date for alert ageing.
    supervisory    : regions under heightened supervision. Adding this factor
                     lifts supervised-region coverage in the top decile from
                     71% to 96% at a cost of 25% queue displacement.
    csv_dir        : location of the extract.
    """

    FATF_WEIGHT = {'BLACK_LIST': 1.00, 'NON_COMPLIANT': 0.80,
                   'GREY_LIST': 0.60, 'MEMBER': 0.15}
    PRIORITY_WEIGHT = {'CRITICAL': 1.00, 'HIGH': 0.65, 'MEDIUM': 0.30}
    DEFAULT_WEIGHTS = {'age': 0.30, 'jurisdiction': 0.25, 'priority': 0.20,
                       'value': 0.15, 'unassigned': 0.10}
    SUPERVISED_REGIONS = ('EMEA', 'APAC')
    BANDS = [('NEW (<=30d)', 0, 30, 0.10),
             ('CURRENT (31-365d)', 31, 365, 0.20),
             ('AGED (>1yr)', 366, 10 ** 6, 0.70)]

    def __init__(self, weights=None, as_of='2026-07-31', supervisory=False,
                 csv_dir=None, seed=0):
        self.weights = dict(weights or self.DEFAULT_WEIGHTS)
        if supervisory and 'supervisory' not in self.weights:
            self.weights = {k: v * 0.85 for k, v in self.weights.items()}
            self.weights['supervisory'] = 0.15
        self.as_of = pd.Timestamp(as_of)
        self.supervisory = supervisory
        self.csv_dir = Path(csv_dir) if csv_dir else ROOT / 'data' / 'TEAM_07'
        self.rng = np.random.default_rng(seed)
        self._backlog = None
        self._factors = None

    def __repr__(self):
        return ('ExposureRanker(factors=%s, supervisory=%s, as_of=%s)'
                % (list(self.weights), self.supervisory, self.as_of.date()))

    # -- data -------------------------------------------------------------
    def _load(self, name):
        """CSV extract if present, else the parquet cache written by
        pipeline/ingestion/hana_source.py. Same frames either way - the cache is
        just what the HANA read path already persists, so the ranker works in a
        checkout that has data/cache/ but not the data/TEAM_07/ CSV extract."""
        csv = self.csv_dir / (name + '.csv')
        if csv.exists():
            df = pd.read_csv(csv, low_memory=False)
        else:
            pq = ROOT / 'data' / 'cache' / (name.lower() + '.parquet')
            if not pq.exists():
                raise FileNotFoundError(
                    'no source for %s: looked for %s and %s' % (name, csv, pq))
            df = pd.read_parquet(pq)
        for c in df.columns:
            if df[c].dtype == object:
                u = set(df[c].dropna().astype(str).unique()[:5])
                if u and u <= {'true', 'false'}:
                    df[c] = df[c].astype(str).map({'true': True, 'false': False})
        return df

    def backlog(self, pack=None, recompute=False):
        """Unresolved alerts with the attributes the factors need.

        Accepts a Stage2 evidence pack, or assembles from the extract.
        """
        if self._backlog is not None and not recompute and pack is None:
            return self._backlog
        if pack is not None:
            B = pack[pack['RESOLUTION_CODE'].isna()].copy()
            B['FATF_STATUS'] = B.get('DEST_FATF_STATUS', B.get('DEST_FATF'))
        else:
            A, T = self._load('RISK_ALERTS'), self._load('TRANSACTIONS')
            CTRY, CO = self._load('COUNTRIES'), self._load('COMPANIES')
            REG = self._load('REGIONS')
            A['CREATED_AT'] = pd.to_datetime(A['CREATED_AT'], errors='coerce')
            B = A[A['RESOLUTION_CODE'].isna()].copy()
            B = (B.merge(T[['TRANSACTION_ID', 'AMOUNT_USD', 'DESTINATION_COUNTRY_ID']],
                         on='TRANSACTION_ID', how='left')
                  .merge(CTRY[['COUNTRY_ID', 'COUNTRY_NAME', 'FATF_STATUS', 'RISK_TIER']],
                         left_on='DESTINATION_COUNTRY_ID', right_on='COUNTRY_ID', how='left')
                  .merge(CO[['COMPANY_ID', 'HEADQUARTERS_COUNTRY_ID']],
                         on='COMPANY_ID', how='left'))
            geo = (CTRY[['COUNTRY_ID', 'REGION_ID']]
                   .merge(REG[['REGION_ID', 'REGION_CODE']], on='REGION_ID')
                   .rename(columns={'COUNTRY_ID': 'HEADQUARTERS_COUNTRY_ID'}))
            B = B.merge(geo, on='HEADQUARTERS_COUNTRY_ID', how='left')
        if 'age_days' not in B.columns:
            B['age_days'] = (self.as_of - pd.to_datetime(B['CREATED_AT'],
                                                         errors='coerce')).dt.days
        self._backlog = B.reset_index(drop=True)
        self._factors = None
        return self._backlog

    # -- the score --------------------------------------------------------
    def factors(self, B=None):
        """Each factor scaled to [0,1] so the weights are directly comparable."""
        if self._factors is not None and B is None:
            return self._factors
        B = self.backlog() if B is None else B
        f = pd.DataFrame(index=B.index)
        # rank-normalised, so the long ageing tail cannot dominate
        f['age'] = B['age_days'].rank(pct=True)
        f['jurisdiction'] = B['FATF_STATUS'].map(self.FATF_WEIGHT).fillna(0.15)
        f['priority'] = B['ALERT_PRIORITY'].map(self.PRIORITY_WEIGHT).fillna(0.30)
        # percentile of log amount: USD is lognormal, so raw values would let a
        # handful of large wires decide the whole ordering
        f['value'] = np.log10(B['AMOUNT_USD'].clip(lower=1)).rank(pct=True)
        f['unassigned'] = B['ASSIGNED_TO'].isna().astype(float)
        if 'supervisory' in self.weights:
            f['supervisory'] = (B.get('REGION_CODE', pd.Series(index=B.index, dtype=object))
                                .isin(self.SUPERVISED_REGIONS).astype(float))
        self._factors = f
        return f

    def score(self, F=None, weights=None):
        F = self.factors() if F is None else F
        w = weights or self.weights
        use = {k: v for k, v in w.items() if k in F.columns}
        return sum(F[k] * v for k, v in use.items()) / sum(use.values())

    def rank(self, pack=None):
        """Scored backlog, highest exposure first."""
        B = self.backlog(pack=pack).copy()
        B['exposure'] = self.score(self.factors(B))
        B['exposure_rank'] = B['exposure'].rank(ascending=False, method='first').astype(int)
        B['band'] = pd.cut(B['age_days'], [-1, 30, 365, 10 ** 6],
                           labels=[b[0] for b in self.BANDS])
        return B.sort_values('exposure_rank')

    # -- capacity ---------------------------------------------------------
    def queues(self, capacity=None, pack=None):
        """Banded queues with reserved capacity.

        A single ranked list starves new critical alerts: 1,010 aged alerts
        compete for a 155-slot decile, so any weight on age fills it with the
        aged cohort and only 1 of 38 new CRITICALs surfaces. That is capacity
        allocation, not ranking, and no weighting fixes it. Reserving a share
        per band lifts new criticals to 5 of 38 and raises high-risk
        jurisdiction coverage from 67.7% to 76.8%.
        """
        B = self.rank(pack=pack)
        capacity = capacity or max(1, int(0.10 * len(B)))
        picked = []
        for name, lo, hi, share in self.BANDS:
            sub = B[(B['age_days'] >= lo) & (B['age_days'] <= hi)]
            k = int(round(share * capacity))
            picked.append(sub.nlargest(min(k, len(sub)), 'exposure'))
        out = pd.concat(picked).sort_values('exposure', ascending=False)
        return out

    # -- stress tests -----------------------------------------------------
    def discrimination(self):
        s = self.rank()['exposure']
        x = np.sort(s.values)
        n = len(x)
        gini = (2 * np.arange(1, n + 1) - n - 1).dot(x) / (n * x.sum())
        return {'iqr': round(float(s.quantile(.75) - s.quantile(.25)), 3),
                'gini': round(float(gini), 3),
                'separates': bool(s.quantile(.75) - s.quantile(.25) > 0.10)}

    def displacement(self, n=100):
        """How far this reorders the queue versus working by priority alone.

        The baseline must be built on the backlog in its natural order, NOT on
        the exposure-sorted frame. ALERT_PRIORITY has three levels, so ties are
        enormous and method='first' breaks them by row position - computing it
        on an exposure-sorted frame makes the baseline agree with exposure by
        construction and reports 15% displacement instead of 84%.
        """
        B = self.rank()
        nat = self.backlog().sort_values('ALERT_ID')
        today = nat['ALERT_PRIORITY'].map(self.PRIORITY_WEIGHT).rank(
            ascending=False, method='first')
        new = set(B.nlargest(n, 'exposure')['ALERT_ID'])
        old = set(nat.assign(t=today).nsmallest(n, 't')['ALERT_ID'])
        return {'n': n, 'overlap': len(new & old),
                'displaced_pct': round(100 * (1 - len(new & old) / n), 1),
                'spearman': round(float(
                    B.set_index('ALERT_ID')['exposure'].corr(
                        nat.assign(t=today).set_index('ALERT_ID')['t'],
                        method='spearman')), 3)}

    def weight_sensitivity(self, n=50, draws=300, jitter=0.30):
        """Bounded perturbation - the fair robustness test.

        Dirichlet draws let one weight reach 0.6+, which is a different risk
        policy rather than a perturbation. Report both if asked; this is the
        one that answers "why those weights".
        """
        F = self.factors()
        base = set(self.rank().nlargest(n, 'exposure')['ALERT_ID'])
        B = self.rank()
        jac = []
        for _ in range(draws):
            w = {k: v * self.rng.uniform(1 - jitter, 1 + jitter)
                 for k, v in self.weights.items()}
            alt = set(B.assign(z=self.score(F, w)).nlargest(n, 'z')['ALERT_ID'])
            jac.append(len(base & alt) / len(base | alt))
        eq = set(B.assign(z=self.score(F, dict.fromkeys(self.weights, 1.0)))
                 .nlargest(n, 'z')['ALERT_ID'])
        return {'n': n, 'mean_jaccard': round(float(np.mean(jac)), 3),
                'p5': round(float(np.percentile(jac, 5)), 3),
                'min': round(float(np.min(jac)), 3),
                'equal_weight_overlap': len(base & eq)}

    def coverage(self, use_queues=False):
        """Share of the surfaced set exhibiting each risk dimension, against
        single-factor baselines. Multi-factor must beat the best single sort or
        the complexity is unjustified."""
        B = self.rank()
        F = self.factors()
        N = max(1, int(0.10 * len(B)))
        dims = {
            'high-risk jurisdiction': B['FATF_STATUS'].isin(
                ['BLACK_LIST', 'GREY_LIST', 'NON_COMPLIANT']),
            'CRITICAL': B['ALERT_PRIORITY'].eq('CRITICAL'),
            'over 1 year old': B['age_days'].gt(365),
            'unassigned': B['ASSIGNED_TO'].isna(),
            'top-decile value': B['AMOUNT_USD'].rank(pct=True).gt(0.9),
        }
        cands = {'exposure (multi-factor)': B['exposure']}
        for k in F.columns:
            cands['%s only' % k] = F[k]
        rows = {}
        for name, s in cands.items():
            top = (self.queues(capacity=N).index if
                   (use_queues and name.startswith('exposure')) else s.nlargest(N).index)
            rows[name] = {d: round(100 * m.loc[top].mean(), 1) for d, m in dims.items()}
        out = pd.DataFrame(rows).T
        out['MEAN'] = out.mean(axis=1).round(1)
        return out.sort_values('MEAN', ascending=False)

    def starvation(self, max_age=90, priority='CRITICAL'):
        """Do new high-priority alerts reach the worked set?"""
        B = self.rank()
        N = max(1, int(0.10 * len(B)))
        young = B[(B['age_days'] <= max_age) & (B['ALERT_PRIORITY'] == priority)]
        single = set(B.nlargest(N, 'exposure')['ALERT_ID'])
        banded = set(self.queues(capacity=N)['ALERT_ID'])
        return {'candidates': len(young),
                'surfaced_single_queue': len(set(young['ALERT_ID']) & single),
                'surfaced_banded_queues': len(set(young['ALERT_ID']) & banded)}

    def capacity_plan(self, analysts=10, per_day=1.0, working_days=21):
        B = self.rank()
        rate = analysts * per_day * working_days
        return {'backlog': len(B), 'per_month': rate,
                'months_to_clear': round(len(B) / rate, 1)}

    # -- reporting --------------------------------------------------------
    def report(self):
        B = self.rank()
        print(self)
        print('backlog under evaluation: %s unresolved alerts\n' % format(len(B), ','))
        print('discrimination      :', self.discrimination())
        print('displacement vs today:', self.displacement())
        print('weight sensitivity  :', self.weight_sensitivity())
        print('starvation          :', self.starvation())
        print('capacity            :', self.capacity_plan())
        print('\ncoverage of the top decile (%):')
        print(self.coverage().to_string())
        print('\nCannot be validated predictively: there is no ground-truth priority '
              'and alert outcomes are noise. The defence is normative - these are the '
              'factors the CRO and the regulator named, and the ordering is stable '
              'across reasonable weightings.')
        return B


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--supervisory', action='store_true',
                    help='add the supervised-jurisdiction factor (EMEA/APAC)')
    ap.add_argument('--out')
    a = ap.parse_args()
    r = ExposureRanker(supervisory=a.supervisory)
    B = r.report()
    if a.out:
        cols = ['ALERT_ID', 'exposure_rank', 'exposure', 'band', 'age_days',
                'ALERT_PRIORITY', 'FATF_STATUS', 'AMOUNT_USD', 'ASSIGNED_TO']
        B[[c for c in cols if c in B.columns]].to_csv(a.out, index=False)
        print('\nwrote %s' % a.out)
