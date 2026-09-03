"""knowledge.cyber.* — the RAG corpus as harness tools.

Three tools over one corpus:

``knowledge.cyber.search`` retrieves passages and **cites them the way `visit`
does** — every passage's source URL enters the run's citation index, so a claim
drawn from the corpus carries a ``[N]`` marker into the report and lands in the
bibliography. Retrieval that can't be cited is not research, it is recall.

``knowledge.cyber.ingest`` fills the corpus from the internet mid-run: the agent
notices the corpus has nothing on a vendor advisory, ingests that feed or a web
query, and searches again.

``knowledge.cyber.status`` reports what is actually in the corpus, so the model
can tell "no such thing" from "not ingested yet" instead of guessing.

All three are deferred: they cost one catalog line each until the model loads
them.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from financeharness.rag import (
  SOURCES,
  format_passages,
  ingest,
  load_rag_config,
  open_corpus,
  resolve_embedder,
  retrieve,
)
from financeharness.rag.sources import DEFAULT_SOURCES
from financeharness.runtime.tool_events import emit_tool_event, emit_tool_progress
from financeharness.runtime.tool_registry import ToolError, ToolResponse, ToolSpec

_SEARCH_DESCRIPTION = (
    "Search the local cyber-security knowledge corpus (MITRE ATT&CK, CISA KEV"
    " and best practices, NVD CVEs, OWASP guidance, security reporting, and any"
    " web pages ingested into it) and return ranked passages with their source"
    " URLs, added to the bibliography for [N] citation. Use before the open web"
    " for threat, vulnerability, technique and control questions — it is"
    " authoritative, already read, and costs no page fetches."
)
_INGEST_DESCRIPTION = (
    "Ingest cyber-security knowledge from the internet into the local corpus"
    " (MITRE ATT&CK, CISA KEV, CISA best practices, NVD, OWASP, security news"
    " feeds, or an open web search) so it can be retrieved and cited. Use when"
    " knowledge.cyber.search comes back thin on a topic."
)
_STATUS_DESCRIPTION = (
    "Report what the cyber-security corpus contains — document and passage"
    " counts per source, and whether vector embeddings are in use. Use to tell"
    " an empty corpus from a genuine absence of evidence."
)

_MAX_K = 25
_HEADLINE_CHARS = 140


class CyberSearchRequest(BaseModel):
  """Input for a corpus retrieval."""

  query: str = Field(
      ...,
      description=(
          "What to retrieve — a question, a CVE id, an ATT&CK id, a technique"
          " or product name."
      ),
  )
  k: int | None = Field(
      None, description="How many passages to return (default 6, max 25)."
  )
  sources: list[str] | None = Field(
      None,
      description=(
          "Optional source filter, e.g. ['mitre-attack'] or ['cisa-kev',"
          " 'nvd-cve']. Omit to search everything."
      ),
  )

  @field_validator("sources", mode="before")
  @classmethod
  def _wrap_single(cls, v):
    return [v] if isinstance(v, str) else v


class CyberIngestRequest(BaseModel):
  """Input for filling the corpus from the internet."""

  sources: list[str] | None = Field(
      None,
      description=(
          "Sources to ingest: "
          + ", ".join(sorted(SOURCES))
          + ". 'all' ingests every source; omit for the default set "
          + ", ".join(DEFAULT_SOURCES)
          + "."
      ),
  )
  query: str | None = Field(
      None,
      description=(
          "Source-specific selector: search queries for 'web' (';'-separated),"
          " an NVD keyword search, or the ATT&CK domain"
          " (enterprise/mobile/ics)."
      ),
  )
  urls: list[str] | None = Field(
      None, description="Explicit URLs to ingest (with the 'urls' source)."
  )
  limit: int | None = Field(
      None, description="Max documents per source (default 200)."
  )

  @field_validator("sources", "urls", mode="before")
  @classmethod
  def _wrap_single(cls, v):
    return [v] if isinstance(v, str) else v


class CyberStatusRequest(BaseModel):
  """Input for the corpus status report (no arguments)."""


def _headline(text):
  line = " ".join(text.split())
  return line[:_HEADLINE_CHARS] + ("…" if len(line) > _HEADLINE_CHARS else "")


def build_cyber_search_spec(cache, *, config=None):
  """Build `knowledge.cyber.search`, citing hits into ``cache``."""
  config = config or load_rag_config()

  async def handler(req):
    k = min(max(1, req.k or config.top_k), _MAX_K)
    unknown = [s for s in (req.sources or []) if s not in SOURCES]
    if unknown:
      raise ToolError(
          f"unknown source(s) {', '.join(unknown)}; available:"
          f" {', '.join(sorted(SOURCES))}"
      )
    store = open_corpus(config)
    try:
      stats = store.stats()
      if not stats["chunks"]:
        raise ToolError(
            "the cyber corpus is empty — run knowledge.cyber.ingest (or"
            " `fh rag ingest`) first, or use `search`/`visit` for the open web."
        )
      emit_tool_progress(f"retrieving {k} passage(s) for {req.query!r}")
      result = await retrieve(
          store,
          req.query,
          k=k,
          config=config,
          embedder=resolve_embedder(config),
          sources=req.sources or None,
      )
      if not result.passages:
        raise ToolError(
            f"no passages matched {req.query!r} in the corpus"
            f" ({stats['documents']} documents). Broaden the query, ingest more"
            " sources, or fall back to the open web."
        )
      # Cite every retrieved source the way `visit` does — the passage was
      # read, so the report can carry a [N] marker back to its URL.
      passages = []
      for passage in result.passages:
        cache.set_title(passage.url, passage.title)
        idx = cache.add_citation(passage.url, passage.title)
        # Deliberately *not* cache.set_content(): a passage is one chunk of the
        # page, and `visit` treats cached content as the whole document — a
        # later visit to the same URL would silently read the fragment.
        emit_tool_event(
            "source",
            {
                "index": idx,
                "url": passage.url,
                "title": passage.title or passage.url,
                "headline": _headline(passage.text),
            },
        )
        passages.append({
            "citation_index": idx,
            "url": passage.url,
            "title": passage.title,
            "source": passage.source,
            "score": round(passage.score, 6),
            "text": passage.text,
            "meta": passage.meta,
        })
      body = format_passages(result.passages)
      cite_map = "\n".join(
          f"[{p['citation_index']}] {p['title'] or p['url']} — {p['url']}"
          for p in passages
      )
      notes = ("\n\nNotes: " + "; ".join(result.notes)) if result.notes else ""
      return ToolResponse(
          markdown=(
              f"**{len(passages)} passage(s)** from the cyber corpus"
              f" ({result.mode} retrieval):\n\n{body}\n\n**Cite as:**\n"
              f"{cite_map}{notes}"
          ),
          structured={"passages": passages, "count": len(passages)},
          meta={
              "mode": result.mode,
              "lexical_hits": result.lexical_hits,
              "vector_hits": result.vector_hits,
              "corpus_documents": stats["documents"],
          },
      )
    finally:
      store.close()

  return ToolSpec(
      name="knowledge_cyber_search",
      display_name="knowledge.cyber.search",
      tier="deferred",
      description=_SEARCH_DESCRIPTION,
      request_schema=CyberSearchRequest,
      handler=handler,
      tags=("cyber", "security", "rag", "retrieval", "threat", "cve"),
  )


def build_cyber_ingest_spec(*, config=None, backend=None, fetcher=None):
  """Build `knowledge.cyber.ingest` — fill the corpus from the internet."""
  config = config or load_rag_config()

  async def handler(req):
    names = req.sources or list(DEFAULT_SOURCES)
    store = open_corpus(config)
    try:
      report = await ingest(
          store,
          names,
          config=config,
          limit=req.limit,
          query=req.query or "",
          urls=req.urls or [],
          embedder=resolve_embedder(config),
          backend=backend,
          fetcher=fetcher,
          on_progress=lambda kind, data: emit_tool_progress(
              f"{data.get('source', '')}: {kind.replace('source_', '')}"
          ),
      )
      stats = store.stats()
    finally:
      store.close()
    if not report.documents:
      raise ToolError(
          "ingest stored no documents"
          + (f" — {report.errors[0]}" if report.errors else "")
      )
    lines = [
        f"- `{name}`: {info['documents']} document(s),"
        f" {info['chunks']} passage(s)"
        for name, info in report.sources.items()
    ]
    errors = (
        "\n\nFailures:\n"
        + "\n".join(f"- {e}" for e in report.errors[:8])
        if report.errors
        else ""
    )
    return ToolResponse(
        markdown=(
            f"Ingested **{report.documents} document(s)** /"
            f" {report.chunks} passage(s) in {report.elapsed_s:.1f}s:\n"
            + "\n".join(lines)
            + f"\n\nCorpus now holds {stats['documents']} documents"
            f" ({stats['chunks']} passages)."
            + errors
        ),
        structured={**report.as_dict(), "corpus": stats},
        meta={"documents": report.documents, "chunks": report.chunks},
    )

  return ToolSpec(
      name="knowledge_cyber_ingest",
      display_name="knowledge.cyber.ingest",
      tier="deferred",
      description=_INGEST_DESCRIPTION,
      request_schema=CyberIngestRequest,
      handler=handler,
      tags=("cyber", "security", "rag", "ingest"),
  )


def build_cyber_status_spec(*, config=None):
  """Build `knowledge.cyber.status` — what the corpus actually holds."""
  config = config or load_rag_config()

  async def handler(_req):
    store = open_corpus(config)
    try:
      stats = store.stats()
    finally:
      store.close()
    embedder = resolve_embedder(config)
    by_source = "\n".join(
        f"- `{name}`: {count} document(s)"
        for name, count in stats["by_source"].items()
    ) or "- (empty — nothing ingested yet)"
    retrieval = (
        f"hybrid (BM25 + {embedder.name})" if embedder else "lexical BM25"
    )
    return ToolResponse(
        markdown=(
            f"**Cyber corpus** — {stats['documents']} documents,"
            f" {stats['chunks']} passages, {stats['chars']:,} characters.\n"
            f"Retrieval: {retrieval} · index: {stats['lexical_index']}\n\n"
            f"{by_source}\n\nIngestable sources:"
            f" {', '.join(sorted(SOURCES))}"
        ),
        structured=stats,
        meta={"documents": stats["documents"]},
    )

  return ToolSpec(
      name="knowledge_cyber_status",
      display_name="knowledge.cyber.status",
      tier="deferred",
      description=_STATUS_DESCRIPTION,
      request_schema=CyberStatusRequest,
      handler=handler,
      tags=("cyber", "security", "rag", "status"),
  )


def build_cyber_specs(cache, *, config=None, backend=None, fetcher=None):
  """All three corpus tools, bound to the run's citation cache."""
  return [
      build_cyber_search_spec(cache, config=config),
      build_cyber_ingest_spec(config=config, backend=backend, fetcher=fetcher),
      build_cyber_status_spec(config=config),
  ]
