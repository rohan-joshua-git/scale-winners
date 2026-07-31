"""Entity dedup: the same clause/jurisdiction/program mentioned across multiple
source documents should collapse to one graph node. Dedup key is a normalized
label, so canonical_node_id() is deterministic -- the same label (or known alias)
always maps to the same NODE_ID regardless of which document or chunk produced it.

This is a lightweight heuristic (exact-normalized-match + a small alias table),
not fuzzy/embedding-based entity resolution -- adequate for 6 documents worth of
jurisdiction and sanctions-program names, not a general-purpose solution. Flagged
in the README as the first thing to revisit at real corpus scale.
"""
import re

# Known aliases -> canonical label, for entities that recur across our 6 documents
# under different names. Not exhaustive -- extend as extraction surfaces more.
_ALIASES = {
    "dprk": "North Korea (DPRK)",
    "north korea": "North Korea (DPRK)",
    "democratic people's republic of korea": "North Korea (DPRK)",
    "bvi": "British Virgin Islands",
    "virgin islands (uk)": "British Virgin Islands",
    "the virgin islands (uk)": "British Virgin Islands",
    "uae": "United Arab Emirates",
    "islamic republic of iran": "Iran",
    "russian federation": "Russia",
    "republic of the union of myanmar": "Myanmar",
    "burma": "Myanmar",
    "us": "United States",
    "usa": "United States",
    "u.s.": "United States",
    "u.s.a.": "United States",
    "uk": "United Kingdom",
    "u.k.": "United Kingdom",
}


def _normalize_key(label: str) -> str:
    key = label.strip().casefold()
    key = re.sub(r"[^\w\s()-]", "", key)
    key = re.sub(r"\s+", " ", key).strip()
    return key


def canonical_label(label: str) -> str:
    """Resolve known aliases to a single display label; otherwise return the
    input label as-is (trimmed)."""
    key = _normalize_key(label)
    return _ALIASES.get(key, label.strip())


def canonical_node_id(node_type: str, label: str) -> str:
    """Deterministic ID: same (type, label-or-alias) always maps to the same ID,
    regardless of source document or original casing/punctuation."""
    key = _normalize_key(canonical_label(label))
    return f"{node_type}:{key}"
