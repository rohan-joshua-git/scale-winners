"""Stage 5a - analytical queries over the ranked alert backlog.

Companion to pipeline/rag (stage 4). Stage 4 answers "why did this alert fire";
this answers "which alerts, how many, how bad" - a structured-query problem that
vector search cannot do and should not be asked to.
"""
from pipeline.stats.spec import QuerySpec, Filter, field_catalogue  # noqa: F401
from pipeline.stats.engine import StatsEngine                        # noqa: F401
