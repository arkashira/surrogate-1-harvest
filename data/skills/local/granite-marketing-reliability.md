---
name: granite-marketing-reliability
description: Ensure the granite-marketing.sh script runs reliably by checking model availability, handling HTTP 404 errors, and logging lessons.
author: Ashira
tags: [granite, marketing, reliability, devops]
---

# Purpose
Automate verification that the required Granite model is available before invoking `granite‑bridge` in the `granite-marketing.sh` script, provide a fallback model, and capture failures as lessons.

# Prerequisites
- Ollama installed and configured.
- Access to `granite‑bridge.sh` and `granite‑marketing.sh`.
- Write permissions to `~/.claude/memory` files.

# Steps
1. **Model Availability Check**
   ```bash
   #!/usr/bin/env bash
   set -euo pipefail
   MODEL="granite-4"
   if ! ollama list | grep -q "$MODEL"; then
     echo "Model $MODEL not found – falling back to qwen-coder"
     MODEL="qwen-coder"
   fi
   ```
   Use this snippet at the start of `granite‑marketing.sh`.
2. **Invoke granite‑bridge with the resolved model**
   ```bash
   response=$(granite-bridge.sh "$MODEL" "${prompt}")
   ```
3. **HTTP 404 Handling**
   ```bash
   if [[ "$response" == *"404"* ]]; then
     echo "granite‑bridge returned 404 – retry after 5s"
     sleep 5
     response=$(granite-bridge.sh "$MODEL" "${prompt}")
   fi
   ```
   # Timeout handling
   if [[ -z "$response" ]] || grep -q "timeout" <<<"$response"; then
     echo "No response or timeout – retry up to 3 times"
     for i in {1..3}; do
       response=$(granite-bridge.sh "$MODEL" "${prompt}")
       if [[ -n "$response" && ! grep -q "timeout" <<<"$response" ]]; then
         break
       fi
       sleep $((i*5))
     done
   fi
   ```bash
   if [[ "$response" == *"404"* ]]; then
     echo "granite‑bridge returned 404 – retry after 5s"
     sleep 5
     response=$(granite-bridge.sh "$MODEL" "${prompt}")
   fi
   ```
4. **Logging**
   Append any failures to `~/.claude/memory/lessons_learned.md` using the self‑improvement pattern (see `self-improvement` skill).
5. **Pattern Registration**
   After confirming the fix works, add a pattern entry to `knowledge_index.md`:
   ```text
   - Pattern: granite marketing model unavailability | Fix: Verify model with `ollama list` and fallback; Tags: #granite #model-unavailability
   ```
6. **Graph Sync**
   Run `~/.claude/bin/graph-sync.sh` to index the new lesson.

# Pitfalls
- Ensure the script has Unix LF line endings (`dos2unix` if needed).
- The `ollama list` command must be in PATH; otherwise provide full path.
- Permissions on `lessons_learned.md` may require `chmod u+w`.

# Verification
- Run `granite-marketing.sh` for a project and confirm no HTTP 404 appears.
- Check that the fallback model is used when the original model is missing.
- Verify a new lesson entry is appended to `lessons_learned.md`.

# References
- Self‑Improvement skill for logging lessons.
- Knowledge‑RAG skill for querying top hubs.
