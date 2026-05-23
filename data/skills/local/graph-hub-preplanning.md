---
name: graph-hub-preplanning
description: Reusable workflow to query the FalkorDB knowledge graph for the most‑connected hub doc before planning complex tasks, record the insight as a pattern and lesson, and sync the graph.
version: 1.0.0
author: Hermes
tags: [knowledge-rag, graph, preplanning]
created_at: 2026-04-27T13:00:00Z
---

# Graph Hub Pre‑Planning Workflow

## Goal
Surface broad context early by identifying the top hub document in the FalkorDB knowledge graph, record the insight for future reuse, and keep the graph in sync.

## Steps
1. **Show graph statistics** (optional, for awareness):
   ```bash
   ~/.claude/bin/graph-query.sh stats
   ```
2. **Identify the most‑connected hub**:
   ```bash
   TOP=$(~/.claude/bin/graph-query.sh hubs | head -1 | awk -F"'" '{print $2}')
   echo "Top hub: $TOP"
   ```
3. **Append a reusable pattern** to `~/.claude/memory/knowledge_index.md`:
   ```bash
   cat >> ~/.claude/memory/knowledge_index.md <<EOF
- Pattern: top‑hub doc insight | Fix: Review the most‑connected hub (e.g., "$TOP") before planning tasks; Tags: #knowledge-rag #graph #hub
EOF
   ```
4. **Log a lesson** in `~/.claude/memory/lessons_learned.md`:
   ```bash
   cat >> ~/.claude/memory/lessons_learned.md <<'EOF'
## $(date +%Y-%m-%d): Graph hub doc insight
- Context: Ran knowledge‑rag skill, identified "$TOP" as the most‑connected hub document.
- Insight: Reviewing the top hub early surfaces broad context and prevents missing key knowledge.
- Fix/Pattern: Query hubs and note the top hub before planning tasks.
- Prevention: Include hub review as a mandatory pre‑planning step.
- Tags: #knowledge-rag #graph #hub
EOF
   ```
5. **Synchronize the graph** so the new entries are searchable:
   ```bash
   ~/.claude/bin/graph-sync.sh
   ```

## When to use
- Before decomposing a complex feature or architectural decision.
- When you suspect missing domain knowledge could affect planning.
- Any time a new project is started and you want a high‑level knowledge snapshot.

## Benefits
- Guarantees early exposure to the most relevant knowledge.
- Captures a reusable pattern for future sessions.
- Keeps the FalkorDB knowledge base up‑to‑date automatically.

## References
- `knowledge-rag` skill (pre‑existing).
- FalkorDB graph‑query scripts located in `~/.claude/bin/`.
---