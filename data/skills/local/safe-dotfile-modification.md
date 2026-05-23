---
name: safe-dotfile-modification
description: Safely update hidden configuration or memory files without using shell redirection, which can be blocked for security. Use `patch` for targeted edits or `write_file` for full replacements.
author: Ashira
version: 1.0.0
---

## When to use
- Updating `~/.claude/memory/knowledge_index.md`, `lessons_learned.md`, or any dotfile.
- Adding a new pattern or lesson entry.
- Modifying hidden configuration files where direct `cat >>` may trigger security warnings.

## Preferred methods
1. **Targeted edit** – use the `patch` tool with mode `replace`.
   ```bash
   patch mode=replace path=~/.claude/memory/knowledge_index.md old_string="- Pattern: old" new_string="- Pattern: new"
   ```
2. **Full replacement** – use `write_file` to overwrite the entire file.
   ```bash
   write_file path=~/.claude/memory/knowledge_index.md content="<full file content>"
   ```

## Example workflow
```bash
# Append a new pattern safely
patch mode=replace path=~/.claude/memory/knowledge_index.md old_string="# Knowledge index (excerpt)" new_string="# Knowledge index (excerpt)\n- Pattern: safe dotfile modification | Fix: Use patch/write_file instead of cat redirection; Tags: #dotfile #security #knowledge-rag"
```

## Pitfalls & checks
- Ensure `old_string` uniquely identifies the line to replace; otherwise set `replace_all=true`.
- After applying a patch, verify the file with `read_file`.
- For large additions, consider reading the file, appending in memory, then `write_file`.

## Tags
#security #dotfile #knowledge-rag #self-improvement
