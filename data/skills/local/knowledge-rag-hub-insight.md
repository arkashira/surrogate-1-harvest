---
name: knowledge-rag-hub-insight
description: Workflow for Knowledge‑RAG that queries graph hubs, logs the top hub as a pattern, and safely records any script failures (e.g., granite‑tagger) with lessons for future runs.
version: 1.0.0
author: Ashira
platforms: [linux, macos]
metadata:
  hermes:
    tags: [knowledge, rag, graph, hub, failure]
---

## Purpose
Automate pre‑task context gathering by retrieving the most‑connected hub from FalkorDB, logging it as a reusable pattern, and capturing any failure of auxiliary scripts (e.g., `granite-tagger.sh`).

## Prerequisites
- Bash shell
- Access to `~/.claude/bin/graph-query.sh`
- Permissions to modify `~/.claude/memory/knowledge_index.md` and `~/.claude/memory/lessons_learned.md`

## Steps
1. **Get top hub**
   ```bash
   TOP=$(~/.claude/bin/graph-query.sh hubs | head -1 | awk -F"'" '{print $2}')
   ```
2. **Record pattern (if not already present)**
   ```bash
   grep -q "top-hub doc insight" ~/.claude/memory/knowledge_index.md || \
   echo "- Pattern: top-hub doc insight | Fix: Review the most‑connected hub (e.g., \"$TOP\") before planning tasks; Tags: #knowledge-rag #graph #hub" >> ~/.claude/memory/knowledge_index.md
   ```
3. **Run optional helper script**
   ```bash
   if ! bash ~/.claude/bin/granite-tagger.sh; then
       # Log failure lesson
       cat >> ~/.claude/memory/lessons_learned.md <<EOF
## $(date +%Y-%m-%d): Granite-tagger failure
- Context: granite-tagger.sh exited with non‑zero status.
- Insight: Potential missing dependencies, permission issues, or env vars.
- Fix/Pattern: Verify script logs, permissions, env vars; add fallback to hub query.
- Prevention: Add success check and log error before proceeding.
- Tags: #knowledge-rag #graph #failure
EOF
   fi
   ```
4. **Sync graph**
   ```bash
   ~/.claude/bin/graph-sync.sh
   ```

## Notes
- Use safe file‑modification tools (`patch`, `write_file`) instead of direct redirection when automating.
- Ensure duplicate pattern entries are avoided via `grep` check.
- This skill can be invoked at the start of any non‑trivial task needing broad context.

## Tags
#knowledge-rag #graph #hub #failure
