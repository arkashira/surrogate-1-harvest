---
name: budget-tracker-wrapper-fix
description: Fix Bash wrapper scripts that are mistakenly executed with Python, causing SyntaxError on exec lines.
version: 1.0.0
author: Ashira
---

## Problem
A wrapper script (e.g., `~/.hermes/scripts/budget-tracker.sh`) intended to exec a real Bash script is being run by the Python interpreter, resulting in a SyntaxError like:
```
Missing parentheses in call to 'exec'. Did you mean exec(...)?
```
This typically happens when the script:
- Lacks a proper Bash shebang as the very first line.
- Has Windows CRLF line endings, preventing the shebang from being recognized.
- Is not executable or invoked from a cron environment that defaults to `/bin/sh` or Python.

## Fix Steps
1. **Add/Verify Shebang**
   ```bash
   #!/usr/bin/env bash
   ```
   Must be the very first line, with no leading whitespace.
2. **Normalize Line Endings**
   ```bash
   dos2unix ~/.hermes/scripts/budget-tracker.sh
   ```
3. **Make Executable**
   ```bash
   chmod +x ~/.hermes/scripts/budget-tracker.sh
   ```
4. **Correct Exec Invocation**
   Use an unquoted exec pointing to the real script:
   ```bash
   exec /Users/Ashira/.claude/bin/budget-tracker.sh "$@"
   ```
5. **Cron Invocation (if applicable)**
   - Ensure the crontab sets `SHELL=/bin/bash`.
   - Or call the wrapper explicitly with Bash:
     ```bash
     bash ~/.hermes/scripts/budget-tracker.sh "$@"
     ```
6. **Validate**
   Run manually to confirm no Python error:
   ```bash
   ~/.hermes/scripts/budget-tracker.sh
   ```

## Prevention
- Lint all wrapper scripts for a correct Bash shebang and Unix LF line endings (e.g., using `shellcheck`).
- Add a pre‑run validation step in cron wrappers:
  ```bash
  head -1 "$SCRIPT" | grep -q '^#!.*bash' || { echo "Missing bash shebang"; exit 1; }
  ```
- Keep a checklist in your CI pipeline for script validation.
- **When invoking the real script, use an absolute path without quoting the command to avoid interpreter confusion.**

- Lint all wrapper scripts for a correct Bash shebang and Unix LF line endings (e.g., using `shellcheck`).
- Add a pre‑run validation step in cron wrappers:
  ```bash
  head -1 "$SCRIPT" | grep -q '^#!.*bash' || { echo "Missing bash shebang"; exit 1; }
  ```
- Keep a checklist in your CI pipeline for script validation.

## Tags
#bash #script-error #wrapper #budget-tracker #cron