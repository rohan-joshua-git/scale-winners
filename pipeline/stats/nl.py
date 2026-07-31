"""Stage 5a - natural language in, validated QuerySpec out.

The language model's entire job is to choose a spec. It never sees a number it
did not receive from engine.StatsEngine, and it never emits SQL. Two guards make
that stick:

  1. The prompt is generated from spec.field_catalogue(), so the vocabulary the
     model is shown is the vocabulary the validator enforces. Prose lists of
     column names drift away from the code; a generated one cannot.
  2. Output is parsed into QuerySpec by pydantic. A hallucinated column, an
     invented operator or an injected SQL fragment fails validation and we fall
     back or clarify - it cannot reach the executor.

There is a deterministic rule-based extractor too (`rule_spec`). It is not a
toy: it covers the demo path with no network call and no tenant credentials, and
it is the fallback when AI Core is unreachable. `extract_spec` prefers the LLM
and falls back to rules.

The third outcome is `clarify`. "Give me the highest priority cases" is
answerable with a stated assumption; "show me the bad ones" is not. Guessing at
the second is how a compliance chatbot ends up confidently wrong, so it asks.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field as dc_field

from pydantic import ValidationError

from pipeline.stats import spec as S
from pipeline.stats.spec import Filter, QuerySpec

# --------------------------------------------------------------------------
# result type
# --------------------------------------------------------------------------

@dataclass
class Extraction:
    spec: QuerySpec | None
    assumptions: list[str] = dc_field(default_factory=list)
    clarify: str | None = None
    method: str = "rules"          # 'llm' | 'rules' | 'llm->rules'

    @property
    def ok(self) -> bool:
        return self.spec is not None


# --------------------------------------------------------------------------
# intent routing (stats vs GraphRAG)
# --------------------------------------------------------------------------

_EXPLANATORY = re.compile(
    r"\b(why|explain|justif\w+|rationale|reason for|what regulation|which clause|"
    r"which rule|cite|citation|regulator\w*|fatf say|guidance|basis for)\b", re.I)
_ANALYTICAL = re.compile(
    r"\b(how many|count|total|average|avg|median|sum|breakdown|distribution|"
    r"top|highest|lowest|most|least|list|show me|which cases|which alerts|"
    r"rank|queue|backlog|per |by region|group)\b", re.I)


def classify_intent(message: str) -> str:
    """'analytical' -> this module; 'explanatory' -> pipeline.rag.retrieve.

    Deliberately lexical rather than a model call: it is on the hot path of
    every message, the two classes are lexically well separated, and a wrong
    route is visible and cheap to correct. Ties go to analytical because the
    stats path reports what it matched, so a misroute is self-evident.
    """
    if _EXPLANATORY.search(message) and not _ANALYTICAL.search(message):
        return "explanatory"
    if _ANALYTICAL.search(message):
        return "analytical"
    if re.search(r"\balert\s*\d+\b|\bcase\s*\d+\b", message, re.I):
        return "explanatory"
    return "analytical"


# --------------------------------------------------------------------------
# deterministic extractor
# --------------------------------------------------------------------------

# "by X" is NOT an aggregate signal on its own - it is ambiguous between
# grouping ("how many by region") and ordering ("give me the cases by priority"),
# and treating it as grouping turned "give me the cases ... by alert priority"
# into a three-row breakdown. The disambiguator is whether an aggregate verb is
# present, so grouping cues and ordering cues are now detected separately.
_AGG_STRONG = re.compile(r"\b(how many|count of|counts?\b|number of|breakdown|"
                         r"distribution|grouped by|broken down by|group by|per)\b", re.I)
_AGG_WEAK = re.compile(r"\b(total|sum|average|avg|mean|median)\b", re.I)
_LIST_CUE = re.compile(r"\b(give me|show me|list|which (?:cases?|alerts?)|"
                       r"the (?:cases?|alerts?)|top \d+|first \d+|pull up|find me)\b", re.I)
#: "by <field>" / "ordered by <field>" - the field the user wants applied.
_BY_PHRASE = re.compile(
    r"\b(?:ordered by|sorted by|sort by|ranked by|rank by|order by|grouped by|"
    r"broken down by|group by|by|per)\s+([a-z_][a-z_ ]{1,29})", re.I)
_ASC = re.compile(r"\b(lowest|smallest|least|oldest|bottom|ascending|asc)\b", re.I)
_LIMIT = re.compile(r"\b(?:top|first|show me|give me)\s+(\d{1,3})\b", re.I)
_GROUP_BY = re.compile(r"\b(?:by|per|grouped by|broken down by|breakdown by)\s+([a-z ]{3,25})", re.I)
_UNASSIGNED = re.compile(r"\b(unassigned|nobody|no one|not assigned|unowned)\b", re.I)
_SLA = re.compile(r"\b(sla|overdue|breached|past due)\b", re.I)
_OVER_DAYS = re.compile(r"\b(?:over|more than|older than|>)\s*(\d+)\s*(day|days|month|months|year|years)\b", re.I)
_OVER_USD = re.compile(r"\b(?:over|above|more than|>)\s*\$?\s*([\d,.]+)\s*(k|m|million|thousand)?\b", re.I)

_DAY_MULT = {"day": 1, "days": 1, "month": 30, "months": 30, "year": 365, "years": 365}
_USD_MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6}


def _value_matches(text: str) -> list[Filter]:
    """Find closed-domain values named anywhere in the question.

    Scans the registry's own value_aliases, longest phrase first so "latin
    america" wins over "america". Only closed-domain fields participate - free
    text fields would produce false positives on ordinary words.
    """
    out, low, claimed = [], text.lower(), []
    cands = []
    for fname, fd in S.DIMENSIONS.items():
        for alias, canon in fd.value_aliases.items():
            cands.append((alias, fname, canon))
        for v in fd.values:
            cands.append((v.lower(), fname, v))
    for alias, fname, canon in sorted(cands, key=lambda c: -len(c[0])):
        if not re.search(r"(?<![a-z])%s(?![a-z])" % re.escape(alias), low):
            continue
        span = low.find(alias)
        if any(span < e and s < span + len(alias) for s, e in claimed):
            continue
        claimed.append((span, span + len(alias)))
        if not any(f.field == fname and f.value == canon for f in out):
            out.append(Filter(field=fname, op="eq", value=canon))
    return out


def _by_fields(text: str) -> list[str]:
    """Fields named after 'by' / 'ordered by' / 'per', longest phrase first.

    Returned in the order they appear so the caller can decide whether they are
    grouping or ordering - the phrase itself does not say.
    """
    out = []
    for gm in _BY_PHRASE.finditer(text):
        words = gm.group(1).split()
        for n in range(min(4, len(words)), 0, -1):
            r = S.resolve_field(" ".join(words[:n]))
            if r and r not in out:
                out.append(r)
                break
    return out


def rule_spec(message: str) -> Extraction:
    """Best-effort spec with no network call. Reports its assumptions."""
    m = message.strip()
    low = m.lower()
    assumptions: list[str] = []
    filters = _value_matches(m)
    by_fields = _by_fields(low)

    if _UNASSIGNED.search(low):
        filters.append(Filter(field="ASSIGNED_TO", op="is_null"))
    if _SLA.search(low):
        filters.append(Filter(field="SLA_BREACHED", op="eq", value=True))

    md = _OVER_DAYS.search(low)
    if md:
        filters.append(Filter(field="age_days", op="gte",
                              value=float(md.group(1)) * _DAY_MULT[md.group(2)]))
    mu = _OVER_USD.search(low)
    if mu and ("$" in m or "usd" in low or "amount" in low or "value" in low):
        amt = float(mu.group(1).replace(",", "")) * _USD_MULT.get((mu.group(2) or "").lower(), 1)
        filters.append(Filter(field="AMOUNT_USD", op="gte", value=amt))

    # -- intent ----------------------------------------------------------
    # An aggregate verb decides this, never a bare "by". "Give me the cases ...
    # by alert priority" is a list ordered by a label, not a breakdown.
    if _AGG_STRONG.search(low):
        intent = "aggregate"
    elif _AGG_WEAK.search(low) and not _LIST_CUE.search(low):
        intent = "aggregate"
    else:
        intent = "list"

    group_by = []
    if intent == "aggregate":
        for r in by_fields:
            if r in S.GROUPABLE and r not in group_by:
                group_by.append(r)

    # -- ordering --------------------------------------------------------
    order_by, descending, explicit_order = "exposure", not bool(_ASC.search(low)), False
    if intent == "list":
        for r in by_fields:
            if r in S.ORDERABLE:
                order_by, explicit_order = r, True     # the user named a field
                break

    if not explicit_order and intent == "list":
        # Only a list has an ordering the user can be ambiguous about. Running
        # this for aggregates double-reported the region assumption, because the
        # group_by loop below already covers it.
        for phrase, (target, note) in S.AMBIGUOUS.items():
            if re.search(r"(?<![a-z])%s" % re.escape(phrase), low):
                if target in S.ORDERABLE:
                    order_by = target
                assumptions.append(note)
                break
        else:
            for term in ("amount", "value", "age", "oldest", "newest", "exposure"):
                if term in low and (r := S.resolve_field(term)) in S.MEASURES:
                    order_by = r
                    break
        if re.search(r"\boldest\b", low):
            order_by, descending = "age_days", True
        if re.search(r"\bnewest\b|\bmost recent\b", low):
            order_by, descending = "age_days", False
    elif order_by in S.ORDINAL_DIMENSIONS:
        assumptions.append(
            "Ordered by the %s label (%s); ties within a level broken by "
            "exposure, since the label alone does not order alerts inside it."
            % (order_by, " > ".join(S.ORDINAL_DIMENSIONS[order_by].ordinal)))

    # An explicitly-named grouping field is not an ambiguity we resolved, but
    # a vague one ("by region") still is.
    for r in group_by:
        for phrase, (target, note) in S.AMBIGUOUS.items():
            if target == r and re.search(r"(?<![a-z])%s(?![a-z_])" % re.escape(phrase), low):
                assumptions.append(note)
                break

    # -- metrics ---------------------------------------------------------
    metrics = ["count"]
    if intent == "aggregate":
        if re.search(r"\btotal\b|\bsum\b", low) and re.search(r"amount|value|usd|\$", low):
            metrics = ["count", "sum_amount_usd"]
        elif re.search(r"\baverage\b|\bavg\b|\bmean\b", low):
            metrics = ["count", "avg_exposure", "avg_age_days"]
        elif re.search(r"\bmedian\b", low):
            metrics = ["count", "median_age_days"]
        else:
            metrics = ["count", "pct_of_backlog", "avg_exposure"]
        # Under a filter, "% of backlog" is the wrong denominator for the
        # question the reader is asking, so report both rather than swapping.
        if filters and "pct_of_backlog" in metrics:
            metrics.insert(metrics.index("pct_of_backlog") + 1, "pct_of_matched")

    ml = _LIMIT.search(low)
    limit = int(ml.group(1)) if ml else (50 if intent == "aggregate" else 20)

    allocation = "banded" if re.search(
        r"\bwork on\b|\bwhat should i\b|\btoday\b|\bqueue\b|\bassign\b", low) else "flat"
    if allocation == "banded":
        assumptions.append(
            "Used banded capacity allocation (10/20/70 across NEW/CURRENT/AGED) "
            "rather than a flat top-N, so new critical alerts are not starved by "
            "the aged cohort.")

    try:
        s = QuerySpec(intent=intent, filters=filters, group_by=group_by,
                      metrics=metrics, order_by=order_by, descending=descending,
                      limit=min(limit, 500), allocation=allocation)
    except ValidationError as e:
        return Extraction(spec=None, clarify="Could not build a valid query: %s" % e)
    return Extraction(spec=s, assumptions=list(dict.fromkeys(assumptions)),
                      method="rules")


# --------------------------------------------------------------------------
# LLM extractor
# --------------------------------------------------------------------------

_SYSTEM = """You convert an analyst's question about an anti-money-laundering \
alert backlog into a JSON query spec. You never answer the question and never \
invent numbers - you only choose the query.

