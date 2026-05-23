---
name: bash-shebang-validator
description: Validate Bash scripts for proper shebang, line endings, executable permission, and correct interpreter invocation to prevent Python SyntaxError misfires.
version: 1.0.0
author: Ashira
platforms: [linux, macos]
metadata:
  hermes:
    tags: [bash, script, validator, shebang, line-endings, exec]
---

# Bash Shebang Validator Skill

## Overview
This skill provides a checklist and automated steps to ensure Bash wrapper scripts execute correctly. It prevents errors where the OS mistakenly invokes the Python interpreter due to a missing or malformed shebang.

## Steps
1. **Check for Shebang**
   - Verify the first line of the script is exactly `#!/usr/bin/env bash` with **no preceding whitespace**.
   - If missing, prepend the shebang.
2. **Normalize Line Endings**
   - Ensure the file uses UNIX LF line endings. Run `dos2unix <script>` if needed.
3. **Set Executable Permission**
   - Run `chmod +x <script>`.
4. **Verify Interpreter**
   - Use `file <script>`; it should report `POSIX shell script` or `Bourne-Again shell script`.
   - Optionally, run `bash -n <script>` for syntax check (no execution).
5. **Execute via Bash**
   - When invoking from cron or another script, call explicitly with `bash <script>` or ensure the script is directly executable.
   - For wrapper scripts that delegate to another script, use `exec /full/path/to/inner.sh "$@"` **without extra quoting** around the script path. This avoids the Python `SyntaxError` caused by mistaken interpreter invocation.
   - Example:
     ```bash
     #!/usr/bin/env bash
     set -euo pipefail
     exec /Users/Ashira/.claude/bin/budget-tracker.sh "$@"
     ```
6. **Automated Lint Hook (optional)**
   - Add a pre‑commit hook that runs the above checks on any staged `.sh` files.

## Pitfalls
- **Leading Blank Lines** before the shebang cause the OS to ignore it.
- **Windows CRLF** line endings make the shebang ineffective.
- **Missing Execute Bit** leads to “Permission denied” errors.
- **Calling the script without a shebang** (e.g., `./script.sh` where the shebang is wrong) can cause the default interpreter to be Python on systems where `.py` is associated.

## Verification
- After applying the steps, running the script should not produce a Python `SyntaxError`.
- `file <script>` output should include `Bourne-Again shell script`.
- `bash -n <script>` should exit with code `0`.

## Tags
#bash #script-error #shebang #validation #devops
