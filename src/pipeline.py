#!/usr/bin/env python3
"""
Object interface over the explainability pipeline.

    Stage2 - evidence assembly.  One provenance-carrying record per alert.
    Stage3 - driver derivation, narration and persistence.

The existing module-level functions in evidence_pack.py and risk_drivers.py are
unchanged and still work; these classes wrap them so configuration and cached
state live on an instance instead of in module globals.

That is the substantive gain, not the syntax. The functional code kept the
scoring population and the recovered weights in module-level caches, so two
pipelines pointed at different schemas would silently share whichever was
computed first. Here each Stage3 owns its own.

One honest limitation: the rule closures in risk_drivers.RULES read those caches
through the module, so Stage3._activate() installs this instance's state before
every operation. Instances are therefore safe used sequentially - which is how a
batch pipeline runs - but not if two are interleaved across threads. Documented
rather than hidden; fixing it properly means threading a context object through
all 31 rules.

Usage:
    from pipeline import Stage2, Stage3

    s2 = Stage2(source='local')
    s2.self_test()

    s3 = Stage3(s2)
    explanations, driver_queries = s3.build()
    s3.write_hana(explanations, driver_queries)

    # or the whole thing
    Stage3(Stage2(source='hana')).run(to_hana=True)
"""

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
sys.path.insert(0, str(ROOT))

import evidence_pack as ep      # noqa: E402
import risk_drivers as rd       # noqa: E402


# ==========================================================================
class Stage2:
    """Evidence assembly.

    Parameters
    ----------
    schema   : HANA schema holding the source tables.
    source   : 'hana' to query live, 'local' to read the CSV extract.
    csv_dir  : override the extract location.
    creds    : override the credentials file.
    """

    def __init__(self, schema=ep.S, source='hana', csv_dir=None, creds=None):
        if source not in ('hana', 'local'):
            raise ValueError("source must be 'hana' or 'local', got %r" % source)
        self.schema = schema
        self.source = source
        self.csv_dir = Path(csv_dir) if csv_dir else ep.CSV
        self.creds = Path(creds) if creds else ep.CREDS
        self._cache = None

    # -- introspection ----------------------------------------------------
    @property
    def sql(self):
        """The pushed-down query, with this instance's schema substituted."""
        return ep.sql(schema=self.schema)

    def __repr__(self):
        return 'Stage2(schema=%r, source=%r)' % (self.schema, self.source)

    # -- retrieval --------------------------------------------------------
    def fetch(self, alert_ids=None, limit=None, use_cache=False):
        """One row per alert. Column names are canonicalised so downstream code
        cannot depend on which transport ran."""
        if use_cache and self._cache is not None and not alert_ids and not limit:
            return self._cache
        self._install()
        if self.source == 'hana':
            df = ep.fetch_hana(alert_ids=alert_ids, limit=limit)
        else:
            df = ep.fetch_local(alert_ids=alert_ids, limit=limit)
        if use_cache and not alert_ids and not limit:
            self._cache = df
        return df

    def fetch_one(self, alert_id):
        df = self.fetch(alert_ids=[alert_id])
        if df.empty:
            raise LookupError('alert %s not found' % alert_id)
        return df.iloc[0]

    def provenance(self, row):
        """Source table and key for every block - the INPUT_CONTEXT payload."""
        return ep.build_provenance(row, schema=self.schema)

    def self_test(self):
        """Verify the join logic on the extract. Returns True if all checks pass."""
        self._install()
        return ep.self_test() == 0

    # -- internals --------------------------------------------------------
    def _install(self):
        """Point the functional module at this instance's configuration."""
        ep.S = self.schema
        ep.CSV = self.csv_dir
        ep.CREDS = self.creds


