# Architecture Decision Records (ADR)

This directory contains Architecture Decision Records for the scrapping-service project.

An ADR documents a significant design decision, including the context, options considered, and the chosen solution. ADRs help preserve institutional knowledge and enable informed decisions when revisiting past trade-offs.

## Current ADRs

| # | Title | Date | Status |
|---|-------|------|--------|
| 0001 | Plugin Model | 2026-11-05 | Accepted |

## Template

Each ADR follows the [MADR](https://adr.github.io/madr/) format:

```markdown
# ADR NNNN: Title

## Context

## Decision

## Consequences

## Alternatives Considered

## References
```

## Creating a New ADR

1. Copy the template from the repository's ADR template (if available) or use the format above.
2. Number the file sequentially (e.g., `0002-new-feature.md`).
3. Commit the ADR alongside the implementation or as a standalone PR.
4. Update this README with the new ADR entry.

## References

- [ADR GitHub](https://adr.github.io/)
- [How to write ADRs](https://github.com/joelparkerhenderson/architecture_documentation/blob/main/guides/adr/adr.md)