---
name: self-improvement
description: Continuous self-training pipeline. Run after every non-trivial task to extract patterns, update knowledge graph, and grow smarter over time.
version: 1.0.0
author: Ashira
platforms: [macos, linux]
metadata:
  hermes:
    tags: [self-improvement, learning, knowledge, graph, memory, training]
---

# Self-Improvement — Continuous Learning Pipeline

Run this after every non-trivial task. Each session makes the next one smarter.

## Step 1: Extract the Lesson

Ask yourself:
- What was non-obvious about this task?
- What would have saved time if known earlier?
- What pattern is reusable across future tasks?
- What went wrong (if anything) and why?

## Step 2: Save to Lessons Log

```bash
cat >> ~/.claude/memory/lessons_learned.md << 'EOF'

## $(date +%Y-%m-%d): <title>
- Context: <what task / what project>
- Insight: <what was learned>
- Fix/Pattern: <concrete solution or approach>
- Prevention: <how to avoid the problem next time>
- Tags: <tag1 tag2 tag3>
EOF
```

## Step 3: Add to Pattern Index (if reusable)

```bash
# Append to knowledge_index.md under the right category
echo "- Pattern: <keywords> | Fix: <1-line summary> | Tags: #tag1 #tag2" \
  >> ~/.claude/memory/knowledge_index.md
```

## Step 4: Sync to Graph DB

```bash
~/.claude/bin/graph-sync.sh
```
This makes the new knowledge discoverable via FalkorDB for future sessions.

## Step 5: Update MEMORY.md (if environment changed)

If you discovered a new tool, account, service, or convention:
```bash
# Append to ~/.hermes/memories/MEMORY.md
# Format: ## Section \n - key: value
```

## When to Run This Full Pipeline

| Trigger | Action |
|---------|--------|
| After complex task (>10 tool calls) | Full Steps 1–5 |
| After fixing a non-obvious bug | Steps 1–3 + 4 |
| After new tool/service discovered | Steps 1, 5, 4 |
| After trivial task | Skip (no new knowledge) |

## Automated Pipeline (cron)

These run automatically — no manual action needed:
- **Daily 6 AM**: `~/.hermes/bin/harvest-hermes-sessions.sh` → harvests sessions → graph-sync
- **Sunday 3 AM**: `~/.claude/bin/distill-patterns.sh` → finds novel patterns → adds to graph
- **Quarterly**: `~/.claude/bin/knowledge-refresh.sh` → full domain refresh

The graph grows every day. Query it at the start of tasks:
```bash
~/.claude/bin/graph-query.sh tag <domain>   # find relevant docs
~/.claude/bin/graph-query.sh hubs           # find most-connected knowledge
```
