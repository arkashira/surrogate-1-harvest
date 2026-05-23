---
name: cron_script_wrapper_fix
title: Fix Cron Script Wrapper Execution Errors
description: Resolve cron job failures due to scripts residing outside ~/.hermes/scripts and incorrect interpreter usage.
author: Hermes Team Lead
tags: [cron, script-wrapper, bash, security-guard, devops]
---

## Purpose
Resolve failures where a cron job attempts to execute a wrapper script that:
1. Resides outside the allowed `~/.hermes/scripts` directory, triggering the security guard.
2. Uses an incorrect interpreter (e.g., Python) causing a `SyntaxError`.

This skill automates moving the script into the permitted location, ensuring correct Bash syntax, and making it executable.

## Steps
1. **Identify the failing wrapper**
   - Locate the wrapper script referenced in the cron definition (usually `~/.hermes/scripts/<name>.sh`).
   - Verify the error output for messages like "Blocked: script path resolves outside the scripts directory" or a Python `SyntaxError`.
2. **Copy the actual script into the allowed directory**
   ```bash
   mkdir -p ~/.hermes/scripts
   cp /path/to/original/script.sh ~/.hermes/scripts/<script-name>.sh
   ```
   Replace `/path/to/original/script.sh` with the real script location (often `~/.claude/bin/` or similar).
3. **Add a proper Bash shebang** (if missing)
   ```bash
   sed -i '1s|^|#!/usr/bin/env bash\n|' ~/.hermes/scripts/<script-name>.sh
   ```
4. **Set Bash as the cron shell**
   - Edit the crontab entry to include `SHELL=/bin/bash` at the top, ensuring Bash is used regardless of the system default.
   ```bash
   (crontab -l ; echo "SHELL=/bin/bash") | crontab -
   ```
5. **Add pre‑run existence and permission checks**
   ```bash
   SCRIPT=~/.hermes/scripts/<script-name>.sh
   if [[ ! -x "$SCRIPT" ]]; then
     echo "Missing or non‑executable script: $SCRIPT"
     exit 1
   fi
   "$SCRIPT" "$@"
   ```
6. **Make the script executable**
   ```bash
   chmod +x ~/.hermes/scripts/<script-name>.sh
   ```
7. **Validate Bash syntax**
   ```bash
   bash -n ~/.hermes/scripts/<script-name>.sh
   ```
   - Exit code `0` indicates no syntax errors.
8. **Update the cron definition** if it references the old path.
   - Ensure the `script` field points to the new path (e.g., `scrape-dev-patterns.sh`).
9. **Run a manual test** (optional)
   ```bash
   bash ~/.hermes/scripts/<script-name>.sh --dry-run
   ```
   - Confirm expected log output and no errors.
10. **Commit the change** (if under version control)
   ```bash
   git add ~/.hermes/scripts/<script-name>.sh
   git commit -m "fix: move cron wrapper into allowed directory and correct interpreter"
   ```
1. **Identify the failing wrapper**
   - Locate the wrapper script referenced in the cron definition (usually `~/.hermes/scripts/<name>.sh`).
   - Verify the error output for messages like "Blocked: script path resolves outside the scripts directory" or a Python `SyntaxError`.
2. **Copy the actual script into the allowed directory**
   ```bash
   mkdir -p ~/.hermes/scripts
   cp /path/to/original/script.sh ~/.hermes/scripts/<script-name>.sh
   ```
   Replace `/path/to/original/script.sh` with the real script location (often `~/.claude/bin/` or similar).
3. **Add a proper Bash shebang** (if missing)
   ```bash
   sed -i '1s|^|#!/usr/bin/env bash\n|' ~/.hermes/scripts/<script-name>.sh
   ```
4. **Ensure the script invokes the intended logic directly**
   - Remove any `exec bash "..." "$@"` wrapper lines; the script should be self‑contained.
   - If you need to delegate to another script, use `source /path/to/real.sh` *after* confirming it’s also within `~/.hermes/scripts`.
5. **Make the script executable**
   ```bash
   chmod +x ~/.hermes/scripts/<script-name>.sh
   ```
6. **Validate Bash syntax**
   ```bash
   bash -n ~/.hermes/scripts/<script-name>.sh
   ```
   - Exit code `0` indicates no syntax errors.
7. **Update the cron definition** if it references the old path.
   - Ensure the `script` field points to the new path (e.g., `scrape-dev-patterns.sh`).
8. **Run a manual test** (optional)
   ```bash
   bash ~/.hermes/scripts/<script-name>.sh --dry-run
   ```
   - Confirm expected log output and no errors.
9. **Commit the change** (if under version control)
   ```bash
   git add ~/.hermes/scripts/<script-name>.sh
   git commit -m "fix: move cron wrapper into allowed directory and correct interpreter"
   ```

## Pitfalls & Checks
- **Wrong interpreter**: Ensure the file starts with a Bash shebang; otherwise the system may invoke `/usr/bin/python` leading to `SyntaxError`.
- **Security guard**: Scripts outside `~/.hermes/scripts` are blocked. Always keep cron‑executed scripts inside this directory.
- **Executable flag**: Forgetting `chmod +x` results in `Permission denied`.
- **Residual wrapper lines**: Leaving an `exec bash "..." "$@"` line can cause double execution or path resolution errors.

## Verification
- Cron log should no longer contain "Blocked: script path resolves outside the scripts directory".
- Running the script manually should exit with code `0` and produce expected log entries.
- Subsequent cron runs should succeed; check the latest entry in `~/.claude/logs/<script>.log` for a successful completion line.

## References
- Hermes Cron Security Guard documentation (`~/.hermes/rules/cron.md`).
- Bash best practices for script wrappers.

## Related Skills
- `debug-shell-interpreter` – general debugging of interpreter mismatches.
- `post-mortem-script-not-found` – handling missing script errors.
