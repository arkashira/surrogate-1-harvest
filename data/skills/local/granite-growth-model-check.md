---
name: granite-growth-model-check
description: Ensure granite‑growth.sh runs with an available Ollama model, applying a fallback if the default model is missing. Includes pre‑flight model verification, script patching, execution, and logging of patterns/lessons.
version: 1.0.0
author: Ashira
---

# Granite‑Growth Model Availability Skill

## Purpose
Run the `granite-growth.sh` script safely by verifying that the requested model exists in the local Ollama server. If the default model is unavailable, automatically select an alternative (e.g., `qwen-coder`) and patch the script.

## Prerequisites
- Bash (`#!/usr/bin/env bash`).
- Local Ollama server running on `http://localhost:11434`.
- Access to `~/.claude/bin/granite-growth.sh` and `~/.claude/memory/` files.

## Steps
1. **Query available models**:
   ```bash
   MODELS=$(curl -s http://localhost:11434/v1/models | jq -r '.data[].id')
   ```
2. **Determine desired model** (default is `granite4`).
3. **Check presence**:
   ```bash
   if echo "$MODELS" | grep -q "granite4"; then
       SELECTED="granite4"
   else
       # fallback to first known good model
       SELECTED="qwen-coder"
   fi
   ```
4. **Patch the script if needed** (only when fallback required):
   ```bash
   if [ "$SELECTED" != "granite4" ]; then
       patch --mode replace --old_string "--model granite4" --new_string "--model $SELECTED" ~/.claude/bin/granite-growth.sh
   fi
   ```
5. **Execute the script** and capture the decision document path:
   ```bash
   OUTPUT=$(bash ~/.claude/bin/granite-growth.sh 2>&1)
   echo "$OUTPUT"
   ```
6. **Log pattern** in `~/.claude/memory/knowledge_index.md`:
   ```bash
   cat >> ~/.claude/memory/knowledge_index.md <<EOF
- Pattern: granite growth script model availability | Fix: Verify local model list and use an existing model (e.g., qwen-coder) before invoking granite‑bridge; Tags: #granite #growth #model-switch #knowledge-rag
EOF
   ```
7. **Append lesson** to `~/.claude/memory/lessons_learned.md`:
   ```bash
   cat >> ~/.claude/memory/lessons_learned.md <<'EOF'
## $(date +%Y-%m-%d): Granite growth script model switch
- Context: Running granite‑growth.sh for Costinel; default model missing.
- Insight: Default model may not be installed locally; always verify.
- Fix/Pattern: Add pre‑flight model check and fallback logic.
- Prevention: Prevent HTTP‑404 failures from granite‑bridge.
- Tags: #granite #growth #model-switch #knowledge-rag
EOF
   ```
8. **Sync knowledge graph**:
   ```bash
   ~/.claude/bin/graph-sync.sh
   ```

## Pitfalls
- **Missing `jq`**: Install via `brew install jq` (macOS) or `apt-get install jq` (Linux).
- **Model list empty**: Ensure Ollama is running; start with `ollama serve`.
- **Patch idempotence**: Running the skill repeatedly will replace the same flag; safe because the script already contains the chosen model.

## Verification
- After execution, confirm the decision markdown exists at `~/ .hermes/workspace/swarm-shared/decisions/<timestamp>_Costinel_growth.md`.
- Check that the pattern entry appears in `knowledge_index.md` and the lesson entry appears in `lessons_learned.md`.

## References
- `~/.claude/bin/granite-growth.sh`
- Ollama API docs: http://localhost:11434/v1/models

---
