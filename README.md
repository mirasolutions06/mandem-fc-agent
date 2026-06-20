# Mandem FC Agent

Self-hosted football social-media agent with Telegram approval, FastMCP tools,
SQLite state, async image stylization jobs, identity checks, fallbacks, and 67
passing tests.

Mandem watches football fixtures and news, drafts opinionated social posts,
finds or generates visual assets, sends drafts to an operator over Telegram, and
only publishes after approval. The interesting part is not "generate a caption";
it is the operating system around that caption: event polling, ranking, state,
approval, async jobs, image safety checks, deterministic fallbacks, and recovery.

## Demo

Screen recording coming soon: draft creation, Telegram approval, async
stylization, and the final queued post.

## Live Output

Example approved posts are published at
[@mandemfchq](https://www.instagram.com/mandemfchq/). The GitHub repo is
sanitized and does not include live deployment details, credentials, or private
operator state.

## What It Proves

- Agent tool design with 35 FastMCP tools, 3 resources, and 2 prompts.
- SQLite-backed state for fixtures, drafts, news, Telegram messages, and jobs.
- Human-in-the-loop approval via Telegram before anything is published.
- Async stylization jobs so long image calls do not block MCP RPCs.
- Image-source ladder: official player photos, news search, Wikimedia, Pexels,
  generated images, and deterministic Pillow composites.
- Identity and relevance checks before using AI-edited player imagery.
- Practical fallback behavior when APIs fail, images mutate, or live data is missing.
- Reproducible public test suite: `make test`.

## Quick Start

```bash
python3 -m venv .venv
. .venv/bin/activate
make install
make test
cp .env.example .env
make db-init
```

To run the MCP server locally:

```bash
MANDEM_DATA_DIR="$PWD/.mandem-data" python3 scripts/mandem_mcp.py
```

Real provider calls require environment variables in `.env` or a server env file
referenced by `MANDEM_ENV_FILE`. Tests do not require live secrets.

## Repository Layout

```text
.
├── scripts/
│   ├── mandem_mcp.py        # FastMCP stdio server
│   ├── mandem_db.py         # SQLite schema/query/cleanup helper
│   ├── smoke.py             # Environment and feed smoke checks
│   ├── mandem/              # Agent modules
│   └── tests/               # 67 tests
├── ARCHITECTURE.md
├── RUNBOOK.md
├── FAILURE_MODES.md
├── Makefile
├── requirements.txt
└── .github/workflows/ci.yml
```

## Docs

- [ARCHITECTURE.md](ARCHITECTURE.md) explains the pipeline and system boundaries.
- [RUNBOOK.md](RUNBOOK.md) covers local setup, server wiring, and useful commands.
- [FAILURE_MODES.md](FAILURE_MODES.md) lists known failure modes and mitigations.

## Public-Safe Notes

This repo is a sanitized proof repo. It intentionally excludes private workspace
memory, historical handoff docs, live hostnames, IPs, chat IDs, secrets, and
deployment-specific paths. Keep real credentials in `.env` locally or in a server
environment file outside git.
