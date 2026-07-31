/**
 * Mock case data standing in for the real pipeline's POST /score response.
 *
 * Shape mirrors what pipeline/scoring + pipeline/explain would actually return:
 * a composite score, tier (from config/severity_matrix.yaml's likelihood x
 * severity matrix), the AHP factor breakdown (config/ahp_weights.yaml), and a
 * RAG-generated plain-English rationale (pipeline/rag). Where a real call
 * would go, it's marked below.
 *
 * REPLACE-WITH-API: each case object is what `const res = await fetch('/score', {
 *   method: 'POST', body: JSON.stringify(transaction)
 * }).then(r => r.json())` would return from the FastAPI scoring service.
 */

const TRUSTSPHERE_CASES = [
  {
    id: 'sanctions',
    typology: 'sanctions_exposure',
    tierLabel: 'Sanctions exposure',
    corridor: 'Istanbul → Tehran',
    transaction: {
      id: 'TXN-88214-KL',
      amountUsd: 4_180_000,
      currency: 'USD',
      originator: 'Anadolu Trade Partners Ltd',
      originatingCountry: 'Turkey',
      beneficiary: 'Pasargad Commercial Est.',
      destinationCountry: 'Iran',
      entityType: 'Correspondent bank',
      initiatedAt: '2026-07-28T03:14:00Z',
    },
    score: 97,
    tier: 'Critical',
    hardOverride: true,
    preOverrideScore: 71,
    factors: [
      { key: 'geography_risk', label: 'Geography risk', weight: 0.30, contribution: 0.98, detail: 'Destination resolves to a sanctioned jurisdiction (Iran).' },
      { key: 'entity_type_risk', label: 'Counterparty type', weight: 0.15, contribution: 0.30, detail: 'Correspondent-banking relationship, elevated baseline risk.' },
      { key: 'velocity_vs_baseline', label: 'Velocity vs. baseline', weight: 0.25, contribution: 0.41, detail: 'Within normal range for this originator.' },
      { key: 'kyc_completeness', label: 'KYC completeness', weight: 0.20, contribution: 0.22, detail: 'Beneficiary KYC file is 3 fields short of complete.' },
      { key: 'size_percentile', label: 'Size percentile', weight: 0.10, contribution: 0.58, detail: '84th percentile for this corridor.' },
    ],
    rationale: [
      'Destination entity resolves to Iran, a sanctioned jurisdiction on the active screening list.',
      'This is a hard override: any sanctions-list hit routes directly to Critical, regardless of the composite score.',
      'Composite AHP score before override was 71 / 100, driven mainly by geography risk and a correspondent-banking counterparty.',
      'Recommended action: freeze pending manual sanctions confirmation and file within 24 hours.',
    ],
  },
  {
    id: 'layering',
    typology: 'layering',
    tierLabel: 'Layering — GARCH-flagged corridor',
    corridor: 'Singapore → Frankfurt',
    transaction: {
      id: 'TXN-90067-QN',
      amountUsd: 2_340_000,
      currency: 'USD',
      originator: 'Meridian Bay Holdings Pte Ltd',
      originatingCountry: 'Singapore',
      beneficiary: 'Rheinallee Logistik GmbH',
      destinationCountry: 'Germany',
      entityType: 'SME',
      initiatedAt: '2026-07-27T14:02:00Z',
    },
    score: 78,
    tier: 'High',
    hardOverride: false,
    factors: [
      { key: 'velocity_vs_baseline', label: 'Velocity vs. baseline', weight: 0.25, contribution: 0.93, detail: '4.1x the 90-day baseline; 11 transfers in 36 hours.' },
      { key: 'geography_risk', label: 'Geography risk', weight: 0.30, contribution: 0.08, detail: 'Both endpoints are low-risk jurisdictions.' },
      { key: 'entity_type_risk', label: 'Counterparty type', weight: 0.15, contribution: 0.40, detail: 'SME, standard baseline risk.' },
      { key: 'kyc_completeness', label: 'KYC completeness', weight: 0.20, contribution: 0.15, detail: 'Complete on both sides.' },
      { key: 'size_percentile', label: 'Size percentile', weight: 0.10, contribution: 0.71, detail: '91st percentile for this corridor.' },
    ],
    rationale: [
      'Transfer volume across the Singapore–Frankfurt corridor is 4.1x this originator’s 90-day baseline.',
      'A GARCH(1,1) volatility model flags the spike as a 3.2-sigma deviation — consistent with layering, where funds move rapidly to obscure their origin.',
      'Geography risk is low for both endpoints; velocity and transaction timing are what drive the tier here, not the corridor itself.',
      'Recommended action: escalate for analyst review of the full transaction chain before the next settlement cycle.',
    ],
  },
  {
    id: 'shell',
    typology: 'shell_company',
    tierLabel: 'Shell-company network',
    corridor: 'London → Road Town, BVI',
    transaction: {
      id: 'TXN-91325-FZ',
      amountUsd: 6_750_000,
      currency: 'USD',
      originator: 'Blackwell Regent Capital LLP',
      originatingCountry: 'United Kingdom',
      beneficiary: 'Solvay Crest Holdings Ltd',
      destinationCountry: 'British Virgin Islands',
      entityType: 'Shell company (suspected)',
      initiatedAt: '2026-07-29T09:41:00Z',
    },
    score: 92,
    tier: 'Critical',
    hardOverride: false,
    factors: [
      { key: 'entity_type_risk', label: 'Counterparty type', weight: 0.15, contribution: 1.00, detail: 'Suspected shell company — maxed under current AHP weighting.' },
      { key: 'size_percentile', label: 'Size percentile', weight: 0.10, contribution: 0.96, detail: '96th percentile for this corridor.' },
      { key: 'geography_risk', label: 'Geography risk', weight: 0.30, contribution: 0.35, detail: 'Destination is a secrecy-jurisdiction registry, not a sanctioned one.' },
      { key: 'kyc_completeness', label: 'KYC completeness', weight: 0.20, contribution: 0.62, detail: 'Beneficial-ownership fields largely unresolved.' },
      { key: 'velocity_vs_baseline', label: 'Velocity vs. baseline', weight: 0.25, contribution: 0.20, detail: 'Single transaction, no baseline pattern yet.' },
    ],
    rationale: [
      'Beneficial-ownership graph traversal links the beneficiary to 3 other shell entities registered within 11 days of each other.',
      'All 4 entities share a single ultimate beneficial owner, surfaced through the graph traversal rather than any single flagged field.',
      'Entity-type risk is maxed for suspected shell companies; combined with a 96th-percentile transaction size, both likelihood and severity land high — independent of any sanctions hit.',
      'Recommended action: route to Level 2 investigation for beneficial-ownership verification before funds clear.',
    ],
    graph: {
      nodes: [
        { id: 'originator', label: 'Blackwell Regent\nCapital LLP', kind: 'originator' },
        { id: 'beneficiary', label: 'Solvay Crest\nHoldings Ltd', kind: 'shell' },
        { id: 'shell2', label: 'Corvid Peak\nTrading Ltd', kind: 'shell' },
        { id: 'shell3', label: 'Ashgrove Meridian\nLtd', kind: 'shell' },
        { id: 'shell4', label: 'Fenwick Straits\nHoldings Ltd', kind: 'shell' },
        { id: 'ubo', label: 'Ultimate beneficial\nowner (individual)', kind: 'ubo' },
      ],
      edges: [
        { from: 'originator', to: 'beneficiary', label: 'transfers to' },
        { from: 'beneficiary', to: 'ubo', label: 'controlled by' },
        { from: 'shell2', to: 'ubo', label: 'controlled by' },
        { from: 'shell3', to: 'ubo', label: 'controlled by' },
        { from: 'shell4', to: 'ubo', label: 'controlled by' },
      ],
    },
  },
];

const TRUSTSPHERE_DEFAULT_CASE_ID = 'shell';
