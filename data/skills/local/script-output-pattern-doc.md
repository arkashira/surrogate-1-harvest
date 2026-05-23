---
name: script-output-pattern-doc
description: Document reusable output patterns from automation scripts (e.g., granite‑business‑research.sh). Adds entries to knowledge_index.md, logs lessons in lessons_learned.md, and syncs the FalkorDB graph.
version: 1.0.0
author: Ashira
---

## Purpose
When a new automation script is created or an existing one is run, capture its output format as a reusable pattern. This ensures future developers can quickly understand and reuse the script’s results.

## Steps
1. **Run the target script**
   ```bash
   ~/path/to/script.sh   # capture any generated file paths if needed
   ```
   Record the output location (e.g., `decisions/20260427_1722_Project_bd-research.md`).

2. **Add a pattern entry to `knowledge_index.md`** using `patch` (avoids security‑scanner block on direct redirection):
   ```bash
   cat <<'PATCH' > /tmp/pattern.patch
   --- a/~/.claude/memory/knowledge_index.md
   +++ b/~/.claude/memory/knowledge_index.md
   @@
   +- Pattern: <script‑name> output format |
   +  Fix: <brief description of the structured output, key sections, and any scoring logic>;
   +  Tags: #<tag1> #<tag2> #knowledge‑rag #self‑improvement
   PATCH
   patch < /tmp/pattern.patch
   rm /tmp/pattern.patch
   ```
   Replace `<script‑name>` and description with concrete values.

3. **Log the lesson in `lessons_learned.md`** (again via `patch`):
   ```bash
   cat <<'PATCH' > /tmp/lesson.patch
   --- a/~/.claude/memory/lessons_learned.md
   +++ b/~/.claude/memory/lessons_learned.md
   @@
   +## $(date +%Y-%m-%d): <Script‑Name> pattern added
   +- Context: Added pattern entry for <script‑name> output format.
   +- Insight: Documenting output patterns early surfaces reuse opportunities and prevents duplication.
   +- Fix/Pattern: Use the standard "Pattern: … | Fix: … ; Tags:" line in `knowledge_index.md`.
   +- Prevention: Log pattern immediately after creating or modifying a script.
   +- Tags: #<tag1> #<tag2> #knowledge‑rag #self‑improvement
   PATCH
   patch < /tmp/lesson.patch
   rm /tmp/lesson.patch
   ```

4. **Sync the knowledge graph**
   ```bash
   ~/.claude/bin/graph-sync.sh
   ```
   This makes the new pattern discoverable via `graph-query.sh`.

5. **(Optional) Record the top hub** – after syncing, you may capture the current hub for reference:
   ```bash
   TOP=$(~/.claude/bin/graph-query.sh hubs | head -1 | awk -F"'" '{print $2}')
   echo "Top hub: $TOP"
   ```
   Include the hub name in the lesson if it adds valuable context.

## Pitfalls & Checks
- **Security policy**: Never use `>>` or `>` on dotfiles; always employ `patch`.
- **Diff context**: Ensure the `@@` header matches the surrounding lines; when appending at EOF, use `@@
+new line` without a removal line.
- **Verification**:
  ```bash
  grep -F "Pattern: <script‑name>" ~/.claude/memory/knowledge_index.md
  grep -F "$(date +%Y-%m-%d): <Script‑Name> pattern added" ~/.claude/memory/lessons_learned.md
  ```
- **Tag hygiene**: Keep tags concise and relevant; they enable fast graph queries.

## Example
For `granite‑business‑research.sh`:
```bash
- Pattern: granite business research script output format |
  Fix: Use granite‑business‑research.sh to generate structured market analysis with signals, competitor landscape, positioning, feature suggestions, and priority scoring; Tags: #granite #business‑research #automation #knowledge‑rag #self‑improvement
```
Corresponding lesson entry:
```markdown
## 2026-04-27: Granite business research pattern added
- Context: Added pattern entry for granite‑business‑research.sh output format.
- Insight: Documented reusable output format pattern for future reference.
- Fix/Pattern: Use consistent pattern entry format when adding new script documentation.
- Prevention: Log pattern immediately after creating new script outputs to keep the knowledge base up‑to‑date.
- Tags: #granite #business‑research #automation #knowledge‑rag #self‑improvement
```

## References
- Existing `dotfile-safe-modification` skill for safe patch usage.
- `knowledge-rag` workflow for graph queries.
- `self-improvement` pipeline for logging lessons.

---
