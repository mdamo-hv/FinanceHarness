"""RAG configuration — where the corpus lives and how it is chunked/embedded.

Precedence (low → high): built-in defaults < ``configs/rag.json`` < environment
variables, mirroring ``runtime/config.py``. The loader never raises on a missing
or malformed file, so the corpus always has a usable configuration.

Embeddings are *optional*: with no embedding model configured the system runs
lexical-only (BM25), which needs no credentials and no network. Set
``FH_RAG_EMBED_MODEL`` (plus a base URL/key when the endpoint isn't OpenAI's)
and retrieval becomes hybrid — BM25 and vector ranks fused.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict

_DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parents[2] / "configs" / "rag.json"
)


class RagConfig(BaseModel):
  """Corpus location, chunk geometry, ingest bounds, and the embedding seam."""

  # Tolerate unknown keys in the file rather than crashing the loader.
  model_config = ConfigDict(extra="ignore")

  # Where the SQLite corpus lives. Relative paths resolve against the CWD.
  db_path: str = "~/.financeharness/rag/cyber.sqlite3"

  # Chunk geometry, in characters (a chunk is what retrieval returns and what
  # the model reads, so it is sized for a readable passage, not a token budget).
  chunk_chars: int = 1200
  chunk_overlap: int = 200
  min_chunk_chars: int = 80

  # Ingest bounds — backstops against a runaway crawl, not a quality budget.
  max_docs_per_source: int = 200  # backstop when a source declares no default
  fetch_concurrency: int = 8
  max_doc_chars: int = 200_000

  # Retrieval defaults.
  top_k: int = 6
  candidate_k: int = 40  # per-ranker depth before fusion

  # Embeddings (optional). Empty model → lexical-only.
  embed_model: str = ""
  embed_base_url: str = "https://api.openai.com/v1"
  embed_api_key_env: str = "OPENAI_API_KEY"
  embed_batch: int = 64
  embed_timeout_s: float = 60.0

  def resolved_db_path(self):
    """The corpus path with ``~`` expanded, as an absolute :class:`Path`."""
    return Path(os.path.expanduser(self.db_path)).resolve()

  def embeddings_enabled(self):
    return bool(self.embed_model.strip())


# env var → (field, caster). Env always wins, so a shell can point one run at a
# scratch corpus without touching the file.
_ENV = {
    "FH_RAG_DB": ("db_path", str),
    "FH_RAG_CHUNK_CHARS": ("chunk_chars", int),
    "FH_RAG_CHUNK_OVERLAP": ("chunk_overlap", int),
    "FH_RAG_MAX_DOCS": ("max_docs_per_source", int),
    "FH_RAG_CONCURRENCY": ("fetch_concurrency", int),
    "FH_RAG_TOP_K": ("top_k", int),
    "FH_RAG_EMBED_MODEL": ("embed_model", str),
    "FH_RAG_EMBED_BASE_URL": ("embed_base_url", str),
    "FH_RAG_EMBED_API_KEY_ENV": ("embed_api_key_env", str),
    "FH_RAG_EMBED_BATCH": ("embed_batch", int),
}


def _file_values(path):
  """Values from the optional JSON file; ``{}`` when absent or malformed."""
  if not path.exists():
    return {}
  with contextlib.suppress(Exception):
    data = json.loads(path.read_text())
    if isinstance(data, dict):
      return {k: v for k, v in data.items() if not k.startswith("_")}
  return {}


def _env_values(env):
  values = {}
  for var, (field, cast) in _ENV.items():
    raw = env.get(var)
    if raw is None or not raw.strip():
      continue
    with contextlib.suppress(ValueError):
      values[field] = cast(raw)
  return values


def load_rag_config(*, path=None, overrides=None, env=None):
  """Load the RAG config: defaults < file < programmatic overrides < env."""
  path = Path(path) if path else _DEFAULT_CONFIG_PATH
  values = _file_values(path)
  values.update(overrides or {})
  values.update(_env_values(os.environ if env is None else env))
  with contextlib.suppress(Exception):
    return RagConfig(**values)
  return RagConfig()
