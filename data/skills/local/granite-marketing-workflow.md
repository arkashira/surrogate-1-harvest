---
name: granite-marketing-workflow
description: Workflow for generating marketing positioning materials using `granite-marketing.sh`, including pre‑planning knowledge‑graph hub review, safe script patching, model fallback, and self‑improvement logging.
version: 1.0.0
author: Ashira
---

## Overview
This skill captures the end‑to‑end process used to generate a marketing brief for a project (e.g., Costinel) with the `granite-marketing.sh` script while ensuring knowledge continuity and robust handling of missing LLM models.

## Steps
1. **Knowledge‑RAG pre‑planning**
   - Run `~/.claude/bin/graph-query.sh hubs`.
   - Record the top hub (usually `MOC`) in `knowledge_index.md` as a pattern:
     ```markdown
     - Pattern: review top‑hub doc before planning tasks | Fix: Query hubs, note top hub | Tags: #knowledge-rag #graph
     ```
   - Log the insight to `lessons_learned.md`.
2. **Safe script modification**
   - If the target script is hidden or under version control, use the `patch` tool (replace mode) instead of direct redirection.
   - Example to change the model flag:
     ```bash
     patch 
       --mode replace \
       --path ~/.claude/bin/granite-marketing.sh \
       --old_string "--model granite4" \
       --new_string "--model qwen-coder"
     ```
   - Verify the script is still executable.
3. **Run the marketing script**
   - Execute `bash ~/.claude/bin/granite-marketing.sh`.
   - The script writes a markdown file to `~/.hermes/workspace/swarm-shared/decisions/` named `<timestamp>_<project>_marketing.md`.
4. **Validate output**
   - Open the generated file and confirm sections: elevator pitch, ICP, value propositions, positioning, hero copy, objection handlers.
5. **Self‑Improvement logging**
   - Append a lesson entry to `~/.claude/memory/lessons_learned.md` describing the fallback model decision and the hub‑review pattern.
   - Add a pattern entry to `~/.claude/memory/knowledge_index.md` if not already present.
   - Run `~/.claude/bin/graph-sync.sh` to index new knowledge.

## Pitfalls & Mitigations
- **Model unavailable** – If the desired Granite model cannot be pulled (timeout or missing), switch to an available model (e.g., `qwen-coder`) before running the script.
- **Empty script files** – Verify script contents with `head` or `cat` after any patch; if empty, restore from backup or re‑clone repository.
- **Permission issues** – Ensure the script is executable (`chmod +x`).
- **Knowledge‑graph out‑of‑sync** – Run `graph-sync.sh` after updating knowledge files.

## Verification
- After execution, `ls -l ~/.hermes/workspace/swarm-shared/decisions/` should list a new `.md` file.
- `grep "Pattern:" ~/.claude/memory/knowledge_index.md` should include the hub‑review pattern.
- `cat ~/.claude/memory/lessons_learned.md | tail -n 5` should show the latest entry.

## References
- `knowledge-rag` skill for graph queries.
- `self-improvement` skill for logging lessons.
- `patch` tool usage guidelines.
