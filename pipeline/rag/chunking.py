"""Paragraph-aware chunker. Packs whole paragraphs up to a target character
budget; splits any single paragraph that alone exceeds the budget on sentence
boundaries. Regulatory source docs (see data/regulatory_docs/) tend to have one
paragraph per jurisdiction/clause after cleaning, which makes this a natural
chunk-per-clause split most of the time.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from pipeline.rag import config

_SENTENCE_SPLIT = re.compile(r"(?<=[.;])\s+")


@dataclass
class Chunk:
    doc_name: str
    section: str
    index: int
    text: str


def _split_long_paragraph(para: str, target_chars: int) -> list[str]:
    sentences = _SENTENCE_SPLIT.split(para)
    pieces, current = [], ""
    for sent in sentences:
        if current and len(current) + len(sent) + 1 > target_chars:
            pieces.append(current.strip())
            current = sent
        else:
            current = f"{current} {sent}".strip()
    if current:
        pieces.append(current.strip())
    return pieces


def chunk_text(text: str, doc_name: str, section: str = "",
                target_chars: int = config.CHUNK_TARGET_CHARS,
                overlap_chars: int = config.CHUNK_OVERLAP_CHARS) -> list[Chunk]:
    paragraphs = [p.strip() for p in re.split(r"\n{1,}", text) if p.strip()]

    packed: list[str] = []
    current = ""
    for para in paragraphs:
        candidate = f"{current}\n{para}".strip() if current else para
        if len(candidate) <= target_chars:
            current = candidate
            continue
        if current:
            packed.append(current)
        if len(para) > target_chars:
            packed.extend(_split_long_paragraph(para, target_chars))
            current = ""
        else:
            current = para
    if current:
        packed.append(current)

    # Apply a small overlap by prefixing each chunk with the tail of the previous one
    chunks = []
    prev_tail = ""
    for i, body in enumerate(packed):
        full = f"{prev_tail}\n{body}".strip() if prev_tail else body
        chunks.append(Chunk(doc_name=doc_name, section=section, index=i, text=full))
        prev_tail = body[-overlap_chars:] if overlap_chars else ""
    return chunks


def chunk_file(path, doc_name: str) -> list[Chunk]:
    text = path.read_text(encoding="utf-8")
    return chunk_text(text, doc_name=doc_name)
