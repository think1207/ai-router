# Snippets

This directory holds reference code snippets, prototypes, and design drafts created in external chats or experiments.
It is intended for maintenance and exploration—reviewing ideas here before deciding how to design and integrate them into `src/ai_router/`.

## Directory Convention

- `snippets/<YYYYMMDD>-<topic>.<ext>` — Active drafts or snippets under review for future implementation.
- `snippets/archive/` — Historical prototypes that have already been integrated or superseded (kept for reference).

## Workflow

1. Drop raw snippets from another chat into `snippets/` with a descriptive filename (e.g. `20261121-cost-tracking.py` or `20261121-pr-review-idea.md`).
2. Review the snippet against the modular architecture in `src/ai_router/` to plan where each piece belongs.
3. Implement the finalized version inside `src/ai_router/` with tests under `tests/`.
4. Move integrated snippets into `snippets/archive/` or remove them once they are no longer needed.

> Note: `snippets/` is excluded from the built Python package (`src/ai_router/` is the only packaged source directory).
