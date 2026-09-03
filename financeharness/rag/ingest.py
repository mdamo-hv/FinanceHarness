"""Ingestion — fetch sources, chunk them, store them, embed them.

The pipeline is deliberately linear: a source yields documents, each document is
normalized and split into passages, the passages are upserted under the
document's URL, and (when embeddings are configured) any chunk still missing a
vector is embedded. Every stage reports rather than raises, so ingesting eight
sources where one is down produces seven sources' worth of corpus and a named
error for the eighth.

Embedding is a separate, resumable pass over "chunks without a vector for this
model", which means an interrupted ingest resumes cheaply, and turning
embeddings on later backfills the corpus you already have instead of re-fetching
the internet.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from financeharness.rag.chunking import chunk_text
from financeharness.rag.config import load_rag_config
from financeharness.rag.embeddings import EmbeddingError
from financeharness.rag.sources import FetchContext, resolve_sources


@dataclass
class IngestReport:
  """What an ingest run actually did — per source, plus failures."""

  sources: dict = field(default_factory=dict)
  documents: int = 0
  chunks: int = 0
  embedded: int = 0
  errors: list = field(default_factory=list)
  elapsed_s: float = 0.0

  def as_dict(self):
    return {
        "sources": self.sources,
        "documents": self.documents,
        "chunks": self.chunks,
        "embedded": self.embedded,
        "errors": self.errors,
        "elapsed_s": round(self.elapsed_s, 2),
    }


async def ingest(
    store,
    source_names,
    *,
    config=None,
    limit=None,
    query="",
    urls=None,
    embedder=None,
    fetcher=None,
    backend=None,
    embed=True,
    on_progress=None,
):
  """Fetch ``source_names`` into ``store`` and return an :class:`IngestReport`.

  ``query`` is passed to the source and means whatever that source makes of it:
  a keyword search for NVD, a domain for ATT&CK, the search queries for the open
  web. ``limit`` caps documents per source; without one each source uses its own
  default (a whole catalog for ATT&CK/KEV, a page for a feed or a web crawl).
  """
  config = config or load_rag_config()
  report = IngestReport()
  started = time.time()
  emit = on_progress or (lambda *_a, **_k: None)

  specs, unknown = resolve_sources(source_names)
  for name in unknown:
    report.errors.append(f"unknown source {name!r}")
  ctx = FetchContext(
      query=query,
      urls=list(urls or []),
      concurrency=config.fetch_concurrency,
      max_doc_chars=config.max_doc_chars,
      fetcher=fetcher,
      backend=backend,
  )

  for spec in specs:
    ctx.limit = limit or spec.default_limit or config.max_docs_per_source
    emit("source_start", {"source": spec.name})
    try:
      result = await spec.fetch(ctx)
    except Exception as exc:  # noqa: BLE001 — a broken source is reported, not fatal
      report.errors.append(f"{spec.name}: {type(exc).__name__}: {exc}")
      report.sources[spec.name] = {"documents": 0, "chunks": 0, "failed": True}
      emit("source_error", {"source": spec.name, "error": str(exc)})
      continue
    docs = chunks = 0
    for doc in result.documents:
      pieces = chunk_text(
          doc.text,
          chunk_chars=config.chunk_chars,
          overlap=config.chunk_overlap,
          min_chars=config.min_chunk_chars,
      )
      if not pieces:
        continue
      store.add_document(doc, pieces)
      docs += 1
      chunks += len(pieces)
    report.sources[spec.name] = {"documents": docs, "chunks": chunks}
    report.documents += docs
    report.chunks += chunks
    report.errors += [f"{spec.name}: {e}" for e in result.errors[:10]]
    if len(result.errors) > 10:
      report.errors.append(
          f"{spec.name}: +{len(result.errors) - 10} more fetch failures"
      )
    emit("source_done", {"source": spec.name, "documents": docs, "chunks": chunks})

  if embed and embedder is not None:
    try:
      report.embedded = await embed_pending(store, embedder, on_progress=emit)
    except EmbeddingError as exc:
      report.errors.append(str(exc))

  report.elapsed_s = time.time() - started
  return report


async def embed_pending(store, embedder, *, batch=256, on_progress=None):
  """Embed every chunk still missing a vector for ``embedder``. Resumable."""
  emit = on_progress or (lambda *_a, **_k: None)
  total = 0
  while True:
    pending = store.chunks_without_vectors(embedder.name, limit=batch)
    if not pending:
      return total
    vectors = await embedder.embed([text for _, text in pending])
    store.set_vectors(
        embedder.name,
        [(cid, vec) for (cid, _), vec in zip(pending, vectors, strict=True)],
    )
    total += len(pending)
    emit("embedded", {"count": total})
