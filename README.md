# Personal Brain (core)

An AI "second brain" and self-cloning platform, built around a chat bot, a
Karpathy-style knowledge base, and a set of self-improvement loops.

This repository is the **core system only**. It contains no personal or
business data — all real conversations, knowledge, personas, and sales figures
live under `data/` at runtime and are `.gitignore`d. Example values in code,
tests, and docs have been replaced with fictional placeholders, and the export
is gated by a scanner that refuses to publish if anything real survives
(see [SECURITY.md](SECURITY.md)).

It has been running in production since April 2026 as the assistant for a
retail CEO and, in a separate public-facing persona, for ~120 employees.
**1,600+ tests** run without any private data.

## Why it might be worth reading

Most of what is interesting here is not the RAG pipeline — it is the set of
guardrails that grew out of things going wrong in production. A few that
generalize beyond this system:

- **Superlatives cannot be answered by retrieval.** Asked for a record high,
  the bot answered with the maximum of whatever chunks the search happened to
  return — a real number, correctly hedged, and still wrong, because a higher
  one existed in the same file. Ranking is now computed deterministically and
  injected; the model is told not to look for the maximum itself.
  (`brain_wiki_helpers/record_inject.py`)
- **A gate that refuses is not the same as a gate that is safe.** An
  append-guard correctly stopped an oversized write 191 times in a row — and
  discarded the content each time. Refusing and preserving are separate
  requirements. (`brain_wiki_helpers/wiki_append.py`)
- **The classifier is itself an injection target.** The privacy filter embeds
  candidate text into its own prompt, so the text can impersonate instructions
  to the classifier. The defense is deterministic and runs before the model,
  so swapping the model does not change it. (`privacy_gate.py`)
- **Guards written as vocabulary lists die to paraphrase.** A guard that
  blocked the bot from turning its own "I don't have that data" into a wiki
  page was bypassed by rewording. It was replaced with attribution: an edit
  grounded only in the assistant's own utterances is never applied
  automatically. (`scripts/clone_auto_improve.py`)
- **A rebuild that reads only its source silently truncates.** History files
  were rebuilt from scratch each run and fully overwritten, so a 30-day
  retention policy upstream quietly shortened the history downstream. Inputs
  that vary in coverage need to be unioned, not overwritten.
- **Alerts state a hypothesis, not a cause.** One reported a duplicate-append
  loop; every line in the file was unique. The real cause was elsewhere.

Each of these is documented where it lives, in the module that carries the
scar.

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

## Where to start

| If you are… | Read |
|---|---|
| deciding whether this is relevant at all | the section above, then `docs/porting/00_CONCEPT_DECK_FOR_CEO.md` |
| porting it to another person / company | `docs/porting/GENERIC_VS_SPECIFIC.md` — what is reusable vs what must be replaced |
| standing it up from zero | `docs/porting/SETUP_FROM_ZERO.md` |
| interested in the privacy model | `privacy_gate.py`, `brain_wiki_helpers/visibility.py`, `docs/porting/PRIVACY_COMPLIANCE.md` |

## License

MIT — see [LICENSE](LICENSE).