# ==========================================================================
class Stage3:
    """Driver derivation, narration and persistence.

    Holds the scoring-population statistics and the recovered weights, so two
    instances over different data do not contaminate each other.
    """

    def __init__(self, stage2=None, ruleset_version=rd.RULESET_VERSION,
                 schema=None, table=rd.TABLE, driver_table=rd.DRIVER_TABLE,
                 backup_table=rd.BACKUP):
        self.stage2 = stage2 or Stage2(source='local')
        self.ruleset_version = ruleset_version
        self.schema = schema or self.stage2.schema
        self.table = table
        self.driver_table = driver_table
        self.backup_table = backup_table
        self._pop = None
        self._fit = None
        self._freq = None

    def __repr__(self):
        return ('Stage3(schema=%r, ruleset=%r, rules=%d, source=%r)'
                % (self.schema, self.ruleset_version, len(self.rules),
                   self.stage2.source))

    # -- the rulebook -----------------------------------------------------
    @property
    def rules(self):
        return rd.RULES

    def rulebook(self):
        """The rulebook as a table - what a validator signs off."""
        return pd.DataFrame([
            {'rule_id': r['id'], 'category': r['cat'], 'severity': r['sev'],
             'concept': rd.CONCEPT.get(r['id'], rd.CONCEPT_MODEL),
             'sources': ', '.join(sorted({t for t, _ in r['src']}))}
            for r in rd.RULES]).sort_values(['category', 'severity'],
                                            ascending=[True, False])

    # -- population statistics -------------------------------------------
    @property
    def population(self):
        self._activate()
        return rd.population()

    @property
    def alert_fit(self):
        self._activate()
        return rd.alert_fit()

    def frequencies(self, df=None, recompute=False):
        """How often each rule fires. Always measured over the full population -
        computing it from a subset makes every driver read 100%."""
        if self._freq is not None and not recompute:
            return self._freq
        self._activate()
        self._freq = rd.rule_frequencies(df if df is not None else self.stage2.fetch(
            use_cache=True))
        return self._freq

    # -- per-alert --------------------------------------------------------
    def drivers(self, row, with_query_text=True):
        self._activate()
        out = rd.derive_drivers(row, self.frequencies())
        if with_query_text:
            for d in out:
                d['query_text'] = rd.driver_query_text(d, row)
        return out

    def seeds(self, row):
        """Unambiguous graph entry points for this alert - ids, never names."""
        self._activate()
        return rd.graph_seeds(row)

    def actions(self, drivers):
        return rd.derive_actions(drivers)

    def narrative(self, row, drivers=None):
        self._activate()
        return rd.narrative(row, drivers if drivers is not None else self.drivers(row))

    def retrieval_text(self, row, drivers=None):
        self._activate()
        return rd.retrieval_text(row, drivers if drivers is not None else self.drivers(row))

    def verify(self, text, row, drivers):
        """True if the prose introduces no number absent from the source record.
        Point this at LLM output, which is where it earns its keep."""
        self._activate()
        return rd.verify_narrative(text, row, drivers)

    def explain(self, alert_id):
        """Everything about one alert, assembled - for the UI and the demo."""
        row = self.stage2.fetch_one(alert_id)
        drivers = self.drivers(row)
        narr = self.narrative(row, drivers)
        return {
            'alert_id': int(alert_id),
            'ruleset_version': self.ruleset_version,
            'drivers': drivers,
            'recommended_actions': self.actions(drivers),
            'narrative': narr,
            'retrieval_text': self.retrieval_text(row, drivers),
            'input_context': self.stage2.provenance(row),
            'graph_seeds': self.seeds(row),
            'unverified_tokens': self.verify(narr, row, drivers),
        }

    # -- batch ------------------------------------------------------------
    def build(self, df=None, alert_ids=None, limit=None):
        """Returns (explanations, driver_queries) - one row per alert, one row
        per driver."""
        self._activate()
        if df is None:
            df = self.stage2.fetch(alert_ids=alert_ids, limit=limit)
        return rd.build(df, self.frequencies())

    def write_hana(self, explanations, driver_queries=None,
                   update_alerts=False, replace=False):
        self._activate()
        return rd.write_hana(explanations, driver_queries,
                             update_alerts=update_alerts, replace=replace)

    def run(self, alert_ids=None, limit=None, to_hana=False,
            update_alerts=False, replace=False, out_dir=None):
        """Fetch, derive, and persist. Returns the two frames."""
        expl, drv = self.build(alert_ids=alert_ids, limit=limit)
        if to_hana:
            self.write_hana(expl, drv, update_alerts=update_alerts, replace=replace)
        if out_dir:
            out_dir = Path(out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            expl.to_csv(out_dir / 'alert_explanations.csv', index=False)
            drv.to_csv(out_dir / 'alert_driver_queries.csv', index=False)
        return expl, drv

    # -- internals --------------------------------------------------------
    def _activate(self):
        """Install this instance's configuration and caches into the functional
        module. The rule closures read them through it."""
        self.stage2._install()
        rd.S = self.schema
        rd.TABLE = self.table
        rd.DRIVER_TABLE = self.driver_table
        rd.BACKUP = self.backup_table
        rd.RULESET_VERSION = self.ruleset_version
        if self._pop is None:
            rd._POP = None
            self._pop = rd.population()
        rd._POP = self._pop
        if self._fit is None:
            rd._ALERT_FIT = None
            self._fit = rd.alert_fit()
        rd._ALERT_FIT = self._fit


# ==========================================================================
if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='Run the explainability pipeline.')
    ap.add_argument('--source', choices=['hana', 'local'], default='local')
    ap.add_argument('--schema', default=ep.S)
    ap.add_argument('--alert', type=int)
    ap.add_argument('--limit', type=int)
    ap.add_argument('--hana', action='store_true', help='persist to HANA')
    ap.add_argument('--update-alerts', action='store_true')
    ap.add_argument('--replace', action='store_true')
    ap.add_argument('--self-test', action='store_true')
    ap.add_argument('--rulebook', action='store_true')
    a = ap.parse_args()

    s2 = Stage2(schema=a.schema, source=a.source)
    s3 = Stage3(s2)
    print(s2)
    print(s3)

    if a.self_test:
        sys.exit(0 if s2.self_test() else 1)
    if a.rulebook:
        print()
        print(s3.rulebook().to_string(index=False))
        sys.exit(0)
    if a.alert:
        e = s3.explain(a.alert)
        print()
        print(e['narrative'])
        print()
        for d in e['drivers']:
            print('  [%3d] %-14s %s' % (d['severity'], d['category'], d['statement']))
        print()
        print('unverified tokens:', e['unverified_tokens'] or 'none')
        sys.exit(0)

    expl, drv = s3.run(limit=a.limit, to_hana=a.hana,
                       update_alerts=a.update_alerts, replace=a.replace,
                       out_dir=None if a.hana else ROOT)
    print('\nexplanations %s rows | driver queries %s rows'
          % (format(len(expl), ','), format(len(drv), ',')))
    print('narratives fully verified: %d of %d'
          % (int(expl['NARRATIVE_VERIFIED'].sum()), len(expl)))
