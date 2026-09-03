"""Chunking — split a fetched document into retrievable passages.

Retrieval returns chunks, and the model reads them, so a chunk has to be a
self-contained piece of prose: big enough to answer a question, small enough
that the answer isn't buried. The splitter packs whole paragraphs up to
``chunk_chars``, breaks over-long paragraphs on sentence boundaries, and carries
``overlap`` characters of tail context into the next chunk so a fact split
across a boundary is still recoverable from one side of it.
"""

from __future__ import annotations

import re

_PARAGRAPH_RE = re.compile(r"\n\s*\n+")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_WS_RE = re.compile(r"[ \t]+")


def normalize(text):
  """Collapse runs of spaces/blank lines so chunk sizes reflect real content."""
  text = text.replace("\r\n", "\n").replace("\r", "\n")
  text = _WS_RE.sub(" ", text)
  return re.sub(r"\n{3,}", "\n\n", text).strip()


def _split_long(block, limit):
  """Break a paragraph longer than ``limit`` on sentence boundaries."""
  out, current = [], ""
  for sentence in _SENTENCE_RE.split(block):
    if not sentence:
      continue
    if current and len(current) + len(sentence) + 1 > limit:
      out.append(current)
      current = sentence
    elif len(sentence) > limit:
      # A single sentence over the limit (minified text, a table row): cut it.
      if current:
        out.append(current)
        current = ""
      out += [sentence[i : i + limit] for i in range(0, len(sentence), limit)]
    else:
      current = f"{current} {sentence}".strip()
  if current:
    out.append(current)
  return out


def chunk_text(text, *, chunk_chars=1200, overlap=200, min_chars=80):
  """Split ``text`` into overlapping passages.

  Paragraphs are packed greedily up to ``chunk_chars``; the last ``overlap``
  characters of a chunk are prefixed to the next one (so a chunk carrying an
  overlap can reach ``chunk_chars + overlap``). Fragments shorter than
  ``min_chars`` are dropped unless they are the only chunk — a stub document
  (a KEV entry, a one-line advisory) should still be retrievable.
  """
  text = normalize(text)
  if not text:
    return []
  chunk_chars = max(chunk_chars, 200)
  overlap = max(0, min(overlap, chunk_chars // 2))

  blocks = []
  for para in _PARAGRAPH_RE.split(text):
    para = para.strip()
    if not para:
      continue
    blocks += [para] if len(para) <= chunk_chars else _split_long(para, chunk_chars)

  chunks, current = [], ""
  for block in blocks:
    if current and len(current) + len(block) + 2 > chunk_chars:
      chunks.append(current)
      tail = current[-overlap:] if overlap else ""
      current = f"{tail}\n\n{block}".strip() if tail else block
    else:
      current = f"{current}\n\n{block}".strip() if current else block
  if current:
    chunks.append(current)

  kept = [c for c in chunks if len(c) >= min_chars]
  return kept or chunks[:1]
