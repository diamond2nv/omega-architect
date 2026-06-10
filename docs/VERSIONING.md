# Document Versioning Policy

> **Last updated**: 2026-06-10  
> **Applies to repo version**: `0.1.0`

## Rule

Every document in `docs/` MUST have a header block at the top with:

```markdown
> **Doc version**: `0.1.0` — matches repo version bc6eca9
> **Last updated**: 2026-06-10
```

## When to bump

| Event | Action |
|-------|--------|
| **Repo version changes** (pyproject.toml `version`) | Update ALL doc headers to new version |
| **Minor doc fix** (typo, clarification) | Update `Last updated` only |
| **Major doc rewrite** | Update both fields |
| **New document added** | Add header at creation time |

## How version flows

```
pyproject.toml  ─── version = "0.1.0"
     │
     ├──→ omega/__init__.py     (reads via importlib.metadata)
     │
     ├──→ docs/VERSIONING.md    (this file, manual sync)
     │
     └──→ docs/index.qmd        (table with version per doc)
             │
             └──→ quarto render → _site/  (built docs)
```

The **single source of truth** is `pyproject.toml`. All other version references
should either read from it programmatically or be manually synced when bumping.

## Automation (future)

Future CI pipeline should:
1. On push to main with version bump → run `quarto render docs/`
2. Deploy `_site/` to GitHub Pages or internal doc server
3. Optionally auto-generate PDF release artifacts
