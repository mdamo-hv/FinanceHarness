# Cyber RAG — internet security knowledge, retrievable and citable

A retrieval-augmented generation subsystem that ingests cyber-security knowledge
from the internet into a local corpus, and hands the agent ranked passages *with
their source URLs* so every claim drawn from it can carry a `[N]` citation into
the report.

It is deliberately small: SQLite is the entire storage dependency, BM25 works
with no credentials and no network, and embeddings are one environment variable
away when you want semantic recall on top.

---

## Why a corpus at all, next to `search` + `visit`?

The open web is the right tool for "what happened this week". It is the wrong
tool for the parts of security knowledge that are *stable, structured, and
answered the same way every time*: what technique T1003.001 is, whether
CVE-2024-21762 is being exploited in the wild, what OWASP says about session
fixation. Those live in a handful of authoritative catalogs, and re-searching
and re-reading them on every run is slow, lossy, and rate-limited.

So: ingest them once, retrieve them in milliseconds, and cite them exactly the
way a visited page is cited. The open web stays available for everything the
corpus doesn't have — and `knowledge.cyber.ingest` can pull a web query straight
into the corpus mid-run when it doesn't.

---

## The pipeline

```
source → fetch → extract text → chunk → store ─┬─→ BM25 (FTS5)     ─┐
                                               └─→ embed → vectors ─┴─→ RRF → passages → [N] citations
```

| Stage | Where | What it does |
|---|---|---|
| **Source** | [`sources.py`](sources.py) | Named fetchers: structured catalogs, RSS feeds, open-web search, explicit URLs. |
| **Extract** | `visit_fetch.http_fetch` | The same fetch + trafilatura/pdfplumber extraction the `visit` tool uses, so an ingested page reads like a visited one. |
| **Chunk** | [`chunking.py`](chunking.py) | Packs whole paragraphs to ~1200 chars, breaks over-long ones on sentence boundaries, carries 200 chars of overlap forward. |
| **Store** | [`store.py`](store.py) | SQLite: documents keyed by URL, chunks, an FTS5 index, float32 vectors. Re-ingesting a URL replaces it (cascade), so feeds refresh instead of duplicating. |
| **Embed** | [`embeddings.py`](embeddings.py) | Optional. OpenAI-compatible `/embeddings`; a separate resumable pass over "chunks with no vector for this model". |
| **Retrieve** | [`retrieve.py`](retrieve.py) | BM25 and cosine rank independently, then Reciprocal Rank Fusion merges them. |
| **Cite** | [`../tools/knowledge/cyber.py`](../tools/knowledge/cyber.py) | Every returned passage's URL enters the run's citation index. |

### Why hybrid retrieval

BM25 is exact. It finds `CVE-2021-44228` and `T1059.001` because those strings
are literally in the text, and a security corpus is searched by identifier more
than by anything else. (The tokenizer keeps `.`, `-`, `_` and `+` inside a token
precisely so those identifiers survive as single terms.)

Vector search is approximate in the useful sense. It finds the persistence
technique when the question says "keep access after a reboot" and never uses the
word *persistence*.

Neither wins often enough to drop the other, so both run and their **ranks** are
fused with RRF — each list votes `1 / (60 + rank)` and the votes are summed.
Fusing ranks rather than scores means an FTS5 BM25 value and a cosine similarity
combine without being calibrated against each other, which is why RRF is the
default for hybrid search.

With no embedder configured this degrades to plain BM25 — still real retrieval,
just lexical.

---

## Sources

`fh rag sources` prints the live list. `*` marks the default ingest set.

