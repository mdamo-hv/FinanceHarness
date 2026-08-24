<p align="center">
  <img src="assets/FinanceHarness.svg" alt="FinanceHarness" width="60%">
</p>

<div align="center">
  <a href="https://www.readme-i18n.com/Yijia-Xiao/FinanceHarness?lang=de">Deutsch</a> | 
  <a href="https://www.readme-i18n.com/Yijia-Xiao/FinanceHarness?lang=es">Español</a> | 
  <a href="https://www.readme-i18n.com/Yijia-Xiao/FinanceHarness?lang=fr">français</a> | 
  <a href="https://www.readme-i18n.com/Yijia-Xiao/FinanceHarness?lang=ja">日本語</a> | 
  <a href="https://www.readme-i18n.com/Yijia-Xiao/FinanceHarness?lang=ko">한국어</a> | 
  <a href="https://www.readme-i18n.com/Yijia-Xiao/FinanceHarness?lang=pt">Português</a> | 
  <a href="https://www.readme-i18n.com/Yijia-Xiao/FinanceHarness?lang=ru">Русский</a> | 
  <a href="https://www.readme-i18n.com/Yijia-Xiao/FinanceHarness?lang=zh">中文</a>
</div>

---

# FinanceHarness: Autonomous Financial Deep Research Framework

<p align="center">
  <a href="https://arxiv.org/pdf/2607.27853">
    <img src="assets/schema.svg" alt="The FinanceHarness stack: orchestration, capability, tools, runtime and model serving" width="100%">
  </a>
</p>

## Overview

**FinanceHarness is an autonomous financial deep research framework.** Ask a
question; FinanceHarness plans, gathers evidence, runs the analysis, and writes a
cited report.

- **Research and compute.** Web search and reading, company and market data, and
  valuation and risk computed from the gathered data, not guessed.
- **Reference-chaining.** One tool's output feeds the next by reference
  (`prev:<call_id>.<path>`), so a price series becomes a correlation with no
  retyping.
- **Progressive disclosure.** The core loop stays in the prompt; everything else
  waits in a catalog and loads on request, so breadth costs nothing until used.
- **Extensible without code.** A `SKILL.md` file composes existing tools into a
  reusable workflow. Drop one in and it is discovered.
- **Grounded.** Every figure traces back to the tool or source it came from.
- **Connected.** MCP in both directions: borrow tools from other MCP servers
  (including a local process holding private data), and serve this harness's own
  toolkit to any MCP client.
- **Watchable.** A React console streams the run — plan, tool calls, the data
  behind them, sources, and the report as it is written.

## Get Started

### Prerequisites

