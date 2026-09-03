---
name: cyber-threat-brief
description: Grounded cyber-security brief on a CVE, threat actor, technique or product — exploitation status, affected versions, ATT&CK mapping, and the controls that actually mitigate it.
tags: [cyber, security, threat, rag]
requires_tools:
  - knowledge_cyber_search
  - knowledge_cyber_status
  - knowledge_cyber_ingest
---

# Cyber threat brief

Answer a security question from the retrieval corpus first, the open web second,
and cite every claim either way.

## Workflow

1.  `knowledge_cyber_status` — see what the corpus holds. An empty corpus is not
    an absence of evidence; it is an ingest you have not run.
2.  `knowledge_cyber_search` with the specific identifier *and* the plain-English
    question (two calls beat one vague one): `CVE-2021-44228` retrieves the KEV
    and NVD records, "how is Log4Shell exploited in the wild" retrieves the
    technique and reporting.
3.  Missing coverage? `knowledge_cyber_ingest`:
    -   `cisa-kev` / `nvd-cve` for vulnerability records (`query` = NVD keyword),
    -   `mitre-attack` for techniques, groups and mitigations,
    -   `owasp` / `cisa-best-practices` for defensive guidance,
    -   `news:*` for recent reporting, `web` with a query for anything else.
    Then search again.
4.  Anything still uncovered — a vendor advisory, this week's disclosure — goes
    through `search` + `visit` on the open web.
5.  Write the brief. Corpus passages carry `[N]` markers exactly as visited pages
    do; use them.

## What a brief contains

-   **What it is** — the vulnerability/technique/actor in two sentences, with the
    canonical identifier (CVE, CWE, ATT&CK id).
-   **Severity and exploitation status** — CVSS, and whether it is on CISA's KEV
    list (exploited in the wild) with its remediation due date. Distinguish
    "scored high" from "being used against people right now"; only KEV or named
    reporting supports the second.
-   **Affected surface** — vendor, product, versions, platforms.
-   **ATT&CK mapping** — the tactics and techniques involved, by id.
-   **Mitigations and detections** — the ATT&CK mitigations, vendor fix, and the
    OWASP/CISA guidance that applies. Prefer the concrete control over the
    generic advice.
-   **What is unknown** — say so plainly when the corpus and the web disagree or
    go quiet.

## Principles

-   Identifiers are load-bearing. A brief that says "a recent Fortinet flaw"
    instead of `CVE-2024-21762` cannot be acted on.
-   Never infer exploitation, attribution, or a patch version. Those come from a
    source or they don't go in the brief.
-   Corpus records are point-in-time snapshots: a KEV entry ingested last month
    is stale on this month's additions. Re-ingest before relying on recency, and
    say which date the evidence is from.
-   Defensive framing throughout — what to patch, detect, and harden. Do not
    write exploitation steps.
