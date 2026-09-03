"""RAG — a retrieval corpus of internet cyber-security knowledge.

Ingest authoritative feeds (MITRE ATT&CK, CISA KEV, CISA best practices, NVD,
OWASP), security reporting, and anything else reachable on the open web into a
single SQLite corpus; retrieve passages from it with hybrid BM25 + vector search
and hand them to the agent with their source URLs attached.

Entry points: :func:`ingest` to fill the corpus, :func:`retrieve` to query it,
:class:`CorpusStore` to open it, and ``open_corpus`` for the configured default.
"""

from financeharness.rag.chunking import chunk_text
from financeharness.rag.config import RagConfig, load_rag_config
from financeharness.rag.embeddings import (
  EmbeddingError,
  OpenAIEmbedder,
  resolve_embedder,
)
from financeharness.rag.ingest import IngestReport, embed_pending, ingest
from financeharness.rag.retrieve import (
  RetrievalResult,
  format_passages,
  retrieve,
)
from financeharness.rag.sources import DEFAULT_SOURCES, SOURCES, resolve_sources
from financeharness.rag.store import CorpusStore, Document, Passage


def open_corpus(config=None):
  """Open the configured corpus (creating it on first use)."""
  config = config or load_rag_config()
  return CorpusStore(config.resolved_db_path())


__all__ = [
    "DEFAULT_SOURCES",
    "SOURCES",
    "CorpusStore",
    "Document",
    "EmbeddingError",
    "IngestReport",
    "OpenAIEmbedder",
    "Passage",
    "RagConfig",
    "RetrievalResult",
    "chunk_text",
    "embed_pending",
    "format_passages",
    "ingest",
    "load_rag_config",
    "open_corpus",
    "resolve_embedder",
    "resolve_sources",
    "retrieve",
]
