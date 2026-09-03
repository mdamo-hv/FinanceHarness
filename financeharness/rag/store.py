"""Corpus store — SQLite-backed documents, chunks, lexical index, and vectors.

One file on disk holds the whole corpus: the documents ingested from the
internet, the passages they were split into, an FTS5 index for BM25 ranking, and
(when embeddings are configured) one float32 vector per passage. SQLite is the
whole storage dependency — no server, no extra service to run, and the corpus is
a single file you can copy, inspect, or delete.

Documents are keyed by URL and upserted, so re-ingesting a feed refreshes what
changed instead of duplicating it: a document's old chunks and vectors are
deleted with it (``ON DELETE CASCADE``) before the new ones land.

FTS5 is present in essentially every modern SQLite build; when it isn't, the
store falls back to a pure-Python BM25 over the chunk table so retrieval still
works rather than the corpus becoming unreadable.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import struct
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
  id         INTEGER PRIMARY KEY,
  url        TEXT NOT NULL UNIQUE,
  title      TEXT NOT NULL DEFAULT '',
  source     TEXT NOT NULL DEFAULT '',
  fetched_at REAL NOT NULL,
  chars      INTEGER NOT NULL DEFAULT 0,
  meta       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS documents_source ON documents(source);

CREATE TABLE IF NOT EXISTS chunks (
  id      INTEGER PRIMARY KEY,
  doc_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  text    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id);

CREATE TABLE IF NOT EXISTS vectors (
  chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
  model    TEXT NOT NULL,
  dim      INTEGER NOT NULL,
  vec      BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS corpus_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9._+-]*")
# BM25 term-saturation / length-normalization constants (Robertson et al.).
_BM25_K1 = 1.5
_BM25_B = 0.75


def tokenize(text):
  """Lowercase word/identifier tokens — shared by the fallback BM25 scorer.

  Keeps ``.``/``-``/``_``/``+`` inside a token so ``cve-2021-44228`` and
  ``log4j-core`` survive as single terms, which is most of what a security
  corpus is searched by.
  """
  return _TOKEN_RE.findall(text.lower())


@dataclass(frozen=True)
class Passage:
  """One retrieved chunk plus the document it came from."""

  chunk_id: int
  text: str
  url: str
  title: str
  source: str
  score: float = 0.0
  meta: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Document:
  """A document staged for ingestion (already fetched and text-extracted)."""

  url: str
  title: str
  text: str
  source: str
  meta: dict = field(default_factory=dict)


def pack_vector(values):
  """Pack a float sequence into a compact float32 blob."""
  return struct.pack(f"<{len(values)}f", *values)


def unpack_vector(blob):
  return list(struct.unpack(f"<{len(blob) // 4}f", blob))


class CorpusStore:
  """The on-disk corpus. Cheap to open; one connection per instance."""

  def __init__(self, path):
    self.path = Path(path)
    self.path.parent.mkdir(parents=True, exist_ok=True)
    self._conn = sqlite3.connect(str(self.path))
    self._conn.row_factory = sqlite3.Row
    self._conn.execute("PRAGMA foreign_keys = ON")
    self._conn.execute("PRAGMA journal_mode = WAL")
    self._conn.executescript(_SCHEMA)
    self.fts = self._init_fts()
    self._conn.execute(
        "INSERT OR REPLACE INTO corpus_meta(key, value) VALUES('schema', ?)",
        (str(_SCHEMA_VERSION),),
    )
    self._conn.commit()

  # lifecycle ----------------------------------------------------------- #
  def _init_fts(self):
    """Create the FTS5 index; ``False`` when this SQLite lacks the module."""
    try:
      self._conn.executescript(
          "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts"
          " USING fts5(text, tokenize='unicode61 remove_diacritics 2');"
      )
      return True
    except sqlite3.OperationalError:
      return False

  def close(self):
    self._conn.close()

  def __enter__(self):
    return self

  def __exit__(self, *_exc):
    self.close()

  # writes -------------------------------------------------------------- #
  def add_document(self, doc, chunks):
    """Upsert ``doc`` and replace its chunks. Returns ``(doc_id, n_chunks)``.

    Replacing means deleting the row: the cascade takes the old chunks and their
    vectors with it, so a refreshed feed never leaves orphaned passages behind.
    """
    cur = self._conn.cursor()
    old = cur.execute(
        "SELECT id FROM documents WHERE url = ?", (doc.url,)
    ).fetchone()
    if old is not None:
      self._delete_document(cur, old["id"])
    cur.execute(
        "INSERT INTO documents(url, title, source, fetched_at, chars, meta)"
        " VALUES(?, ?, ?, ?, ?, ?)",
        (
            doc.url,
            doc.title or doc.url,
            doc.source,
            time.time(),
            len(doc.text),
            json.dumps(doc.meta, default=str),
        ),
    )
    doc_id = cur.lastrowid
    for ordinal, text in enumerate(chunks):
      cur.execute(
          "INSERT INTO chunks(doc_id, ordinal, text) VALUES(?, ?, ?)",
          (doc_id, ordinal, text),
      )
      if self.fts:
        cur.execute(
            "INSERT INTO chunks_fts(rowid, text) VALUES(?, ?)",
            (cur.lastrowid, text),
        )
    self._conn.commit()
    return doc_id, len(chunks)

  def _delete_document(self, cur, doc_id):
    if self.fts:
      ids = [
          r["id"]
          for r in cur.execute(
              "SELECT id FROM chunks WHERE doc_id = ?", (doc_id,)
          )
      ]
      cur.executemany(
          "DELETE FROM chunks_fts WHERE rowid = ?", [(i,) for i in ids]
      )
    cur.execute("DELETE FROM documents WHERE id = ?", (doc_id,))

  def set_vectors(self, model, rows):
    """Store ``(chunk_id, vector)`` pairs for an embedding model."""
    self._conn.executemany(
        "INSERT OR REPLACE INTO vectors(chunk_id, model, dim, vec)"
        " VALUES(?, ?, ?, ?)",
        [(cid, model, len(vec), pack_vector(vec)) for cid, vec in rows],
    )
    self._conn.commit()

  def clear(self, source=None):
    """Delete the whole corpus, or just one source's documents."""
    cur = self._conn.cursor()
    where = "WHERE source = ?" if source else ""
    args = (source,) if source else ()
    ids = [
        r["id"] for r in cur.execute(f"SELECT id FROM documents {where}", args)
    ]
    for doc_id in ids:
      self._delete_document(cur, doc_id)
    self._conn.commit()
    return len(ids)

  # reads --------------------------------------------------------------- #
  def document_urls(self, source=None):
    where = "WHERE source = ?" if source else ""
    args = (source,) if source else ()
    return {
        r["url"]
        for r in self._conn.execute(f"SELECT url FROM documents {where}", args)
    }

  def chunks_without_vectors(self, model, limit=None):
    """Chunks that still need an embedding for ``model`` (resumable embedding)."""
    sql = (
        "SELECT c.id, c.text FROM chunks c"
        " LEFT JOIN vectors v ON v.chunk_id = c.id AND v.model = ?"
        " WHERE v.chunk_id IS NULL ORDER BY c.id"
    )
    args = [model]
    if limit:
      sql += " LIMIT ?"
      args.append(limit)
    return [(r["id"], r["text"]) for r in self._conn.execute(sql, args)]

  def stats(self):
    """Corpus size, per-source document counts, and embedding coverage."""
    one = lambda sql: self._conn.execute(sql).fetchone()[0]  # noqa: E731
    by_source = {
        r["source"]: r["n"]
        for r in self._conn.execute(
            "SELECT source, COUNT(*) AS n FROM documents"
            " GROUP BY source ORDER BY n DESC"
        )
    }
    models = {
        r["model"]: r["n"]
        for r in self._conn.execute(
            "SELECT model, COUNT(*) AS n FROM vectors GROUP BY model"
        )
    }
    return {
        "path": str(self.path),
        "documents": one("SELECT COUNT(*) FROM documents"),
        "chunks": one("SELECT COUNT(*) FROM chunks"),
        "chars": one("SELECT COALESCE(SUM(chars), 0) FROM documents"),
        "by_source": by_source,
        "embedded": models,
        "lexical_index": "fts5" if self.fts else "python-bm25",
    }

  def _passage(self, row, score):
    meta = {}
    try:
      meta = json.loads(row["meta"] or "{}")
    except (TypeError, ValueError):
      meta = {}
    return Passage(
        chunk_id=row["id"],
        text=row["text"],
        url=row["url"],
        title=row["title"],
        source=row["source"],
        score=score,
        meta=meta,
    )

  def search_lexical(self, query, k, *, sources=None):
    """Top-``k`` passages by BM25 (FTS5 when available, else pure Python)."""
    if self.fts:
      hits = self._search_fts(query, k, sources)
      if hits:
        return hits
      # An FTS5 syntax-free query that matches nothing still deserves the
      # fallback's looser bag-of-words behaviour before giving up.
    return self._search_python(query, k, sources)

  def _search_fts(self, query, k, sources):
    terms = tokenize(query)
    if not terms:
      return []
    # Quote every term: FTS5 treats bare `-`/`.`/`+` as operators, and CVE ids
    # are full of them. OR keeps recall up when only part of the query matches.
    match = " OR ".join(f'"{t}"' for t in terms)
    sql = (
        "SELECT c.id, c.text, d.url, d.title, d.source, d.meta,"
        " bm25(chunks_fts) AS rank"
        " FROM chunks_fts"
        " JOIN chunks c ON c.id = chunks_fts.rowid"
        " JOIN documents d ON d.id = c.doc_id"
        " WHERE chunks_fts MATCH ?"
    )
    args = [match]
    if sources:
      sql += f" AND d.source IN ({','.join('?' * len(sources))})"
      args += list(sources)
    sql += " ORDER BY rank LIMIT ?"
    args.append(k)
    try:
      rows = self._conn.execute(sql, args).fetchall()
    except sqlite3.OperationalError:
      return []
    # bm25() returns a negative score (better = more negative); flip it so
    # higher is better everywhere in the pipeline.
    return [self._passage(r, -float(r["rank"])) for r in rows]

  def _search_python(self, query, k, sources):
    """BM25 over the chunk table — the no-FTS5 fallback.

    Loads the corpus into memory, so it is the slow path by design; it exists so
    a SQLite build without FTS5 degrades in speed rather than in function.
    """
    terms = [t for t in tokenize(query) if t]
    if not terms:
      return []
    sql = (
        "SELECT c.id, c.text, d.url, d.title, d.source, d.meta"
        " FROM chunks c JOIN documents d ON d.id = c.doc_id"
    )
    args = []
    if sources:
      sql += f" WHERE d.source IN ({','.join('?' * len(sources))})"
      args = list(sources)
    rows = self._conn.execute(sql, args).fetchall()
    if not rows:
      return []
    tokenized = [tokenize(r["text"]) for r in rows]
    lengths = [len(t) for t in tokenized]
    avg_len = sum(lengths) / len(lengths) or 1.0
    df = Counter()
    for tokens in tokenized:
      df.update(set(tokens) & set(terms))
    n = len(rows)
    scored = []
    for row, tokens, length in zip(rows, tokenized, lengths, strict=True):
      counts = Counter(tokens)
      score = 0.0
      for term in terms:
        tf = counts.get(term, 0)
        if not tf:
          continue
        idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
        norm = tf + _BM25_K1 * (1 - _BM25_B + _BM25_B * length / avg_len)
        score += idf * (tf * (_BM25_K1 + 1)) / norm
      if score > 0:
        scored.append((score, row))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [self._passage(row, score) for score, row in scored[:k]]

  def iter_vectors(self, model, *, sources=None):
    """``(chunk_id, vector)`` for every embedded chunk of ``model``."""
    sql = "SELECT v.chunk_id, v.vec FROM vectors v WHERE v.model = ?"
    args = [model]
    if sources:
      sql += (
          " AND v.chunk_id IN (SELECT c.id FROM chunks c"
          " JOIN documents d ON d.id = c.doc_id"
          f" WHERE d.source IN ({','.join('?' * len(sources))}))"
      )
      args += list(sources)
    for row in self._conn.execute(sql, args):
      yield row["chunk_id"], unpack_vector(row["vec"])

  def passages_by_id(self, chunk_ids, scores=None):
    """Hydrate chunk ids into passages, preserving the given order."""
    ids = list(chunk_ids)
    if not ids:
      return []
    rows = self._conn.execute(
        "SELECT c.id, c.text, d.url, d.title, d.source, d.meta"
        " FROM chunks c JOIN documents d ON d.id = c.doc_id"
        f" WHERE c.id IN ({','.join('?' * len(ids))})",
        ids,
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    scores = scores or {}
    return [
        self._passage(by_id[i], scores.get(i, 0.0)) for i in ids if i in by_id
    ]
