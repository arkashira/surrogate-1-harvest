---
name: cron-script-execution-safety
title: Ensure reliable execution of wrapper scripts in cron
description: Prevent "script not found" or interpreter errors for cron jobs by enforcing proper permissions, shebang, and environment settings.
tags:
  - "#bash"
  - "#cron"
  - "#script-error"
  - "#devops"
---

## Steps
1. Verify the script file exists at an absolute path.
2. Ensure the file has executable permission:
   ```bash
   chmod +x /full/path/to/script.sh
   ```
3. Ensure the first line is a proper Bash shebang (`#!/usr/bin/env bash`) with no leading whitespace.
4. Convert line endings to Unix LF if needed:
   ```bash
   dos2unix /full/path/to/script.sh
   ```
5. In the crontab entry, set `SHELL=/bin/bash`.
6. Use the absolute path to the script; optionally invoke via `/usr/bin/bash`.
7. Add a pre‑run validation check in the script or wrapper:
   ```bash
   SCRIPT="/full/path/to/script.sh"
   if [[ ! -x "$SCRIPT" ]]; then
     echo "Missing or non‑executable script: $SCRIPT" >&2
     exit 1
   fi
   ```

## Pitfalls
- Missing shebang causes the OS to fallback to the default interpreter (often `sh` or `python`), leading to syntax errors.
- Cron may use `/bin/sh` which lacks Bash‑specific features.
- Relative paths can resolve incorrectly when cron runs with a different working directory.
- **Hermes security rule**: Scripts executed by cron must reside within `~/.hermes/scripts`. If a wrapper `exec`s a script located outside this directory (e.g., `~/.claude/bin/...`), the job is blocked with a “Blocked: script path resolves outside the scripts directory” error. Move the target script into the allowed directory or adjust the wrapper to stay inside the sandbox.
- Missing shebang causes the OS to fallback to the default interpreter (often `sh` or `python`), leading to syntax errors.
- Cron may use `/bin/sh` which lacks Bash‑specific features.
- Relative paths can resolve incorrectly when cron runs with a different working directory.

## Verification
- Run the script manually to confirm successful execution.
- Review cron logs for a successful run entry.
- Use `grep` on the log to ensure no "Script not found" errors.
