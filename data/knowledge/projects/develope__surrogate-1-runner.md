---
name: surrogate-1-runner
path: /Users/Ashira/develope/surrogate-1-runner
tags: ["project", "codebase", "python"]
last_indexed: 2026-05-01
type: project
---

# surrogate-1-runner

**Path**: `/Users/Ashira/develope/surrogate-1-runner`
**Group**: root
**Languages**: Python
**Frameworks**: none detected
**LOC**: ~0
**Deps**: 0

## README
Parallel public-dataset ingest workers for the [axentx/surrogate-1-training-pairs](https://huggingface.co/datasets/axentx/surrogate-1-training-pairs) HuggingFace dataset. Every 30 minutes (or on `workflow_dispatch`), GitHub Actions launches **16 parallel runners**. Each runner takes a deterministic 1/16 slice (`slug-hash bucket = SHARD_ID`) of the public dataset list defined in `bin/dataset-enrich.sh`, streams,

## Git
- Branch: `main`
- Last commit: 2026-04-29 02:42:25 +0700 stamp-and-move: query central cursor before each dataset stream
- Commits (last 30d): 13

## Key dependencies
(none)

## Scripts
(none)

## Structure
```
📄 README.md
📄 requirements.txt
```

## Related
- [[../../patterns/MOC|Knowledge Graph Hub]]
- [[../workspace-map|Workspace Map]]