The backlog contains UNRESOLVED alerts only. Do not accept a filter on resolved \
status; if the user asks about resolved or closed alerts, return a clarify.

Return ONLY a JSON object, no prose, no code fence, with this shape:
{
  "intent": "list" | "aggregate",
  "filters": [{"field": <field>, "op": <op>, "value": <value>}],
  "group_by": [<dimension>],
  "metrics": [<metric>],
  "order_by": <measure>,
  "descending": true|false,
  "limit": <1-500>,
  "allocation": "flat" | "banded",
  "assumptions": [<short strings explaining any reading you had to choose>],
  "clarify": <string question, ONLY if the request is too vague to express here>
}

Rules that matter:
- "priority", "risk", "urgency", "most important" mean order_by "exposure" (the \
composite score), NOT the ALERT_PRIORITY label. Say so in assumptions. Use \
ALERT_PRIORITY only when the user names the label or says "severity".
- "region" means CLIENT_REGION_CODE (client HQ) unless the user clearly means \
the counterparty/destination, which is DEST_REGION_CODE. Say which you chose.
- "by X" is ambiguous. If the question asks for CASES or ALERTS, "by X" means \
order_by X and intent stays "list". Only use group_by when the question asks \
how many / a count / a breakdown / a distribution / an average.
- order_by accepts ordered categoricals (ALERT_PRIORITY, DEST_FATF_STATUS, \
DEST_RISK_TIER, band) as well as numeric measures. Ties inside a level are \
broken by exposure automatically - do not work around that.
- When the request has filters and you are producing a breakdown, include both \
"pct_of_backlog" and "pct_of_matched": under a filter they answer different \
questions and only pct_of_matched sums to 100.
- Use allocation "banded" when the user is asking what to WORK ON (a queue, \
today's work, assignment), because a flat top-N starves new critical alerts.
- group_by is only valid with intent "aggregate".
- If a needed field is not in the catalogue below, return clarify rather than \
substituting a different field.

Field catalogue (the only fields, ops and metrics that exist):
%s
"""

_FEWSHOT = [
    ("Give me the cases with the highest priority",
     {"intent": "list", "filters": [], "order_by": "exposure", "descending": True,
      "limit": 20, "allocation": "flat",
      "assumptions": ["Ranked by composite exposure score, not the ALERT_PRIORITY label."]}),
    ("How many unresolved alerts in EMEA, broken down by FATF status?",
     {"intent": "aggregate",
      "filters": [{"field": "CLIENT_REGION_CODE", "op": "eq", "value": "EMEA"}],
      "group_by": ["DEST_FATF_STATUS"],
      "metrics": ["count", "pct_of_backlog", "pct_of_matched", "avg_exposure"],
      "limit": 50, "assumptions": ["'EMEA' read as client HQ region."]}),
    ("Give me the cases with the highest priority in EMEA by alert priority",
     {"intent": "list",
      "filters": [{"field": "CLIENT_REGION_CODE", "op": "eq", "value": "EMEA"}],
      "group_by": [], "order_by": "ALERT_PRIORITY", "descending": True, "limit": 20,
      "assumptions": ["Ordered by the ALERT_PRIORITY label since you named it; "
                      "ties within a level broken by exposure."]}),
    ("What should my team work on today?",
     {"intent": "list", "filters": [], "order_by": "exposure", "descending": True,
      "limit": 20, "allocation": "banded",
      "assumptions": ["Banded capacity allocation so new criticals are not starved."]}),
    ("Show me the bad ones",
     {"clarify": "Which sense of 'bad' - highest exposure score, oldest, "
                 "largest value, or on a blacklisted jurisdiction?"}),
]


def _prompt(message: str) -> list[dict]:
    cat = json.dumps(S.field_catalogue(), indent=1)
    msgs = [{"role": "system", "content": _SYSTEM % cat}]
    for q, a in _FEWSHOT:
        msgs.append({"role": "user", "content": q})
        msgs.append({"role": "assistant", "content": json.dumps(a)})
    msgs.append({"role": "user", "content": message})
    return msgs


def _parse(raw: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("no JSON object in model output")
    return json.loads(raw[start:end + 1])


def llm_spec(message: str) -> Extraction:
    from pipeline.rag import ai_core_client        # imported lazily: needs credentials

    body = _parse(ai_core_client.complete(_prompt(message), max_tokens=600,
                                          temperature=0.0).content)
    if body.get("clarify"):
        return Extraction(spec=None, clarify=body["clarify"], method="llm")
    assumptions = [str(a) for a in body.pop("assumptions", []) if a]
    body.pop("clarify", None)
    return Extraction(spec=QuerySpec(**body), assumptions=assumptions, method="llm")


def extract_spec(message: str, use_llm: bool = True) -> Extraction:
    """LLM first, deterministic rules as the fallback.

    A ValidationError here is the guard working, not a bug: the model proposed
    something outside the contract and we decline to run it.
    """
    if use_llm:
        try:
            return llm_spec(message)
        except (ValidationError, ValueError, KeyError, TypeError) as e:
            out = rule_spec(message)
            out.method = "llm->rules"
            out.assumptions.append(
                "Model output failed schema validation (%s); fell back to the "
                "deterministic extractor." % type(e).__name__)
            return out
        except Exception as e:                        # transport/auth/deployment
            out = rule_spec(message)
            out.method = "llm->rules"
            detail = str(e).strip() or type(e).__name__
            if isinstance(e, FileNotFoundError):
                detail = ("team_07_credentials.json not found - AI Core needs the "
                          "gitignored tenant credentials in the repo root")
            out.assumptions.append(
                "AI Core unavailable (%s); used the deterministic extractor."
                % detail[:140])
            return out
    return rule_spec(message)


# --------------------------------------------------------------------------
# narration
# --------------------------------------------------------------------------

_NARRATE_SYSTEM = """You are a compliance analytics assistant. You will be given \
a question, the query that was run, and the RESULT ROWS already computed by a \
deterministic engine.

State the answer in at most four sentences of plain English. Use ONLY numbers \
present in the result payload - never estimate, extrapolate or recompute. If \
assumptions are listed, state the relevant one plainly. If n_matched is 0, say \
nothing matched and suggest a nearby filter to relax. Do not add caveats about \
AI or data quality."""


def narrate(message: str, result: dict, assumptions: list[str]) -> str:
    from pipeline.rag import ai_core_client

    payload = {"question": message, "query": result["spec_english"],
               "n_matched": result["n_matched"], "assumptions": assumptions,
               "rows": result["rows"][:15],
               "exposure_weights": result["provenance"]["exposure_weights"]}
    msgs = [{"role": "system", "content": _NARRATE_SYSTEM},
            {"role": "user", "content": json.dumps(payload, default=str)}]
    return ai_core_client.complete(msgs, max_tokens=300, temperature=0.0).content.strip()


def narrate_fallback(result: dict, assumptions: list[str]) -> str:
    """Deterministic answer sentence, used when AI Core is unavailable.

    Not a degraded mode anyone should be embarrassed by - it says exactly what
    was matched and on what basis.
    """
    n = result["n_matched"]
    if n == 0:
        return ("No unresolved alerts match %s. Try relaxing one filter."
                % result["spec_english"])
    head = "%d unresolved alert%s match %s; showing %d." % (
        n, "" if n == 1 else "s", result["spec_english"], result["n_returned"])
    rows = result["rows"]
    if result["spec"]["intent"] == "list" and rows:
        top = rows[0]
        head += (" Highest exposure is alert %s at %.3f (%s, %s, %s days old)."
                 % (top.get("ALERT_ID"), top.get("exposure", 0),
                    top.get("ALERT_PRIORITY"), top.get("DEST_FATF_STATUS"),
                    top.get("age_days")))
    if assumptions:
        head += " " + " ".join(assumptions)
    return head
