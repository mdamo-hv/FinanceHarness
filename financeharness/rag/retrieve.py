"""Retrieval — hybrid BM25 + vector search over the corpus.

Two rankers see the same query. BM25 is exact: it finds ``CVE-2021-44228`` and
``T1059.001`` because those strings are literally in the text, which is most of
what a security corpus is asked for. Vector search is approximate in the useful
sense: it finds the persistence technique when the question says "keep access
after reboot" and never uses the word persistence. Neither one wins often enough
to drop the other.

The two ranked lists are fused with Reciprocal Rank Fusion — each list votes
``1 / (K + rank)`` for its hits and the votes are summed. RRF compares *ranks*,
not scores, so an FTS5 BM25 value and a cosine similarity combine without
calibrating one against the other; that scale-free property is why it is the
default fusion for hybrid search.

With no embedder configured this degrades to plain BM25 — still real retrieval,
just lexical.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from financeharness.rag.config import load_rag_config
from financeharness.rag.embeddings import EmbeddingError, cosine_ranking

# RRF's rank-smoothing constant. 60 is the value from the original paper and the
# de-facto default; it damps the top of each list so one ranker's #1 can't
# dominate a result the other ranker never saw.
_RRF_K = 60


@dataclass
class RetrievalResult:
  """Ranked passages plus how they were found (for the tool's `meta`)."""

  passages: list = field(default_factory=list)
  lexical_hits: int = 0
  vector_hits: int = 0
  mode: str = "lexical"
  notes: list = field(default_factory=list)


def _rrf(ranked_lists):
  """Fuse ranked id lists into ``{id: score}`` by Reciprocal Rank Fusion."""
  fused = {}
  for ids in ranked_lists:
    for rank, chunk_id in enumerate(ids):
      fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (_RRF_K + rank + 1)
  return fused


async def retrieve(
    store,
    query,
    *,
    k=None,
    config=None,
    embedder=None,
    sources=None,
    candidate_k=None,
):
  """Top-``k`` passages for ``query``. Hybrid when an embedder is given."""
  config = config or load_rag_config()
  k = k or config.top_k
  depth = candidate_k or max(config.candidate_k, k)
  notes = []

  lexical = store.search_lexical(query, depth, sources=sources)
  vector_ranked = []
  if embedder is not None:
    try:
      query_vec = (await embedder.embed([query]))[0]
      vector_ranked = cosine_ranking(
          query_vec, store.iter_vectors(embedder.name, sources=sources), depth
      )
      if not vector_ranked:
        notes.append(
            f"no vectors stored for {embedder.name} — run `fh rag embed`"
        )
    except EmbeddingError as exc:
      notes.append(f"vector search unavailable: {exc}")

  if not vector_ranked:
    passages = lexical[:k]
    return RetrievalResult(
        passages=passages,
        lexical_hits=len(lexical),
        mode="lexical",
        notes=notes,
    )

  fused = _rrf(
      [[p.chunk_id for p in lexical], [cid for cid, _ in vector_ranked]]
  )
  order = sorted(fused, key=lambda cid: fused[cid], reverse=True)[:k]
  return RetrievalResult(
      passages=store.passages_by_id(order, scores=fused),
      lexical_hits=len(lexical),
      vector_hits=len(vector_ranked),
      mode="hybrid",
      notes=notes,
  )


def format_passages(passages, *, max_chars=1200):
  """Render passages as numbered, attributed markdown for the model to read."""
  if not passages:
    return "No passages matched."
  blocks = []
  for i, p in enumerate(passages, 1):
    text = p.text if len(p.text) <= max_chars else p.text[:max_chars] + " …"
    label = p.title or p.url
    blocks.append(
        f"### [{i}] {label}\n"
        f"Source: `{p.source}` · {p.url} · score {p.score:.4f}\n\n{text}"
    )
  return "\n\n".join(blocks)
