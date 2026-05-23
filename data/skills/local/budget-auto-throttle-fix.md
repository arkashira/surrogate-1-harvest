---
name: budget-auto-throttle-fix
description: Resolve and prevent Bash wrapper script execution errors for the budget-auto-throttle data-collection script.
version: 1.0.0
author: Ashira
---

## Purpose
Automate the remediation of the `budget-auto-throttle.sh` wrapper script that was mistakenly executed by the Python interpreter, causing a `SyntaxError`. Ensures reliable cron execution and logs the fix for future reference.

## Steps
1. **Inspect the script**
   ```bash
   SCRIPT=~/.hermes/scripts/budget-auto-throttle.sh
   head -1 "$SCRIPT"
   ```
   Verify the first line is a Bash shebang (`#!/usr/bin/env bash`).

2. **Add missing shebang (if needed)**
   ```bash
   if ! head -1 "$SCRIPT" | grep -qE '^#!/.*bash'; then
       echo "#!/usr/bin/env bash" | cat - "$SCRIPT" > tmp && mv tmp "$SCRIPT"
   fi
   ```

3. **Convert to Unix line endings**
   ```bash
   if command -v dos2unix >/dev/null; then
       dos2unix "$SCRIPT"
   else
       sed -i 's/\r$//' "$SCRIPT"
   fi
   ```

4. **Make executable**
   ```bash
   chmod +x "$SCRIPT"
   ```

5. **Correct the `exec` quoting** (remove surrounding quotes)
   ```bash
   sed -i -E 's|exec "?([^ "]+)"? "\$@"|exec \1 "$@"|' "$SCRIPT"
   ```

6. **Ensure cron uses Bash**
   - Add `SHELL=/bin/bash` at the top of the crontab, **or**
   - Invoke the script explicitly with Bash in the cron entry:
     ```cron
     0 * * * * bash /Users/Ashira/.hermes/scripts/budget-auto-throttle.sh
     ```

7. **Log the lesson** (self‑improvement)
   ```bash
   cat >> ~/.claude/memory/lessons_learned.md <<'EOF'
   ## $(date +%Y-%m-%d): Budget auto-throttle script execution error
   - Context: Data‑collection script ~/.hermes/scripts/budget-auto-throttle.sh failed with a Python SyntaxError when run via cron.
   - Insight: The script was interpreted by Python due to missing Bash shebang / wrong interpreter in cron.
   - Fix/Pattern: Ensure the script runs with Bash (shebang, LF endings, executable, correct exec line) and set `SHELL=/bin/bash` in cron.
   - Prevention: Lint wrapper scripts for shebang and line endings, add a pre‑run validation step, and test scripts manually before adding to cron.
   - Tags: #bash #script-error #budget-auto-throttle #cron
   EOF
   ```

8. **Append a reusable pattern to the knowledge index**
   ```bash
   echo "- Pattern: budget-auto-throttle script error | Fix: Ensure Bash shebang, LF endings, executable, and invoke via Bash; Tags: #bash #script-error #budget-auto-throttle" >> ~/.claude/memory/knowledge_index.md
   ```

9. **Sync to the graph DB**
   ```bash
   ~/.claude/bin/graph-sync.sh
   ```

## Prevention Checklist
- All wrapper scripts must start with a Bash shebang.
- Store scripts with Unix LF line endings.
- Verify executable permission (`chmod +x`).
- Use explicit `bash` in cron entries or set `SHELL=/bin/bash`.
- Include a lint step (`bash-script-validator`) in CI pipelines.
- Log each fix via the self‑improvement pipeline.

## Tags
#bash #script-error #budget-auto-throttle #cron
