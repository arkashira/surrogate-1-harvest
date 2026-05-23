---
name: granite-bridge-model-update
description: Procedure to diagnose and fix HTTP 404 errors from granite-bridge when the specified model is unavailable, and to update scripts to use an available OLLAMA model.
category: devops
author: Ashira
tags: [model, ollama, script, patch, troubleshooting]
---
## Overview
When `granite-bridge.sh` returns `HTTP Error 404: Not Found` the requested model is not installed on the local OLLAMA server. This skill outlines how to identify the missing model, select a suitable replacement, patch the bridge and any dependent scripts, and verify functionality.

## Steps
1. **Reproduce the error**
   ```bash
   ~/.claude/bin/granite-bridge.sh --model granite4 --max-tokens 1200 "test"
   ```
2. **List available OLLAMA models**
   ```bash
   ollama list
   ```
   Verify that the model used (`granite4` or `granite4`) is absent and note an alternative (e.g., `qwen-coder`).
3. **Choose a replacement model**
   Preferred: a coder‑oriented model that supports the same interface, such as `qwen-coder`.
4. **Patch `granite-bridge.sh`**
   Replace the default model argument or any hard‑coded `granite4` occurrences:
   ```bash
   patch -p0 <<'EOF'
   *** Begin Patch
   *** Update File: ~/.claude/bin/granite-bridge.sh
  @@
   -model="granite4"
   +model="qwen-coder"
   *** End Patch
   EOF
   ```
   Ensure the change is saved with `patch` (not direct redirection).
5. **Search for other scripts invoking `granite-bridge.sh` with `granite4`**
   ```bash
   search_files pattern="granite4" target="content" path="~/.claude/bin"
   ```
   Patch each occurrence similarly or update the call to pass `--model qwen-coder`.
6. **Run a sanity test**
   ```bash
   ~/.claude/bin/granite-bridge.sh --model qwen-coder --max-tokens 1200 "Hello world"
   ```
   Expect a non‑error JSON response.
7. **Validate dependent workflows** (e.g., `granite-deduper.sh`) by running them on a sample backlog and confirming deduplication output.
8. **Record the fix**
   Append a pattern entry to `knowledge_index.md` and a lesson to `lessons_learned.md` using the standard scripts (see `knowledge-rag` skill).

## Pitfalls
- Forgetting to update scripts that invoke `granite-bridge.sh` with an explicit model argument; they will continue to fail.
- Using a model that lacks the required tooling (e.g., not a coder model) may cause malformed responses.
- Not re‑starting any long‑running background services that cache the old model name.

## Verification
- `granite-bridge.sh` runs without HTTP errors.
- All scripts that previously failed due to `granite4` now succeed.
- Logs (`~/.claude/logs/granite-bridge.log`) contain successful request entries.

## References
- `~/.claude/bin/granite-bridge.sh`
- `~/.claude/bin/granite-deduper.sh`
- OLLAMA documentation for model management