FinanceHarness needs [`uv`](https://docs.astral.sh/uv/):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Install

```bash
git clone https://github.com/Yijia-Xiao/FinanceHarness.git
cd FinanceHarness
uv tool install .  # installs the `financeharness` command (alias `fh`)
```

From a source checkout, `uv sync` sets up the environment and
`uv run python main.py ...` (or `uv run fh ...`) runs the same CLI without
installing.

### Configure a backbone

Backbones are named by the provider or runtime that serves them. The default is
`gemini` (a cloud backbone that requires only an API key). Configure whichever
suits the machine you are on:

| Backbone | Serves | Infra | Needs |
|---|---|---|---|
| `gemini` | `gemini-3.6-flash` (Google) | cloud | `GEMINI_API_KEY` |
| `gpt` | `gpt-5.6` (OpenAI) | cloud | `OPENAI_API_KEY` |
| `qwen` | Qwen (open-weight) | GPU server | a local vLLM stack |

```bash
export GEMINI_API_KEY=...  # makes the Google/Gemini backbone available
export OPENAI_API_KEY=...  # makes the OpenAI/GPT backbone available

# Or point at an OpenAI-compatible vLLM stack you serve yourself:
export FH_QWEN_BASE_URL=http://gpu-box:8000/v1
export FH_QWEN_READER_BASE_URL=http://gpu-box:8001/v1   # the long-document reader
```

Pick a backbone per run with `--profile NAME`, or change the default in
[`configs/providers.json`](configs/providers.json) (or via `FH_PROFILE`).

### Run

One-shot research. The cited report goes to stdout, progress to stderr, so
stdout stays pipeable:

```bash
fh -p "Estimate NVDA's intrinsic value with a DCF."
fh -p "Apple's competitive position in 2026?" --mode research
fh -p "..." --profile gpt --save run.json  # switch backbone; persist trajectory
echo "What's AAPL's P/E?" | fh -p          # question piped via stdin
fh --list                                  # backbones + skills; confirms the install
```

Options: `--mode {auto|research|analytical}`, `--profile NAME`, `--reader NAME`,
`--save PATH`, `--quiet`. Exit code is `0` when the agent produced an answer,
`1` otherwise.

Two sub-commands: `fh serve` (the HTTP+SSE service, and the React console when
built) and `fh mcp` (serve the harness to MCP clients). Both are covered below.

### Modes

Modes are prompt variants over a constant tool registry, so switching never
strands a trajectory. Set with `--mode`.

| Mode | Focus |
|---|---|
| `auto` | Full toolkit; the agent decides |
| `research` | Web-first deep research |
| `analytical` | Numbers-first: data and valuation tools, web for context |

A `-p` one-shot defaults to `research`.

## Tools

Seven tools are always visible; the rest sit in a catalog and are loaded on
demand with `load_tool`.

| Group | Tools |
|---|---|
| **Web research** | `search`, `visit`, `compose_citations` |
| **Core** | `calc`, `update_plan`, `load_tool`, `load_skill` |
| **Equity data** | `data_equity_reference`, `data_equity_prices`, `data_equity_fundamentals`, `data_equity_ratios`, `data_equity_comps`, `data_equity_estimates` |
| **Market data** | `data_market_rates`, `data_market_indices` |
| **Valuation** | `compute_valuation_dcf`, `compute_valuation_dcf_sensitivity`, `compute_valuation_wacc` |
| **Risk** | `compute_risk_correlation`, `compute_risk_var`, `compute_risk_beta` |
| **MCP** (optional) | `mcp_<server>_<tool>` — anything a configured MCP server exposes |

Company and market data are sourced through `yfinance`.

## Extend with skills

A **skill** is a `SKILL.md` (YAML frontmatter + a markdown body) that orchestrates
the existing tools into a reusable workflow, no code required. The model loads
one with `load_skill` when it fits the task. Bundled:

| Skill | What it does |
|---|---|
| `ticker-snapshot` | Quick structured overview of one equity (identity, ratios, price/trend). |
| `dcf-valuation` | Intrinsic value via DCF: fundamentals → CAPM/WACC → project and discount. |
| `relative-valuation` | Peer-median multiples applied to the company for an implied range. |
| `consensus-check` | Sell-side view (targets, estimates, ratings) corroborated against the web. |
| `equity-deep-dive` | The full workflow: qualitative picture + fundamentals + DCF + comps + consensus. |

Discovery runs in increasing precedence: bundled `financeharness/skills/` → a
project `./skills/` → `FH_SKILLS_DIR`. A project skill overrides a built-in by
name, and a malformed one is skipped rather than fatal.

## Web console (React)

`web/` is a React console over the service: one question at a time, streamed. It
shows the plan as the agent maintains it, every tool call with the data that came
back, the sources filling as pages are read, and the report as it is written —
so the evidence sits next to the answer instead of behind it.

```bash
make install web-install      # Python env + npm install
make serve                    # the API on :8080  (terminal 1)
make web                      # the console on :5173 (terminal 2)
```

The dev server proxies `/api` to the service, so the console talks to one origin
(`FH_API_URL` retargets the backend). For a single process, build it once and let
the service serve it:

```bash
make app        # web-build + serve → API and UI together on :8080
```

What the console gives you beyond the CLI: the live trace (rounds, tool
arguments, the grounding data behind each figure), the sources rail numbered to
match the report's `[N]` markers, the scoping dialog when a question is
genuinely ambiguous, multi-turn sessions with a context meter and one-click
compaction, a backbone picker, markdown/trajectory export, and a panel showing
which MCP data sources this run can reach.

### HTTP + SSE service

The service is useful on its own — for a remote client, an eval harness, or
anything programmatic:

```bash
fh serve  # HTTP+SSE on 127.0.0.1:8080
```

| Endpoint | Purpose |
|---|---|
| `POST /research` | run a trajectory; sync JSON, or SSE with `stream=true` |
| `POST /clarify` | scope a question before researching (fail-open) |
| `POST /compact` | summarize a session's older turns to free context |
| `GET /models` | backbones + credential availability |
| `GET /mcp` | configured MCP servers (`?probe=true` dials them) |
| `GET /sessions`, `/sessions/{id}`, `/status`, `/health` | session + readiness |

The SSE frame protocol is documented in
[`financeharness/service/events.py`](financeharness/service/events.py).

## MCP integration

The harness speaks the [Model Context Protocol](https://modelcontextprotocol.io)
in both directions.

### Borrow tools from other MCP servers (inbound)

Configure a server in [`configs/mcp.json`](configs/mcp.json) and its tools join
every run's catalog, in the deferred tier — the agent sees them alongside the
first-party tools and loads what the question needs. Its resources (files,
tables, documents) are bridged too, as a list/read pair, so a source with no
tools of its own is still readable.

Two transports:

| Transport | Config | For |
|---|---|---|
| stdio | `command` + `args` | a local process — **private data that never leaves the machine** |
| http | `url` + `headers` | a streamable-HTTP MCP endpoint, local or remote |

```json
{
  "servers": {
    "portfolio": {
      "command": "uv",
      "args": ["run", "python", "examples/mcp/local_portfolio.py"]
    },
    "internal-api": {
      "url": "https://mcp.internal.example.com/mcp",
      "headers": { "Authorization": "Bearer ${INTERNAL_MCP_TOKEN}" },
      "tools": ["lookup_customer"]
    }
  }
}
```

`${VAR}` is expanded from the environment, so tokens stay out of the file.
`tools` is an optional allowlist. A server that won't connect is reported and
skipped — the run keeps every tool it does have. `FH_MCP_DISABLE=1` turns the
integration off; `FH_MCP_CONFIG` points at a different file.

[`examples/mcp/local_portfolio.py`](examples/mcp/local_portfolio.py) is a working
local data source — a CSV of holdings exposed as three tools and a resource.
Enable the `portfolio` entry in `configs/mcp.json` and ask a question only your
own data can answer:

```bash
fh -p "Given my holdings, what is my concentration risk in semis?"
```

Borrowed tools appear as `mcp_<server>_<tool>` in the trace, so a trajectory
always says which figures came from outside the harness.

### Serve the harness to MCP clients (outbound)

`fh mcp` exposes the harness over MCP: every data, valuation, risk and research
tool with its real schema, the bundled skills as prompts *and* resources, and a
`deep_research` tool that runs the whole loop and returns a cited report.

```bash
fh mcp                          # stdio — what an MCP host spawns
fh mcp --http --port 8765       # streamable HTTP, for a remote client
fh mcp --list                   # what would be exposed, no client needed
```

For Claude Desktop (or any MCP client), point it at the checkout:

```json
{
  "mcpServers": {
    "financeharness": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/FinanceHarness", "fh", "mcp"],
      "env": { "GEMINI_API_KEY": "..." }
    }
  }
}
```

Calls route through the harness dispatcher, so an MCP client inherits the same
argument coercion, the same actionable error text, and the same reference
chaining a first-party run gets: every result carries a `call_id`, and
`prev:<call_id>.<path>` feeds one tool's output into the next without the client
ever holding the numbers.

```
data_equity_prices(ticker="AAPL", period="3mo")   → call_1
compute_risk_var(prices="prev:call_1.bars[*].close", confidence=0.95)
```

The data and compute tools need no API key at all — only `visit` and
`deep_research` call a model.

## Benchmark

The **FinanceGym** benchmark and leaderboard are maintained at
[google-research/finance_harness](https://github.com/google-research/google-research/tree/master/finance_harness).

## Citation

If you find *FinanceHarness* helpful, please cite our work.

```bibtex
@misc{xiao2026financeharnessautonomousfinancialdeep,
      title={FinanceHarness: Autonomous Financial Deep Research Framework}, 
      author={Yijia Xiao and Rujun Han and Yanfei Chen and Zifeng Wang and Ke Jiang and Zhongying CuiZhu and Vishy Tirumalashetty and Wei Wang and Burak Gokturk and Tomas Pfister and Chen-Yu Lee},
      year={2026},
      eprint={2607.27853},
      archivePrefix={arXiv},
      primaryClass={cs.CL},
      url={https://arxiv.org/abs/2607.27853}, 
}
```
