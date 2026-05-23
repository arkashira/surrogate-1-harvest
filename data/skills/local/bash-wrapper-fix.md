---
name: bash-wrapper-fix
description: Fix common errors in Bash wrapper scripts used for cron or symlinked execution, such as missing shebang, Windows line endings, improper exec quoting, and missing executable flag.
version: 1.0.0
author: Ashira
---

## Purpose

This skill also includes a concrete example for fixing the `opus-pr-reviewer.sh` wrapper script.
This skill automates the remediation of wrapper scripts that fail with Python `SyntaxError` or other interpreter issues when invoked via cron or other automation.

## Steps
1. **Verify shebang**
   ```bash
   head -1 "$SCRIPT" | grep -qE '^#!/.*bash' || echo "#!/usr/bin/env bash" | cat - "$SCRIPT" > tmp && mv tmp "$SCRIPT"
   ```
2. **Convert line endings to Unix LF**
   ```bash
   if command -v dos2unix >/dev/null; then dos2unix "$SCRIPT"; else sed -i 's/\r$//' "$SCRIPT"; fi
   ```
3. **Ensure executable permission**
   ```bash
   chmod +x "$SCRIPT"
   ```
4. **Correct exec line** (remove surrounding quotes if present)
   ```bash
   sed -i -E 's|exec "?([^ "]+)"? "\$@"|exec \1 "$@"|' "$SCRIPT"
   ```
5. **Set cron SHELL if needed**
   Add `SHELL=/bin/bash` at the top of the crontab or ensure the cron entry invokes the script via `bash /path/to/script.sh`.

## Prevention
- Add a lesson entry to `~/.claude/memory/lessons_learned.md` documenting context, insight, fix/pattern, prevention, and tags.
- Use the `patch` tool (or `write_file`) to safely append the markdown block.
- Lint wrapper scripts with `bash-script-validator` before committing.
- Add a pre‑run check in CI: `head -1 script.sh | grep -q '^#!.*bash'`.
- Store scripts in version control with Unix line endings.

### Example: Dynadebate verifier script
... (existing content unchanged) ...

### Example: Axentx backup sync script

### Example: Magma graph enrichment script
The `magma-graph-enrich.sh` wrapper failed with a Python `SyntaxError` because the script was invoked without a proper Bash shebang and the `exec` line was incorrectly quoted.
1. Verify the shebang is present:
   ```bash
   head -1 ~/.hermes/scripts/magma-graph-enrich.sh | grep -qE '^#!/.*bash'
   ```
2. Convert to Unix LF line endings and make executable:
   ```bash
   dos2unix ~/.hermes/scripts/magma-graph-enrich.sh
   chmod +x ~/.hermes/scripts/magma-graph-enrich.sh
   ```
3. Fix the `exec` line to remove extra quoting:
   ```bash
   sed -i -E 's|exec "?([^ "]+)"? "\$@"|exec \1 "$@"|' ~/.hermes/scripts/magma-graph-enrich.sh
   ```
4. Ensure cron invokes the script via Bash or set `SHELL=/bin/bash`.
5. Add a pre‑run validation step in the cron wrapper:
   ```bash
   if ! head -1 ~/.hermes/scripts/magma-graph-enrich.sh | grep -qE '^#!/.*bash'; then
       echo "Missing Bash shebang" >&2; exit 1;
   fi
   ```
These steps resolve the interpreter error and ensure reliable execution.
- Tags: #bash #script-error #magma-graph #cron
The `axentx-backup-sync.sh` wrapper failed with a Python `SyntaxError` because the `exec` line was incorrectly quoted and the script lacked a proper Bash shebang.
1. Verify the shebang is present:
   ```bash
   head -1 ~/.hermes/scripts/axentx-backup-sync.sh | grep -qE '^#!/.*bash'
   ```
2. Convert to Unix LF line endings and make executable:
   ```bash
   dos2unix ~/.hermes/scripts/axentx-backup-sync.sh
   chmod +x ~/.hermes/scripts/axentx-backup-sync.sh
   ```
3. Fix the `exec` line to remove extra quoting:
   ```bash
   sed -i -E 's|exec "?([^ \"']+)"? "\$@"|exec \1 "$@"|' ~/.hermes/scripts/axentx-backup-sync.sh
   ```
4. Ensure cron invokes the script via Bash or set `SHELL=/bin/bash`.
5. Add a pre‑run validation step in the cron wrapper:
   ```bash
   if ! head -1 ~/.hermes/scripts/axentx-backup-sync.sh | grep -qE '^#!/.*bash'; then
       echo "Missing Bash shebang" >&2; exit 1;
   fi
   ```
These steps resolve the exec quoting error and prevent Python interpretation.
- Tags: #bash #script-error #axentx #cron
The `dynadebate-verifier.sh` wrapper failed with a Python `SyntaxError` because it was invoked with the wrong interpreter.
1. Verify the shebang is present:
   ```bash
   head -1 ~/.hermes/scripts/dynadebate-verifier.sh | grep -qE '^#!/.*bash'
   ```