| Source | What it is | Default cap |
|---|---|---|
| `mitre-attack` * | The official ATT&CK STIX bundle: every technique, tactic, mitigation, group, software and campaign as its own document, with its `attack.mitre.org` URL. `--query` picks `enterprise` (default), `mobile` or `ics`. | whole domain (~1.8k) |
| `cisa-kev` * | CISA Known Exploited Vulnerabilities — CVEs with *confirmed in-the-wild exploitation*, ransomware flag and remediation due date. | whole catalog (~1.7k) |
| `cisa-best-practices` * | [cisa.gov/topics/cybersecurity-best-practices](https://www.cisa.gov/topics/cybersecurity-best-practices) and the guidance pages it links (topics, Secure Our World, resources, advisories). | 60 pages |
| `owasp` * | The OWASP Cheat Sheet Series (~120 sheets) fetched as source markdown, so it is prose rather than page chrome. | 200 |
| `nvd-cve` | NVD CVE records with CVSS severity, CWE mappings and references. `--query` is an NVD keyword search; without one, the most recent CVEs. | 200 |
| `news:*` | Security reporting and research: Krebs, The Hacker News, Schneier, Unit 42, Project Zero, Google TAG, MSRC. `news` ingests all of them. | 25 per feed |
| `web` | **Open-web ingestion.** Runs `--query` (`;`-separated for several) through the harness search backend and ingests what it can read. This is the open-ended lever — the curated feeds are the high-signal floor, not the ceiling. | 20 |
| `urls` | An explicit list of URLs through the same page reader. | 50 |

Per-source caps exist because a structured catalog wants *all* of itself (ATT&CK
with a quarter of its techniques is misleading) while a feed or a crawl wants a
page — every one of those documents costs a live page fetch. `--limit` overrides
any of them.

A failing source is reported, never fatal: ingesting eight sources with one feed
down gives you seven sources of corpus and a named error for the eighth.

---

## Use it

### From the shell

```bash
fh rag sources                                  # what can be ingested
fh rag ingest                                   # the default set: ATT&CK, KEV, OWASP, CISA
fh rag ingest mitre-attack --query ics          # the ICS ATT&CK domain
fh rag ingest nvd-cve --query "apache struts"   # targeted CVE pull
fh rag ingest web --query "kubernetes rbac hardening; eBPF detection bypass"
fh rag ingest urls --url https://example.com/advisory
fh rag ingest news                              # every editorial feed

fh rag status                                   # documents, passages, sources, retrieval mode
fh rag query "how is LSASS credential dumping detected" -k 5
fh rag query CVE-2021-44228 --source cisa-kev --full
fh rag clear --source news:krebs                # drop one source
```

### From the agent

Three deferred tools — three catalog lines until the model loads them:

| Tool | Purpose |
|---|---|
| `knowledge.cyber.search` | Retrieve ranked passages; **each one is added to the run's bibliography** and returned with its `[N]` marker. |
| `knowledge.cyber.ingest` | Fill the corpus mid-run when search comes back thin, then search again. |
| `knowledge.cyber.status` | What the corpus holds — so the model can tell "not ingested yet" from "no such thing". |

The bundled **`cyber-threat-brief`** skill composes them: check status → retrieve
by identifier *and* by question → ingest the gap → fall back to `search`/`visit`
for anything still uncovered → write a brief with CVE/CWE/ATT&CK ids,
exploitation status, affected versions, mitigations, and an explicit list of what
is unknown.

They are also exposed over MCP (`fh mcp`) like every other harness tool, so an
IDE or Claude Desktop can query the corpus directly.

### From Python

```python
from financeharness.rag import open_corpus, ingest, retrieve, format_passages

store = open_corpus()
await ingest(store, ["mitre-attack", "cisa-kev"])
result = await retrieve(store, "phishing initial access mitigations", k=5)
print(format_passages(result.passages))
```

---

## Configuration

Defaults work with no configuration at all. Precedence is *built-in defaults <
`configs/rag.json` < environment*, matching the rest of the harness.

| Variable | Default | Effect |
|---|---|---|
| `FH_RAG_DB` | `~/.financeharness/rag/cyber.sqlite3` | corpus location (one file; copy it, ship it, delete it) |
| `FH_RAG_CHUNK_CHARS` | `1200` | target passage size |
| `FH_RAG_CHUNK_OVERLAP` | `200` | overlap carried between passages |
| `FH_RAG_TOP_K` | `6` | passages returned per query |
| `FH_RAG_MAX_DOCS` | `200` | per-source cap when a source declares no default |
| `FH_RAG_CONCURRENCY` | `8` | concurrent page fetches during ingest |
| `FH_RAG_EMBED_MODEL` | *(unset)* | **set this to enable vector search**, e.g. `text-embedding-3-small` |
| `FH_RAG_EMBED_BASE_URL` | `https://api.openai.com/v1` | any OpenAI-compatible endpoint (a local vLLM/Ollama works) |
| `FH_RAG_EMBED_API_KEY_ENV` | `OPENAI_API_KEY` | which env var holds the credential |
| `NVD_API_KEY` | *(unset)* | raises the NVD rate limit (without it, pages are pulled ~6.5s apart) |

Turning embeddings on later does **not** require re-ingesting: `fh rag embed`
backfills vectors for the passages already stored, and the pass is resumable.

---

## Operational notes

- **The corpus is a point-in-time snapshot.** A KEV entry ingested last month is
  stale on this month's additions. Re-run `fh rag ingest` before relying on
  recency; the URL-keyed upsert makes that cheap and non-duplicating.
- **Sizes.** The full ATT&CK enterprise domain plus the whole KEV catalog is
  ~3.5k documents / ~4.4k passages / ~2.9 MB of text, ingested in a few seconds.
  Adding OWASP and the CISA guidance pages brings it to ~3.7k documents.
- **`www.cisa.gov` sits behind a bot filter** that denies some networks outright.
  A 403 on every CISA page means the *network* is blocked, not the code; the KEV
  feed is served from a different path and stays reachable either way.
- **FTS5** is used when the SQLite build has it (essentially always). If it does
  not, the store falls back to a pure-Python BM25 — slower, same results.
- **Defensive framing.** The corpus is built from defender-facing sources and the
  skill is written to produce patch/detect/harden guidance. It is not an exploit
  library.
