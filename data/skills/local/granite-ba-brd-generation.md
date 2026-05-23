---
name: granite-ba-brd-generation
description: Automate Business Requirements Document (BRD) creation from raw backlog items using the granite-ba script.
version: 1.0.0
author: Ashira
platforms: [macos, linux]
metadata:
  hermes:
    tags: [automation, brd, backlog, documentation]
---

# Overview
The `granite-ba.sh` script converts the most recent raw backlog entry into a fully‑structured BRD in Markdown.
It is useful for quickly documenting new features, ensuring consistent format, and keeping backlog documentation up‑to‑date.

# Prerequisites
- Bash installed (`bash` command).
- Backlog JSONL file at `~/.hermes/workspace/swarm-shared/backlog.jsonl` with entries that include:
  - `status: "raw"`
  - `project` field (non‑`meta`)
  - `item` field describing the feature.
- Write permission to `~/.hermes/workspace/swarm-shared/backlog/`.

# Usage
```bash
~/.claude/bin/granite-ba.sh
```
The script will:
1. Load the backlog JSONL.
2. Select the most recent raw item.
3. Build a prompt for the `granite-bridge.sh` LLM.
4. Write the BRD to a file named `<date>_<project>_brd.md`.
5. Log the operation to `~/.claude/logs/granite-ba.log`.

# Common Patterns & Fixes
- **No raw items** – The script exits silently with log entry `no raw items to spec`. Ensure a raw backlog entry exists before running.
- **Missing permissions** – Verify the script has execute permission (`chmod +x ~/.claude/bin/granite-ba.sh`).
- **Custom scheduling** – To run automatically, add a cron entry:
  ```cron
  0 * * * * ~/.claude/bin/granite-ba.sh >> ~/.claude/logs/granite-ba.cron.log 2>&1
  ```

# Integration with Knowledge RAG & Self‑Improvement
After each run, add a lesson entry to `~/.claude/memory/lessons_learned.md` and a pattern to `knowledge_index.md` (see self‑improvement skill). This makes the approach discoverable for future tasks.

# Example Lesson Entry (auto‑generated)
```markdown
## 2026-04-27: Granite BA script BRD generation
- Context: Ran ~/.claude/bin/granite-ba.sh to generate a BRD for a raw backlog item in project "Vanguard".
- Insight: The script reliably produces structured BRDs with executive summary, stakeholder list, user stories, requirements, acceptance criteria, etc.
- Fix/Pattern: Use granite-ba script to automate BRD creation; ensure raw items have status "raw" and include a project name.
- Prevention: Verify raw backlog items exist before running; consider scheduling via cron.
- Tags: #granite #brd #automation #knowledge-rag
```

# References
- `~/.claude/bin/granite-ba.sh` – main script.
- `~/.claude/bin/granite-bridge.sh` – LLM bridge used by the script.
- Knowledge‑RAG skill – query hub docs before planning new features.
- Self‑Improvement skill – log lessons and sync graph.