2. Convert line endings if needed and make executable:
   ```bash
   dos2unix ~/.hermes/scripts/dynadebate-verifier.sh
   chmod +x ~/.hermes/scripts/dynadebate-verifier.sh
   ```
3. Ensure cron invokes it via Bash or set `SHELL=/bin/bash` in the crontab.
4. Add a pre‑run validation step in the cron wrapper:
   ```bash
   if ! head -1 ~/.hermes/scripts/dynadebate-verifier.sh | grep -qE '^#!/.*bash'; then
       echo "Missing Bash shebang" >&2; exit 1;
   fi
   ```
This resolves the exec quoting error and prevents Python interpretation.
- Tags: #bash #script-error #dynadebate #cron

### Example: Tournament synthesis script
The `tournament-synthesis.sh` wrapper failed with a Python `SyntaxError` because the `exec` line was quoted and the script lacked a reliable Bash shebang in the cron environment.
1. Verify the shebang is present and correct:
   ```bash
   head -1 ~/.hermes/scripts/tournament-synthesis.sh | grep -qE '^#!/.*bash'
   ```
2. Ensure Unix LF line endings:
   ```bash
   dos2unix ~/.hermes/scripts/tournament-synthesis.sh
   ```
3. Fix the `exec` line to avoid quoting the path:
   ```bash
   sed -i -E 's|exec "?([^ "]+)"? "\$@"|exec \1 "$@"|' ~/.hermes/scripts/tournament-synthesis.sh
   ```
4. Make the script executable and set `SHELL=/bin/bash` in the crontab or invoke via `bash` explicitly.
5. Add a pre‑run validation step in the cron wrapper:
   ```bash
   if ! head -1 ~/.hermes/scripts/tournament-synthesis.sh | grep -qE '^#!/.*bash'; then echo "Missing Bash shebang" >&2; exit 1; fi
   ```
These steps prevent the script from being interpreted as Python and ensure reliable execution.
- Tags: #bash #script-error #tournament-synthesis #cron
- Lint wrapper scripts with `bash-script-validator` before committing.
- Add a pre‑run check in CI: `head -1 script.sh | grep -q '^#!.*bash'`.
- Store scripts in version control with Unix line endings.

### Example: Active‑learning‑from‑rejects script
The `active-learning-from-rejects.sh` wrapper failed with a Python `SyntaxError` because the script was invoked without a proper Bash shebang and the `exec` line was incorrectly quoted.
1. Verify the shebang is present:
   ```bash
   head -1 ~/.hermes/scripts/active-learning-from-rejects.sh | grep -qE '^#!/.*bash'
   ```
2. Convert to Unix LF line endings and make executable:
   ```bash
   dos2unix ~/.hermes/scripts/active-learning-from-rejects.sh
   chmod +x ~/.hermes/scripts/active-learning-from-rejects.sh
   ```
3. Fix the `exec` line to remove extra quoting:
   ```bash
   sed -i -E 's|exec "?([^ \"']+)"? "\$@"|exec \1 "$@"|' ~/.hermes/scripts/active-learning-from-rejects.sh
   ```
4. Ensure cron invokes the script via Bash or set `SHELL=/bin/bash`.
5. Add a pre‑run validation step in the cron wrapper:
   ```bash
   if ! head -1 ~/.hermes/scripts/active-learning-from-rejects.sh | grep -qE '^#!/.*bash'; then
       echo "Missing Bash shebang" >&2; exit 1;
   fi
   ```
These steps resolve the interpreter error and ensure reliable execution.
- Tags: #bash #script-error #active-learning #cron

### Example: Budget Auto-Throttle script
The `budget-auto-throttle.sh` wrapper failed with a Python `SyntaxError` because the `exec` line was quoted:
```bash
exec "/Users/Ashira/.claude/bin/budget-auto-throttle.sh" "$@"
```
Fix:
1. Verify shebang is present:
   ```bash
   head -1 ~/.hermes/scripts/budget-auto-throttle.sh | grep -qE '^#!/.*bash'
   ```
2. Convert line endings and make executable:
   ```bash
   dos2unix ~/.hermes/scripts/budget-auto-throttle.sh
   chmod +x ~/.hermes/scripts/budget-auto-throttle.sh
   ```
3. Remove surrounding quotes from exec line:
   ```bash
   sed -i -E 's|exec "?([^ "]+)"? "\$@"|exec \1 "$@"|' ~/.hermes/scripts/budget-auto-throttle.sh
   ```
4. Ensure cron invokes via Bash or set `SHELL=/bin/bash`.

These steps resolve the SyntaxError and ensure reliable execution.
- Tags: #bash #script-error #budget-auto-throttle #cron

## Tags
#bash #script-error #wrapper #cron

## Duplicate Lesson Entry Prevention
- Before appending a new entry to `lessons_learned.md`, check if an entry with the same title/date already exists to avoid duplicates.
- Example check (bash):
  ```bash
  if grep -q "## $(date +%Y-%m-%d): AxentX backup sync wrapper" ~/.claude/memory/lessons_learned.md; then
      echo "Entry already exists, skipping append"
  else
      # Append entry
  fi
  ```
- Automate this check in any script that logs lessons to keep the log concise.

