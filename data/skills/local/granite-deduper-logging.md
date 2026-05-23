---
name: granite-deduper-logging
description: Procedure for running the granite deduper script, interpreting its output, and logging results to lessons_learned and knowledge_index, followed by graph synchronization.
author: Ashira
tags: [knowledge-rag, granite, dedup, self-improvement]
---

# Granite Deduper Logging Skill

## Overview
This skill describes a repeatable process for executing the `granite-deduper.sh` script, handling its output, and updating the knowledge base. It ensures that duplicate detection results are captured consistently, and the knowledge graph stays current.

## Steps
1. **Run the deduper script**
   ```bash
   ~/.claude/bin/granite-deduper.sh
   ```
   Capture the printed path to the duplicates file and the pair count.

2. **Interpret output**
   - If the pair count is **0**, no duplicates were found.
   - If the pair count is **greater than 0**, note the duplicate pairs file path.

3. **Log to lessons_learned.md**
   - Append a new section using the template:
   ```markdown
   ## <DATE>: Granite deduper <result>
   - Context: Ran granite-deduper script to detect duplicate review pairs.
   - Insight: <brief insight based on result>
   - Fix/Pattern: <if duplicates, list next steps; otherwise note "No action needed".>
   - Prevention: <recommend regular runs>
   - Tags: #knowledge-rag #granite #dedup #self-improvement
   ```
   - Use `patch` in replace mode to insert the entry after the most recent entry.

4. **Add pattern to knowledge_index.md**
   - For the *no duplicates* case, add:
   ```markdown
   - Pattern: granite deduper no duplicates found | Fix: No action needed; deduplication logic works as intended. Tags: #knowledge-rag #granite #dedup #self-improvement
   ```
   - For the *duplicates* case, add a pattern describing the remediation steps (e.g., review duplicate pairs, adjust dedup logic).
   - Use `patch` to insert the line at the end of the file.

5. **Synchronize the knowledge graph**
   ```bash
   ~/.claude/bin/graph-sync.sh
   ```
   This makes the new entries searchable via `graph-query.sh`.

## Pitfalls & Tips
- Ensure the deduper script has execute permission (`chmod +x`).
- Verify the output parsing correctly extracts the pair count; different script versions may format the line differently.
- When inserting into `lessons_learned.md`, keep a blank line before the new section for readability.
- After patching files, run `graph-sync.sh` to avoid stale graph data.

## Verification
- After completing the steps, `lessons_learned.md` should contain a new dated entry.
- `knowledge_index.md` should include the new pattern line.
- Running `graph-query.sh tag granite` should now list the new pattern as a searchable result.

## References
- `~/.claude/bin/granite-deduper.sh`
- `~/.claude/bin/graph-query.sh`
- `~/.claude/bin/graph-sync.sh`
