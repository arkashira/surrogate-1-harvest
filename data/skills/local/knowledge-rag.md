---
name: knowledge-rag
description: Query Ashira's FalkorDB knowledge graph before starting complex tasks. Surfaces relevant patterns, past lessons, and domain knowledge from 248 docs across devops, mlops, github, mcp, data-science, and more.
version: 1.0.0
author: Ashira
platforms: [macos, linux]
metadata:
  hermes:
    tags: [knowledge, rag, falkordb, graph, memory, learning]
prerequisites:
  commands: [bash]
---

## Knowledge RAG — FalkonDB Graph Query

### Fallback handling
If `granite-tagger.sh` fails (non‑zero exit status), continue with the standard hub query (`graph-query.sh hubs`) and pattern logging. Record the failure in the lesson log for debugging.

Before planning any non-trivial task, query the knowledge graph to surface relevant context.
The graph lives at `~/.claude/graph-db.rdb` (FalkorDB, 248 docs, 169 tags).

## When to Use

- **Before decomposing a complex task** — check if a known pattern or past fix exists
- **Before architectural decisions** — find related docs in the graph
- **When you need domain knowledge** — query by tag for the relevant domain
- Skip only for trivial single-step tasks (typos, simple lookups)

## Commands

```bash
# Get graph overview (always run first if unfamiliar with the state)
~/.claude/bin/graph-query.sh stats

# Find docs by tag (use the domain closest to your task)
~/.claude/bin/graph-query.sh tag devops
~/.claude/bin/graph-query.sh tag mlops
~/.claude/bin/graph-query.sh tag github
~/.claude/bin/graph-query.sh tag mcp
~/.claude/bin/graph-query.sh tag data-science
~/.claude/bin/graph-query.sh tag inference-sh

# Find related docs (2-hop traversal from a known doc)
~/.claude/bin/graph-query.sh related "<doc-name>"

# Most-connected hub docs (best entry points for broad context)
~/.claude/bin/graph-query.sh hubs

# Shortest path between two concepts
~/.claude/bin/graph-query.sh path "<doc-a>" "<doc-b>"
```

## Available Tags

apple, autonomous-ai-agents, creative, data-science, devops, diagramming, dogfood,
domain, email, feeds, gaming, gifs, github, inference-sh, mcp, media, mlops,
note-taking, productivity, red-teaming, webhook-subscriptions

## Integration with Learning Loop

## Automated Insight Capture
After querying hubs, record the most‑connected hub document as a reusable pattern.
```bash
# Identify top hub (already shown by `graph-query.sh hubs`)
TOP=$(~/.claude/bin/graph-query.sh hubs | head -1 | awk -F"'" '{print $2}')
# Append a pattern entry (example for MOC)
# Safely append pattern using the patch tool (avoids direct redirection)
# Example:
#   patch --mode replace --path ~/.claude/memory/knowledge_index.md --old_string "" --new_string "- Pattern: top‑hub doc insight | Fix: Review the most‑connected hub (e.g., \"$TOP\") before planning tasks; Tags: #knowledge-rag #graph #hub" --replace_all
- Pattern: top‑hub doc insight | Fix: Review the most‑connected hub (e.g., "$TOP") before planning tasks; Tags: #knowledge-rag #graph #hub
EOF
# Log the lesson
cat >> ~/.claude/memory/lessons_learned.md << EOF
## $(date +%Y-%m-%d): Top hub doc insight
- Context: Ran `knowledge-rag` workflow, identified "$TOP" as the central hub.
- Insight: Reviewing this hub early surfaces broad context and prevents missing key knowledge.
- Fix/Pattern: Query hubs and note the top hub.
- Prevention: Make hub review a mandatory pre‑planning step.
- Tags: #knowledge-rag #graph #knowledge-management
EOF
~/.claude/bin/graph-sync.sh
```
This ensures the insight is captured automatically for future sessions.

After completing a task, check if the result produced a new pattern worth saving:
- New fix or workaround → save to `~/.claude/memory/lessons_learned.md`
- New domain knowledge → save to `~/Documents/Obsidian Vault/AI-Hub/knowledge/`
- Re-index: `~/.claude/bin/graph-sync.sh` (adds it to FalkorDB for future queries)
