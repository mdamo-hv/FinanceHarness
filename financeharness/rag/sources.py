"""Sources — where the cyber-security corpus comes from.

Each source is a named coroutine that returns :class:`Document` objects ready to
chunk. They fall into three groups:

*Structured feeds* — CISA KEV, NVD, MITRE ATT&CK, OWASP: authoritative, machine
readable, and worth ingesting wholesale. One record becomes one document, so a
CVE or an ATT&CK technique is retrievable as a unit and cites its canonical URL.

*Editorial feeds* — vendor and researcher RSS/Atom. The entry gives the title
and link; the page itself is fetched and text-extracted through the same
`visit` pipeline the research tools use, so an ingested article reads the way a
visited one does.

*Open web* — ``web`` runs queries through the harness's search backend and
ingests what comes back, and ``urls`` ingests an explicit list. This is the lever
that makes the corpus open-ended: anything reachable and readable on the
internet can enter it, the curated feeds are just the high-signal floor.

Fetch failures are returned as failures, never raised: one dead feed costs the
run that feed's documents and nothing else.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

from financeharness.rag.store import Document

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept": "application/json, text/html, application/xml;q=0.9, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_HTTP_TIMEOUT_S = 60.0

KEV_URL = (
    "https://www.cisa.gov/sites/default/files/feeds/"
    "known_exploited_vulnerabilities.json"
)
NVD_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"
ATTACK_STIX_URL = (
    "https://raw.githubusercontent.com/mitre-attack/attack-stix-data/master/"
    "{domain}/{domain}.json"
)
ATTACK_DOMAINS = ("enterprise-attack", "mobile-attack", "ics-attack")
OWASP_CHEATSHEETS_API = (
    "https://api.github.com/repos/OWASP/CheatSheetSeries/contents/cheatsheets"
)
# The CISA best-practices hub the corpus crawls for guidance pages.
CISA_BEST_PRACTICES_URL = "https://www.cisa.gov/topics/cybersecurity-best-practices"

# Editorial feeds — advisories and research write-ups, refreshed by re-ingesting.
NEWS_FEEDS = {
    "krebs": "https://krebsonsecurity.com/feed/",
    "hacker-news": "https://feeds.feedburner.com/TheHackersNews",
    "schneier": "https://www.schneier.com/feed/atom/",
    "unit42": "https://unit42.paloaltonetworks.com/feed/",
    "project-zero": "https://googleprojectzero.blogspot.com/feeds/posts/default",
    "google-tag": "https://blog.google/threat-analysis-group/rss/",
    "msrc": "https://msrc.microsoft.com/blog/feed/",
}


@dataclass
class FetchContext:
  """Everything a source fetcher may need, injected rather than imported.

  ``fetcher`` and ``backend`` are the harness's page fetcher and search backend;
  passing them in keeps ingestion testable offline and means the corpus reads
  pages exactly the way `visit` does.
  """

  limit: int = 200
  query: str = ""
  urls: list = field(default_factory=list)
  concurrency: int = 8
  max_doc_chars: int = 200_000
  fetcher: object = None
  backend: object = None


@dataclass
class SourceResult:
  """What one source produced, plus any non-fatal failures worth reporting."""

  documents: list = field(default_factory=list)
  errors: list = field(default_factory=list)


# HTTP helpers --------------------------------------------------------- #
async def _get(url, *, params=None, timeout=_HTTP_TIMEOUT_S):
  """GET a URL and return the raw ``httpx.Response`` (raises on HTTP error)."""
  import httpx

  async with httpx.AsyncClient(
      follow_redirects=True, timeout=timeout, headers=_HEADERS
  ) as client:
    resp = await client.get(url, params=params)
    resp.raise_for_status()
    return resp


async def _get_json(url, *, params=None, timeout=_HTTP_TIMEOUT_S):
  resp = await _get(url, params=params, timeout=timeout)
  return json.loads(resp.text)


async def _page_texts(ctx, entries):
  """Fetch ``[(url, title, fallback_text)]`` concurrently into documents.

  Falls back to the feed's own summary when a page can't be read, so a
  bot-walled article still contributes its abstract instead of vanishing.
  """
  from financeharness.tools.research.visit_fetch import http_fetch

  fetcher = ctx.fetcher or http_fetch
  sem = asyncio.Semaphore(max(1, ctx.concurrency))

  async def one(url, title, fallback):
    async with sem:
      try:
        page = await fetcher(url)
        text = page.text if getattr(page, "ok", False) else ""
      except Exception as exc:  # noqa: BLE001 — a bad page is data, not a crash
        text, page = "", exc
    if not text:
      text = fallback or ""
    if not text.strip():
      return None, f"{url}: no readable content"
    return (url, title, text[: ctx.max_doc_chars]), None

  results = await asyncio.gather(*(one(u, t, f) for u, t, f in entries))
  return [r for r, _ in results if r], [e for _, e in results if e]


# Structured feeds ------------------------------------------------------ #
async def fetch_cisa_kev(ctx):
  """CISA Known Exploited Vulnerabilities — CVEs with confirmed exploitation.

  The single highest-signal patch-priority list there is: every entry is a
  vulnerability someone is actually being attacked with, with a due date.
  """
  data = await _get_json(KEV_URL)
  vulns = data.get("vulnerabilities", [])
  if ctx.query:
    needle = ctx.query.lower()
    vulns = [
        v for v in vulns if needle in json.dumps(v).lower()
    ]
  docs = []
  for v in vulns[-ctx.limit :]:  # newest additions are appended last
    cve = v.get("cveID", "")
    title = f"{cve} — {v.get('vulnerabilityName', '')}".strip(" —")
    body = "\n\n".join(
        part
        for part in (
            f"CVE: {cve}",
            f"Vendor/Project: {v.get('vendorProject', '')}",
            f"Product: {v.get('product', '')}",
            f"Date added to KEV: {v.get('dateAdded', '')}",
            f"Remediation due: {v.get('dueDate', '')}",
            f"Known ransomware campaign use: {v.get('knownRansomwareCampaignUse', '')}",
            f"Description: {v.get('shortDescription', '')}",
            f"Required action: {v.get('requiredAction', '')}",
            f"Notes: {v.get('notes', '')}",
            f"CWEs: {', '.join(v.get('cwes', []) or [])}",
        )
        if part.split(": ", 1)[-1].strip()
    )
    docs.append(
        Document(
            url=f"https://nvd.nist.gov/vuln/detail/{cve}",
            title=title,
            text=body,
            source="cisa-kev",
            meta={
                "cve": cve,
                "date_added": v.get("dateAdded", ""),
                "due_date": v.get("dueDate", ""),
                "ransomware": v.get("knownRansomwareCampaignUse", ""),
                "catalog_version": data.get("catalogVersion", ""),
            },
        )
    )
  return SourceResult(documents=docs)


async def fetch_nvd(ctx):
  """NVD CVE records — the authoritative vulnerability database.

  ``query`` becomes NVD's ``keywordSearch`` (e.g. "apache struts"); without one
  the most recently modified CVEs are pulled. The public API is rate-limited to
  a handful of requests per rolling window without a key, so pages are fetched
  serially with a pause — set ``NVD_API_KEY`` for a higher ceiling.
  """
  import os

  page_size = min(200, max(1, ctx.limit))
  headers_key = os.environ.get("NVD_API_KEY", "")
  docs, errors, start = [], [], 0
  while len(docs) < ctx.limit:
    params = {"resultsPerPage": page_size, "startIndex": start}
    if ctx.query:
      params["keywordSearch"] = ctx.query
    try:
      import httpx

      async with httpx.AsyncClient(
          follow_redirects=True,
          timeout=_HTTP_TIMEOUT_S,
          headers={**_HEADERS, **({"apiKey": headers_key} if headers_key else {})},
      ) as client:
        resp = await client.get(NVD_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 — partial results beat no results
      errors.append(f"nvd page {start}: {type(exc).__name__}: {exc}")
      break
    items = data.get("vulnerabilities", [])
    if not items:
      break
    for item in items:
      cve = item.get("cve", {})
      cve_id = cve.get("id", "")
      descriptions = [
          d.get("value", "")
          for d in cve.get("descriptions", [])
          if d.get("lang") == "en"
      ]
      metrics = cve.get("metrics", {})
      severity = ""
      for key in ("cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        entries = metrics.get(key) or []
        if entries:
          cvss = entries[0].get("cvssData", {})
          severity = (
              f"CVSS {cvss.get('version', '')} {cvss.get('baseScore', '')}"
              f" ({cvss.get('baseSeverity', entries[0].get('baseSeverity', ''))})"
              f" {cvss.get('vectorString', '')}"
          ).strip()
          break
      weaknesses = sorted(
          {
              d.get("value", "")
              for w in cve.get("weaknesses", [])
              for d in w.get("description", [])
              if d.get("value", "").startswith("CWE-")
          }
      )
      refs = [r.get("url", "") for r in cve.get("references", [])][:10]
      body = "\n\n".join(
          part
          for part in (
              f"CVE: {cve_id}",
              f"Published: {cve.get('published', '')}",
              f"Last modified: {cve.get('lastModified', '')}",
              f"Status: {cve.get('vulnStatus', '')}",
              f"Severity: {severity}",
              f"Weaknesses: {', '.join(weaknesses)}",
              "Description: " + " ".join(descriptions),
              "References: " + " ".join(refs),
          )
          if part.split(": ", 1)[-1].strip()
      )
      docs.append(
          Document(
              url=f"https://nvd.nist.gov/vuln/detail/{cve_id}",
              title=f"{cve_id} ({severity or 'no CVSS'})",
              text=body,
              source="nvd-cve",
              meta={
                  "cve": cve_id,
                  "published": cve.get("published", ""),
                  "severity": severity,
                  "cwes": weaknesses,
              },
          )
      )
      if len(docs) >= ctx.limit:
        break
    start += len(items)
    if start >= data.get("totalResults", 0):
      break
    await asyncio.sleep(6.5 if not headers_key else 0.7)  # public rate limit
  return SourceResult(documents=docs, errors=errors)


_ATTACK_TYPES = {
    "attack-pattern": "Technique",
    "course-of-action": "Mitigation",
    "intrusion-set": "Group",
    "malware": "Software (malware)",
    "tool": "Software (tool)",
    "campaign": "Campaign",
    "x-mitre-data-source": "Data source",
    "x-mitre-tactic": "Tactic",
}


def _attack_reference(obj):
  """The canonical attack.mitre.org URL and ATT&CK id for a STIX object."""
  for ref in obj.get("external_references", []):
    if ref.get("source_name", "").startswith("mitre"):
      return ref.get("url", ""), ref.get("external_id", "")
  return "", ""


async def fetch_mitre_attack(ctx):
  """MITRE ATT&CK — the adversary tactics/techniques knowledge base.

  Downloads the official STIX 2.1 bundle and turns every technique, mitigation,
  group, software, campaign and tactic into its own document, so retrieval can
  answer "how is credential dumping detected?" with the technique page that
  defines it. ``query`` selects the domain (``enterprise``/``mobile``/``ics``);
  the default is enterprise.
  """
  domain = (ctx.query or "enterprise").strip().lower()
  if not domain.endswith("-attack"):
    domain = f"{domain}-attack"
  if domain not in ATTACK_DOMAINS:
    return SourceResult(
        errors=[f"unknown ATT&CK domain {domain!r}; try {', '.join(ATTACK_DOMAINS)}"]
    )
  bundle = await _get_json(
      ATTACK_STIX_URL.format(domain=domain), timeout=180.0
  )
  docs = []
  for obj in bundle.get("objects", []):
    kind = _ATTACK_TYPES.get(obj.get("type", ""))
    if kind is None or obj.get("revoked") or obj.get("x_mitre_deprecated"):
      continue
    url, attack_id = _attack_reference(obj)
    if not url:
      continue
    tactics = [
        p.get("phase_name", "")
        for p in obj.get("kill_chain_phases", [])
        if p.get("kill_chain_name", "").startswith("mitre")
    ]
    body = "\n\n".join(
        part
        for part in (
            f"ATT&CK ID: {attack_id}",
            f"Type: {kind}",
            f"Name: {obj.get('name', '')}",
            f"Tactics: {', '.join(tactics)}",
            f"Platforms: {', '.join(obj.get('x_mitre_platforms', []) or [])}",
            f"Data sources: {', '.join(obj.get('x_mitre_data_sources', []) or [])}",
            f"Aliases: {', '.join(obj.get('aliases', []) or [])}",
            f"Description: {obj.get('description', '')}",
            f"Detection: {obj.get('x_mitre_detection', '')}",
        )
        if part.split(": ", 1)[-1].strip()
    )
    docs.append(
        Document(
            url=url,
            title=f"{attack_id} {obj.get('name', '')} ({kind})".strip(),
            text=body[: ctx.max_doc_chars],
            source="mitre-attack",
            meta={
                "attack_id": attack_id,
                "kind": kind,
                "domain": domain,
                "tactics": tactics,
                "version": bundle.get("x_mitre_version", ""),
            },
        )
    )
  docs.sort(key=lambda d: d.meta.get("attack_id", ""))
  return SourceResult(documents=docs[: ctx.limit])


async def fetch_owasp(ctx):
  """OWASP Cheat Sheet Series — practitioner guidance on defending software.

  The repository's markdown is fetched directly, so each cheat sheet lands as
  one document of real prose rather than a rendered page's navigation chrome.
  """
  try:
    listing = await _get_json(OWASP_CHEATSHEETS_API)
  except Exception as exc:  # noqa: BLE001
    return SourceResult(errors=[f"owasp listing: {type(exc).__name__}: {exc}"])
  files = [f for f in listing if f.get("name", "").endswith(".md")]
  if ctx.query:
    needle = ctx.query.lower().replace(" ", "_")
    files = [f for f in files if needle in f["name"].lower()]
  files = files[: ctx.limit]
  sem = asyncio.Semaphore(max(1, ctx.concurrency))

  async def one(entry):
    async with sem:
      try:
        resp = await _get(entry["download_url"])
      except Exception as exc:  # noqa: BLE001
        return None, f"{entry['name']}: {type(exc).__name__}: {exc}"
    name = entry["name"].removesuffix(".md").replace("_", " ")
    return (
        Document(
            url=(
                "https://cheatsheetseries.owasp.org/cheatsheets/"
                f"{entry['name'].removesuffix('.md')}.html"
            ),
            title=f"OWASP {name}",
            text=resp.text[: ctx.max_doc_chars],
            source="owasp",
            meta={"file": entry["name"]},
        ),
        None,
    )

  pairs = await asyncio.gather(*(one(f) for f in files))
  return SourceResult(
      documents=[d for d, _ in pairs if d],
      errors=[e for _, e in pairs if e],
  )


# Editorial feeds ------------------------------------------------------- #
def parse_feed(xml_text):
  """Parse an RSS 2.0 or Atom feed into ``[(url, title, summary)]``.

  Uses the stdlib parser — a feed is a handful of fields and does not justify a
  dependency. Unparseable XML yields an empty list rather than raising.
  """
  try:
    root = ElementTree.fromstring(xml_text.strip())
  except ElementTree.ParseError:
    return []
  entries = []
  # RSS 2.0
  for item in root.iter("item"):
    link = (item.findtext("link") or "").strip()
    title = (item.findtext("title") or "").strip()
    summary = (
        item.findtext("description")
        or item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded")
        or ""
    )
    if link:
      entries.append((link, title, _strip_html(summary)))
  # Atom
  atom = "{http://www.w3.org/2005/Atom}"
  for entry in root.iter(f"{atom}entry"):
    link = ""
    for link_el in entry.findall(f"{atom}link"):
      rel = link_el.get("rel", "alternate")
      if rel == "alternate" and link_el.get("href"):
        link = link_el.get("href")
        break
    title = (entry.findtext(f"{atom}title") or "").strip()
    summary = (
        entry.findtext(f"{atom}summary")
        or entry.findtext(f"{atom}content")
        or ""
    )
    if link:
      entries.append((link, title, _strip_html(summary)))
  return entries


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text):
  import html

  return html.unescape(_TAG_RE.sub(" ", text or "")).strip()


def _feed_fetcher(feed_name, feed_url):
  """Build a source fetcher for one RSS/Atom feed."""

  async def fetch(ctx):
    try:
      resp = await _get(feed_url)
    except Exception as exc:  # noqa: BLE001
      return SourceResult(errors=[f"{feed_name}: {type(exc).__name__}: {exc}"])
    entries = parse_feed(resp.text)[: ctx.limit]
    if not entries:
      return SourceResult(errors=[f"{feed_name}: no entries parsed"])
    fetched, errors = await _page_texts(ctx, entries)
    return SourceResult(
        documents=[
            Document(
                url=url,
                title=title,
                text=text,
                source=f"news:{feed_name}",
                meta={"feed": feed_url},
            )
            for url, title, text in fetched
        ],
        errors=errors,
    )

  return fetch


# CISA guidance --------------------------------------------------------- #
_HREF_RE = re.compile(r'href=["\']([^"\'#]+)', re.IGNORECASE)
_CISA_GUIDANCE_PATHS = (
    "/topics/",
    "/secure-our-world/",
    "/resources-tools/resources/",
    "/news-events/",
)


async def fetch_cisa_best_practices(ctx):
  """CISA cybersecurity best practices — the guidance hub and the pages it links.

  Fetches https://www.cisa.gov/topics/cybersecurity-best-practices, follows the
  guidance links on it (topics, Secure Our World, resources), and ingests each
  page's extracted text. CISA fronts www.cisa.gov with a bot filter that denies
  some networks outright (a 403 on every page here means the *network* is
  blocked, not the code); the KEV feed under /sites/default/files/ is served
  from a different path and stays reachable either way.
  """
  root = ctx.query or CISA_BEST_PRACTICES_URL
  try:
    resp = await _get(root)
    html_text = resp.text
  except Exception as exc:  # noqa: BLE001
    return SourceResult(
        errors=[
            f"{root}: {type(exc).__name__}: {exc} — CISA's edge blocks some"
            " networks; retry from an unfiltered network"
        ]
    )
  links, seen = [], {root}
  for href in _HREF_RE.findall(html_text):
    url = urljoin(root, href.strip())
    parsed = urlparse(url)
    if parsed.netloc != urlparse(root).netloc or url in seen:
      continue
    if not any(parsed.path.startswith(p) for p in _CISA_GUIDANCE_PATHS):
      continue
    seen.add(url)
    links.append((url, "", ""))
  entries = [(root, "CISA Cybersecurity Best Practices", "")] + links[
      : max(0, ctx.limit - 1)
  ]
  fetched, errors = await _page_texts(ctx, entries)
  return SourceResult(
      documents=[
          Document(
              url=url,
              title=title or url.rstrip("/").rsplit("/", 1)[-1].replace("-", " "),
              text=text,
              source="cisa-best-practices",
              meta={"hub": root},
          )
          for url, title, text in fetched
      ],
      errors=errors,
  )


# Open web -------------------------------------------------------------- #
async def fetch_web(ctx):
  """Open-web ingestion — run ``query`` through search and ingest the results.

  The unbounded lever: the curated feeds are a floor, this is how anything else
  on the internet enters the corpus. Multiple queries can be separated by ``;``.
  """
  from financeharness.tools.research.search_backends import DdgsBackend

  backend = ctx.backend or DdgsBackend()
  queries = [q.strip() for q in (ctx.query or "").split(";") if q.strip()]
  if not queries:
    return SourceResult(errors=["web source needs a query"])
  per = max(1, ctx.limit // len(queries))
  entries, seen, errors = [], set(), []
  for query in queries:
    try:
      hits = await backend.search(query, per)
    except Exception as exc:  # noqa: BLE001 — one query failing isn't the batch
      errors.append(f"search {query!r}: {type(exc).__name__}: {exc}")
      continue
    for hit in hits:
      if hit.url in seen:
        continue
      seen.add(hit.url)
      entries.append((hit.url, hit.title, hit.snippet))
  if not entries:
    return SourceResult(errors=errors or ["no search results"])
  fetched, fetch_errors = await _page_texts(ctx, entries[: ctx.limit])
  return SourceResult(
      documents=[
          Document(
              url=url,
              title=title,
              text=text,
              source="web",
              meta={"queries": queries},
          )
          for url, title, text in fetched
      ],
      errors=errors + fetch_errors,
  )


async def fetch_urls(ctx):
  """Ingest an explicit list of URLs (``ctx.urls``) — the manual escape hatch."""
  urls = [u for u in ctx.urls if u.strip()]
  if not urls:
    return SourceResult(errors=["urls source needs at least one URL"])
  fetched, errors = await _page_texts(
      ctx, [(u, "", "") for u in urls[: ctx.limit]]
  )
  return SourceResult(
      documents=[
          Document(url=url, title=title or url, text=text, source="urls")
          for url, title, text in fetched
      ],
      errors=errors,
  )


@dataclass(frozen=True)
class SourceSpec:
  """A named ingestion source: what it is, how to fetch it, and how much.

  ``default_limit`` is the per-source document cap when the caller names none.
  A structured catalog wants all of itself — ATT&CK is meaningless with a
  quarter of its techniques — while a feed or a web crawl wants a page or two,
  because every one of those documents costs a page fetch.
  """

  name: str
  description: str
  fetch: object
  needs_query: bool = False
  default_limit: int = 200


SOURCES = {
    spec.name: spec
    for spec in (
        SourceSpec(
            "mitre-attack",
            "MITRE ATT&CK techniques, tactics, groups, software and mitigations"
            " (STIX bundle; query selects enterprise/mobile/ics).",
            fetch_mitre_attack,
            default_limit=10_000,  # the whole domain; ~1.8k objects
        ),
        SourceSpec(
            "cisa-kev",
            "CISA Known Exploited Vulnerabilities — CVEs with confirmed"
            " in-the-wild exploitation and remediation due dates.",
            fetch_cisa_kev,
            default_limit=10_000,  # the whole catalog
        ),
        SourceSpec(
            "cisa-best-practices",
            "CISA cybersecurity best-practices hub and the guidance pages it"
            " links (topics, Secure Our World, resources).",
            fetch_cisa_best_practices,
            default_limit=60,  # the hub plus the guidance it links
        ),
        SourceSpec(
            "nvd-cve",
            "NVD CVE records with CVSS severity and CWE mappings; query is an"
            " NVD keyword search, otherwise the most recent CVEs.",
            fetch_nvd,
            default_limit=200,  # the public API is rate-limited; page politely
        ),
        SourceSpec(
            "owasp",
            "OWASP Cheat Sheet Series — application-security guidance on"
            " authentication, injection, secrets, hardening and more.",
            fetch_owasp,
            default_limit=200,  # ~120 cheat sheets
        ),
        SourceSpec(
            "web",
            "Open-web search ingestion — anything reachable and readable;"
            " requires a query (';'-separated for several).",
            fetch_web,
            needs_query=True,
            default_limit=20,  # every document here is a live page fetch
        ),
        SourceSpec(
            "urls",
            "Ingest an explicit list of URLs through the harness page reader.",
            fetch_urls,
            default_limit=50,
        ),
        *(
            SourceSpec(
                f"news:{name}",
                f"Security reporting and research from {name} ({url}).",
                _feed_fetcher(name, url),
                default_limit=25,  # a feed page, not an archive crawl
            )
            for name, url in NEWS_FEEDS.items()
        ),
    )
}

# The default ingest set: authoritative, key-free, and broadly useful.
DEFAULT_SOURCES = ("mitre-attack", "cisa-kev", "owasp", "cisa-best-practices")


def resolve_sources(names):
  """Map names to specs; ``news:*`` expands to every editorial feed.

  Returns ``(specs, unknown)`` so a caller can report typos instead of silently
  ingesting less than asked.
  """
  specs, unknown = [], []
  for name in names:
    if name in ("all", "*"):
      specs += list(SOURCES.values())
    elif name in ("news", "news:*"):
      specs += [s for s in SOURCES.values() if s.name.startswith("news:")]
    elif name in SOURCES:
      specs.append(SOURCES[name])
    else:
      unknown.append(name)
  seen, out = set(), []
  for spec in specs:
    if spec.name not in seen:
      seen.add(spec.name)
      out.append(spec)
  return out, unknown
