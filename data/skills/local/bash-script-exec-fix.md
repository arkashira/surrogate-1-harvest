---
name: bash-script-exec-fix
title: Fix Bash Script exec Syntax
description: Correct the use of the `exec` builtin in wrapper bash scripts by removing quotes around the script path.
category: devops
tags: [bash, exec, scripting]
author: Hermes
version: 1.0.0
---

## Steps
1. Open the wrapper script and locate the `exec` line that incorrectly quotes the target script path, e.g.
   ```bash
   exec "path/to/target.sh" "$@"
   ```
2. Replace it with an unquoted `exec` so the shell treats it as a builtin:
   ```bash
   exec /full/path/to/target.sh "$@"
   ```
3. Ensure the target script exists, is within the allowed sandbox directory, and has executable permissions (`chmod +x`).
4. Run the wrapper script to verify it exits cleanly and forwards any arguments.

## Pitfalls
- Quoting the command path makes the shell interpret `exec` as a function call, resulting in a `SyntaxError`.
- Omitting `$@` will drop any arguments passed to the wrapper.
- The target script must be inside the trusted directory; otherwise Hermes will block execution.

## References
- Bash builtin `exec`: https://www.gnu.org/software/bash/manual/bash.html#Shell-Builtin-Commands
- Exec command usage: https://tldp.org/LDP/abs/html/internalcommands.html#EXECPATH