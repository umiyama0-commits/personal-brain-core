# Personal Brain (core)

An AI "second brain" and self-cloning platform, built around a chat bot, a
Karpathy-style knowledge base, and a set of self-improvement loops.

This repository is the **core system only**. It contains no personal or
business data — all real conversations, knowledge, personas, and sales figures
live under `data/` at runtime and are `.gitignore`d. Example values in code,
tests, and docs have been replaced with fictional placeholders.

## What it does

- **Chat clone** — a bot ("clone") that answers as its owner, over LINE / LINE
  Works, grounded in a private knowledge base rather than free generation.
- **Knowledge base** — raw notes and imports are compiled into a wiki
  (`raw → LLM compile → wiki`), then retrieved with embeddings + reranking.
  Contextual retrieval, recency bias, and a multi-layer memory ontology.
- **Privacy gate** — a 3-stage filter (rules → LLM classification → PII
  scrub) that keeps private material out of public-facing responses.
- **Alignment / persona** — voice and form based interviews distilled into a
  persona, with coverage tracking and gap-targeted questioning.
- **Self-improvement loops** — nightly regression, hallucination checks,
  post-hoc fact verification, memory hygiene, and propose-only auto-edits.

## Layout

| Path | Purpose |
|------|---------|
| `main.py` | FastAPI webhook server + cron bootstrap + file watcher |
| `brain_wiki.py`, `brain_wiki_helpers/` | knowledge base: compile, retrieval, clone respond, domain/ontology helpers |
| `brain_index.py` | Chroma vector index |
| `privacy_gate.py` | 3-stage privacy filter |
| `brain_commands.py` | bot command handlers (`/brain`, `/teach`, `/clone`, …) |
| `services/`, `routes/`, `tasks/` | app services, API routers, background tasks |
| `clone_*.py`, `alignment_interview.py` | clone history/memory/feedback + persona interviews |
| `scripts/` | scrapers, quality loops, cron infra, extractors |
| `tests/` | pytest suite (runs without any private data) |
| `docs/` | architecture, development principles, porting guide |

Some optional feature-modules from the full system (market/whitespace
analysis, store-recommendation, analyst/consultant agents, a browser
extension, the sales-data pipeline) are **not** included here — they attach to
the core via the documented hub pattern (`docs/integrations/`), and a few
lazy-imported hooks for them remain in `main.py` as no-ops.

## Running the tests

```bash
pip install -r requirements.txt   # or the minimal subset for pure-logic tests
python3 -m pytest tests/ -q
```

The suite is self-contained (fixtures + `tmp_path`); it needs no `data/`,
no network, and no API keys.

## Configuration

Copy `.env.example` to `.env` and fill in your own values (LLM keys, LINE /
LINE Works credentials, etc.). Nothing in this repo contains real secrets.

## License

TODO — choose a license before making the repository public.
