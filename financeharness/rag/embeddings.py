"""Embeddings — the optional dense half of retrieval.

An embedder turns passages and queries into vectors so retrieval can match on
meaning ("how do attackers keep access after a reboot?" → persistence
techniques) rather than on shared words alone. It speaks the OpenAI-compatible
``/embeddings`` endpoint, which is what OpenAI, a local vLLM/Ollama server, and
most aggregators expose — the same wire assumption the harness's chat providers
already make.

Embeddings are opt-in. With no model configured ``resolve_embedder`` returns
``None`` and retrieval runs lexical-only: no credentials, no network, still
useful. Configure one and the same corpus becomes hybrid without re-ingesting —
``rag embed`` backfills vectors for the chunks already stored.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class EmbeddingError(RuntimeError):
  """The embedding endpoint could not serve a request (expected, actionable)."""


@dataclass
class OpenAIEmbedder:
  """OpenAI-compatible ``/embeddings`` client.

  ``base_url`` includes the version prefix (``https://api.openai.com/v1``,
  ``http://127.0.0.1:8000/v1``); the credential is read from the environment at
  call time and never stored on the instance.
  """

  model: str
  base_url: str = "https://api.openai.com/v1"
  api_key_env: str = "OPENAI_API_KEY"
  batch: int = 64
  timeout_s: float = 60.0

  @property
  def name(self):
    return self.model

  def _key(self):
    key = os.environ.get(self.api_key_env, "")
    if not key:
      # A local server usually ignores the header but still wants one present.
      return "EMPTY" if "127.0.0.1" in self.base_url or "localhost" in self.base_url else ""
    return key

  async def embed(self, texts):
    """Embed ``texts`` in batches, preserving order."""
    import httpx

    texts = list(texts)
    if not texts:
      return []
    key = self._key()
    if not key:
      raise EmbeddingError(
          f"no embedding credential: set ${self.api_key_env} (or unset"
          " FH_RAG_EMBED_MODEL to run lexical-only)"
      )
    out = []
    headers = {"Authorization": f"Bearer {key}"}
    url = f"{self.base_url.rstrip('/')}/embeddings"
    async with httpx.AsyncClient(timeout=self.timeout_s) as client:
      for start in range(0, len(texts), self.batch):
        window = texts[start : start + self.batch]
        try:
          resp = await client.post(
              url, headers=headers, json={"model": self.model, "input": window}
          )
          resp.raise_for_status()
          data = resp.json()["data"]
        except Exception as exc:  # noqa: BLE001 — surfaced as an actionable error
          raise EmbeddingError(
              f"embedding call to {url} failed: {type(exc).__name__}: {exc}"
          ) from exc
        # The API may return items out of order; `index` is authoritative.
        ordered = sorted(data, key=lambda d: d.get("index", 0))
        out += [d["embedding"] for d in ordered]
    if len(out) != len(texts):
      raise EmbeddingError(
          f"embedding endpoint returned {len(out)} vectors for {len(texts)} inputs"
      )
    return out


def resolve_embedder(config):
  """The configured embedder, or ``None`` when the corpus is lexical-only."""
  if not config.embeddings_enabled():
    return None
  return OpenAIEmbedder(
      model=config.embed_model,
      base_url=config.embed_base_url,
      api_key_env=config.embed_api_key_env,
      batch=config.embed_batch,
      timeout_s=config.embed_timeout_s,
  )


def cosine_ranking(query_vec, vectors, k):
  """Rank ``(chunk_id, vector)`` pairs by cosine similarity to ``query_vec``.

  Uses numpy when it is importable (it ships with the harness's data stack) and
  falls back to pure Python so the dense path never hard-depends on it.
  """
  pairs = list(vectors)
  if not pairs or not query_vec:
    return []
  try:
    import numpy as np

    matrix = np.array([v for _, v in pairs], dtype="float32")
    query = np.array(query_vec, dtype="float32")
    norms = np.linalg.norm(matrix, axis=1) * (np.linalg.norm(query) or 1.0)
    norms[norms == 0] = 1.0
    scores = (matrix @ query) / norms
    order = np.argsort(-scores)[:k]
    return [(pairs[i][0], float(scores[i])) for i in order]
  except ImportError:
    scored = []
    qnorm = sum(x * x for x in query_vec) ** 0.5 or 1.0
    for chunk_id, vec in pairs:
      norm = (sum(x * x for x in vec) ** 0.5 or 1.0) * qnorm
      dot = sum(a * b for a, b in zip(vec, query_vec, strict=False))
      scored.append((chunk_id, dot / norm))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:k]
