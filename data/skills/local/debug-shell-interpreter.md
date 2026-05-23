---
name: debug-shell-interpreter
description: Debugs shell scripts failing due to incorrect interpreter execution.
version: 1.0.0
author: Hermes
platforms: [linux, macos]
metadata:
  hermes:
    tags: [shell, bash, script, debugging, interpreter, devops]
---

# Debug Shell Script Interpreter Errors

This skill provides a systematic approach to diagnose and resolve issues where a shell script fails due to being executed by the wrong interpreter (e.g., a Bash script failing with a Python `SyntaxError`).

## When to Use

- A shell script (Bash, Zsh, etc.) is returning errors that are not typical for its language (e.g., `SyntaxError` from Python on a Bash script).
- You suspect the script is not being executed by the interpreter specified in its shebang.
- Permissions or environment variables might be interfering with script execution.

## Steps

1. **Identify the Script and Error**: ...
2. **Verify Script Shebang**: ...
3. **Check Script Permissions**: ...
4. **Test with Explicit Interpreter**: ...
5. **Examine Problematic Line**: ...
6. **Review Execution Environment (for cron jobs, services)**:
   - Ensure the cron entry calls the script with the correct interpreter (e.g., `bash /path/to/script.sh`).
   - Add a simple lockfile guard at the start of the script to prevent overlapping runs:
     ```bash
     LOCKFILE="/tmp/$(basename $0).lock"
     exec 200>"$LOCKFILE"
     flock -n 200 || { echo "Another instance is running, exiting."; exit 1; }
     ```
   - Verify required Python packages (e.g., `feedparser`) are installed.
7. **Additional Recommendations**: ...

1.  **Identify the Script and Error**:\n    -   Note the full path to the script (e.g., `/path/to/script.sh`).\n    -   Carefully read the error message. Pay close attention to the error type (e.g., `SyntaxError`) and the interpreter reporting it (e.g., Python).\n    -   **Note**: A `SyntaxError` from Python on a file with a bash shebang often indicates the script is being run by Python instead of bash, OR there's a bash syntax error that Python's error message is misreporting (though less common).

2.  **Verify Script Shebang**:
    -   The shebang (first line `#!/usr/bin/env bash` or `#!/bin/bash`) tells the system which interpreter to use.
    -   Check the shebang using `head -1 <script_path>`.

3.  **Check Script Permissions**:
    -   Ensure the script has execute permissions.
    -   Use `ls -l <script_path>`. Look for `x` (execute) in the permission string (e.g., `-rwxr-xr-x`). If missing, add with `chmod +x <script_path>`.

4.  **Test with Explicit Interpreter**:
    -   Try running the script explicitly with its intended interpreter to bypass any environment issues.
    -   For Bash: `bash -n <script_path>` (syntax check only)
    -   For Bash: `bash -x <script_path>` (execute with debug trace)
    -   If these commands work, the issue is likely in how the script is *called* (e.g., cron job, parent process), not the script itself.

5.  **Examine Problematic Line (if syntax is ambiguous)**:
    -   If the error points to a specific line and the syntax seems correct for the intended language, check for invisible characters that might be confusing the interpreter.
    -   Use `od -c <script_path> | grep -A 10 -B 10 'line_number'` or `sed -n '<line_number>p' <script_path> | od -c` to see characters in octal/character format.

6.  **Review Execution Environment (for cron jobs, services)**:
    -   If the script fails in a cron job, systemd service, or other automated environment, check the specific command used to invoke the script in that environment.
    -   Ensure the command is `bash /path/to/script.sh` or `./path/to/script.sh` (if executable) and not `python /path/to/script.sh` or similar.
    -   The `PATH` environment variable in cron jobs can be limited; explicitly calling `bash` might be necessary.

## Example Scenario

**Problem**: A script `my_script.sh` with `#!/usr/bin/env bash` fails in a cron job with `SyntaxError: invalid syntax`.

**Diagnosis Steps**:

1.  **Error**: Python `SyntaxError` on a Bash script.
2.  **Shebang**: `#!/usr/bin/env bash` (correct).
3.  **Permissions**: `ls -l my_script.sh` shows `-rwxr-xr-x` (correct).
4.  **Explicit Test**: `bash -x my_script.sh` runs successfully.
5.  **Conclusion**: The cron job is invoking `my_script.sh` with `python` instead of `bash`. The cron entry needs to be changed from `python /path/to/my_script.sh` to `bash /path/to/my_script.sh` or `/path/to/my_script.sh` directly (if `PATH` is set correctly for executables).

6.  **Additional Recommendations**:
   - Ensure the script uses Unix LF line endings (`dos2unix <script>` if needed).
   - In the cron file, set `SHELL=/bin/bash` or invoke the script explicitly with `bash /path/to/script.sh`.
   - Verify the script is executable (`chmod +x <script>`).
   - Optionally add a pre‑run sanity check, e.g., `head -1 <script> | grep -q '^#!.*bash'` to catch missing shebangs before execution.
