# airship / discovery

## Highest-value incremental improvement
Add a zero-config discovery CLI (`airship discover`) that:
1. Runs `granite-business-research.sh` (if present) to refresh market context
2. Executes `knowledge-rag` to query the top hub (e.g., MOC) and related docs
3. Prints a concise, actionable summary (<30s, no prompts)

This directly applies the pattern “review the most-connected hub before planning” and composes research + RAG into a single discovery flow.

---

## Implementation plan (<2h)

1. Create `/opt/axentx/airship/bin/airship` (CLI entrypoint)
   - Shebang `#!/usr/bin/env bash`
   - Subcommand `discover`
   - Auto-detect repo root via `.git` or `airship.toml`
   - Exit fast if no knowledge-rag or research script available (graceful no-op)

2. Create module `/opt/axentx/airship/lib/discovery.sh`
   - `run_granite_research()` — runs `scripts/granite-business-research.sh` if exists; captures last 20 lines to context file
   - `query_top_hub()` — invokes `knowledge-rag` with a canned query: “top hub by centrality and 5 most related docs”
   - `print_summary()` — outputs:
     - Top hub name + centrality score
     - Related docs (titles + short relevance)
     - Research highlights (if any)

3. Wire into repo
   - `chmod +x bin/airship`
   - Add to `PATH` via install target or instruct `./bin/airship discover`

4. Runtime behavior
   - Mac-only orchestration; no local model training
   - All heavy compute (RAG/LLM) delegated to remote (Surrogate/Lightning/HF CDN) via existing `knowledge-rag` tooling
   - Uses HF CDN bypass pattern: if `knowledge-rag` downloads datasets, rely on pre-listed file JSON + CDN-only fetches (zero API calls during query)

5. Logging & errors
   - Log to `/tmp/airship-discover.log`
   - Failures in research script do not block RAG query
   - Exit codes: 0=success (even if partial), 1=hard failure

---

## Code snippets

### `/opt/axentx/airship/bin/airship`
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIB_DIR="${REPO_ROOT}/lib"

# shellcheck source=../lib/discovery.sh
source "${LIB_DIR}/discovery.sh"

cmd="${1:-help}"
case "${cmd}" in
  discover)
    run_discovery
    ;;
  help|--help|-h)
    echo "Usage: airship <discover>"
    echo ""
    echo "Commands:"
    echo "  discover   Run business research + knowledge-rag to surface top hub and related docs"
    ;;
  *)
    echo "Unknown command: ${cmd}"
    echo "Use 'airship help' for usage."
    exit 1
    ;;
esac
```

### `/opt/axentx/airship/lib/discovery.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_FILE="/tmp/airship-discover.log"

exec_log() {
  echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] $*" >> "${LOG_FILE}"
}

run_granite_research() {
  local script="${REPO_ROOT}/scripts/granite-business-research.sh"
  if [[ -x "${script}" ]]; then
    exec_log "Running granite-business-research.sh"
    # Run with timeout to keep <30s total budget
    timeout 20s "${script}" >> "${LOG_FILE}" 2>&1 || true
    # Capture tail for summary
    tail -20 "${LOG_FILE}" | grep -i -E 'hub|moc|insight|finding' > "/tmp/airship_research_context.txt" || true
  else
    exec_log "No executable granite-business-research.sh found, skipping"
    touch "/tmp/airship_research_context.txt"
  fi
}

query_top_hub() {
  local query="top hub by centrality and 5 most related docs"
  exec_log "Querying knowledge-rag for: ${query}"

  # Prefer local CLI if available; fallback to curl against Surrogate
  if command -v knowledge-rag >/dev/null 2>&1; then
    knowledge-rag query "${query}" --limit 6 --format concise > "/tmp/airship_rag_output.txt" 2>> "${LOG_FILE}" || true
  else
    # Surrogate AI endpoint (adjust port if needed)
    local surrogate_url="http://localhost:8001/query"
    if curl -fs --max-time 15 -X POST -H "Content-Type: application/json" \
      -d "{\"query\":\"${query}\",\"limit\":6,\"format\":\"concise\"}" \
      "${surrogate_url}" > "/tmp/airship_rag_output.txt" 2>> "${LOG_FILE}"; then
      exec_log "RAG query via Surrogate succeeded"
    else
      exec_log "RAG query failed or Surrogate unavailable"
      echo "Top hub: unavailable (knowledge-rag not found)" > "/tmp/airship_rag_output.txt"
    fi
  fi
}

print_summary() {
  echo "=== Arkship Discovery Summary ==="
  echo ""

  if [[ -s "/tmp/airship_rag_output.txt" ]]; then
    echo "Top hub & related docs:"
    cat "/tmp/airship_rag_output.txt"
    echo ""
  else
    echo "Top hub & related docs: unavailable"
    echo ""
  fi

  if [[ -s "/tmp/airship_research_context.txt" ]]; then
    echo "Research highlights:"
    cat "/tmp/airship_research_context.txt"
    echo ""
  fi

  echo "Logs: ${LOG_FILE}"
}

run_discovery() {
  exec_log "Starting discovery"
  run_granite_research
  query_top_hub
  print_summary
  exec_log "Discovery completed"
}
```

---

## Usage
```bash
cd /opt/axentx/airship
./bin/airship discover
```

Expected output (example):
```
=== Arkship Discovery Summary ===

Top hub & related docs:
- Hub: MOC (centrality 0.92)
  Related: moc-incident-playbook.md, service-registry.md, blueprint-factory.md, temporal-workflows.md, artifact-registry.md

Research highlights:
- Finding: MOC remains highest-centrality hub for cross-team incident response
- Trend: Increased coupling between Service Registry and Blueprint Factory

Logs: /tmp/airship-discover.log
```
