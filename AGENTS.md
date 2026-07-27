# Repository Guidelines

## Project Structure & Module Organization

This repository is currently specification-first. `openspec/config.yaml` selects the OpenSpec schema. Active changes live under `openspec/changes/<change-name>/`, with `proposal.md`, `design.md`, `tasks.md`, and capability deltas in `specs/<capability>/spec.md`. Local Codex workflows are stored in `.codex/skills/`.

The accepted design uses a Python package at `src/trade_agent/`, split into `core`, `agents`, `capabilities`, `adapters`, and `apps`. Each capability owns `domain`, `application`, `ports`, `tools`, `cards`, and a public `contracts.py`. The minimal Vue/TypeScript client lives in `web/`. Tests live in `tests/` and mirror these boundaries. Do not add implementation outside this layout without updating the OpenSpec design.

## Build, Test, and Development Commands

- `openspec list`: list active changes.
- `openspec status --change create-langgraph-trading-agent`: show artifact completion.
- `openspec show create-langgraph-trading-agent`: inspect the current proposal and deltas.
- `openspec validate --all --strict --no-interactive`: validate every change and spec before review.

- `uv sync --all-groups`: install locked phase-one Python development dependencies. Add `--extra models` only after architecture approval when implementing LiteLLM.
- `uv run ruff check . && uv run ruff format --check .`: lint and format-check Python.
- `uv run mypy && uv run pytest`: type-check and test the scaffold.
- `cd web && npm ci && npm run lint && npm run typecheck && npm run build`: verify the Web scaffold.

## Coding Style & Naming Conventions

Use four-space indentation for Python, type annotations at public boundaries, `snake_case` for modules/functions, and `PascalCase` for classes. Keep capability domain/application code independent of LangGraph, HTTP, and concrete providers. Agents depend only on `core` contracts. Concrete SDKs belong under `adapters`, and all process entry points use `apps/container.py` as the single composition root. Use lowercase kebab-case for OpenSpec change and capability names, such as `market-research`. Requirement language should be testable; pair each `MUST` with explicit `WHEN`/`THEN` scenarios.

## Testing Guidelines

Use `pytest` once the Python scaffold lands. Name files `test_<behavior>.py` and separate unit, integration, and live-provider tests. Prefer deterministic fakes for providers. Cover owner isolation, idempotent recovery, evidence provenance, LLM/quant-model separation, and the structural absence of broker execution. Run OpenSpec validation even for documentation-only changes.

## Commit & Pull Request Guidelines

Git history is unavailable in this checkout. Use an intent-first commit subject and, where useful, Lore trailers such as `Constraint:`, `Confidence:`, `Scope-risk:`, and `Tested:`. Keep commits narrowly scoped.

Pull requests should identify the OpenSpec change, summarize affected capabilities, link relevant tasks, and list exact verification commands. Call out migration, provider, security, or live-data risks; include screenshots only when a user-facing interface is added.

## Security & Configuration

Never commit credentials, provider tokens, market-data payloads, or user data. Preserve the core safety boundaries: US-listed securities only, sourced facts, explicit uncertainty, tenant isolation, human approval for side effects, and no broker order or account capabilities.
