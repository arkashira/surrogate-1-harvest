---
name: post-mortem-script-not-found
description: Diagnose and remediate missing or misconfigured role scripts for ceremony agents, log lessons, and update pattern index.
version: 1.0.0
author: Ashira
---

# Overview
When a cron job or manual invocation of `agent-runner.sh <role>` fails with *"Script not found"* it usually means the role script is missing, incorrectly named, or not executable. This skill provides a reproducible workflow to resolve the issue, capture the learning, and prevent recurrence.

## Steps
1. **Identify the failing role**
   - Review the log (`~/.claude/logs/agent-<role>.log`) for the exact error message.
2. **Verify script existence**
   - Check the expected script path based on the role configuration (`~/.hermes/config/ceremony-agents.json`).
   - Example: for role `post-mortem` the script should be `~/.hermes/scripts/post-mortem.sh` or the wrapper defined in the config.
   - If the file is absent, create a minimal placeholder script with a proper shebang:
     ```bash
     #!/usr/bin/env bash
     echo "[post-mortem] No implementation yet."
     ```
   - Ensure the script is executable: `chmod +x <script>`.
3. **Validate shebang & line endings**
   - Ensure the first line is `#!/usr/bin/env bash` (or `/bin/bash`).
   - Convert Windows CRLF to Unix LF if needed: `dos2unix <script>`.
4. **Add guard in `agent-runner.sh`** (optional but recommended)
   - Insert a pre‑run check before the `exec` line:
     ```bash
     SCRIPT_PATH="$HOME/.hermes/scripts/${ROLE}.sh"
     if [[ ! -x "$SCRIPT_PATH" ]]; then
         echo "[agent-runner] Missing or non‑executable script for role $ROLE" >&2
         exit 1
     fi
     exec "$SCRIPT_PATH" "$@"
     ```
5. **Log the incident**
   - Append a lesson to `~/.claude/memory/lessons_learned.md` using the standard template:
     ```markdown
     ## <date>: Agent‑runner post‑mortem script not found
     - Context: Cron job attempted to run post‑mortem ceremony via /Users/Ashira/.hermes/scripts/agent-runner.sh post‑mortem but script was missing or mis‑configured.
     - Insight: The wrapper expects a concrete script file; missing or non‑executable scripts cause a "Script not found" error.
     - Fix/Pattern: Verify role scripts exist, have correct shebang, proper line endings, and are executable. Add a pre‑run validation guard.
     - Prevention: Include the guard in `agent-runner.sh`; lint all wrapper scripts for shebang/permissions before adding to cron.
     - Tags: #bash #script-error #postmortem #cron
     ```
6. **Add pattern to knowledge index**
   - Append a concise pattern line to `~/.claude/memory/knowledge_index.md`:
     ```text
     - Pattern: post-mortem script not found | Fix: Verify script exists, proper shebang, executable; add guard in runner | Tags: #bash #script-error #postmortem
     ```
7. **Sync to graph DB**
   - Run `~/.claude/bin/graph-sync.sh` to make the new lesson searchable.

## Pitfalls & Tips
- *Missing shebang*: The OS may fall back to the default interpreter (often Python), yielding a SyntaxError.
- *CRLF line endings*: Convert with `dos2unix`; otherwise the shebang may be ignored.
- *Permissions*: Forgetting `chmod +x` results in "Permission denied" rather than "Script not found" – the guard helps surface this.
- *Guard placement*: Ensure the guard runs before any `exec` to avoid silent failures.

## Verification
After applying the fix, re‑run the original command:
```bash
~/hermes/scripts/agent-runner.sh post-mortem
```
The log should show a successful start and an output file in `~/.hermes/workspace/ceremonies/post-mortem/`.

## References
- `agent-runner.sh` (source code) – shows role extraction and execution flow.
- `graph-sync.sh` – synchronizes memory files to FalkorDB.
- Existing pattern entries in `knowledge_index.md` for similar script‑error fixes.
