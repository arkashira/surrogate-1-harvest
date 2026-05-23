---
name: hf-space-ingest
path: /Users/Ashira/develope/hf-space-ingest
tags: ["project", "codebase", "docker"]
last_indexed: 2026-05-01
type: project
---

# hf-space-ingest

**Path**: `/Users/Ashira/develope/hf-space-ingest`
**Group**: root
**Languages**: unknown
**Frameworks**: Docker
**LOC**: ~0
**Deps**: 0

## README
title: surrogate-1 sub-ingest emoji: 🐉 colorFrom: blue colorTo: indigo sdk: docker pinned: false Stripped-down ingest worker for the [axentx/surrogate-1](https://huggingface.co/spaces/axentx/surrogate-1) primary. Pulls public datasets, dedups, normalizes per-schema, uploads to one of the five sibling datasets: - axentx/surrogate-1-training-pairs (primary) - axentx/surrogate-1-pairs-A - axentx/surrogate-1-pairs-B

## Git
- Branch: `main`
- Last commit: 2026-04-28 23:57:48 +0700 feat: sub-Space ingest worker (stripped down)
- Commits (last 30d): 1

## Key dependencies
(none)

## Scripts
(none)

## Structure
```
📄 Dockerfile
📄 README.md
📄 start.sh
```

## Related
- [[../../patterns/MOC|Knowledge Graph Hub]]
- [[../workspace-map|Workspace Map]]
