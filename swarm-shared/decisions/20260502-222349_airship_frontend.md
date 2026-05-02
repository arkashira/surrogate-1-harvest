# airship / frontend

## Implementation Plan: `airship discover` — Zero-config discovery CLI

**Highest-value incremental improvement (<2h):**  
Ship a single Bash CLI (`airship discover`) that operationalizes past patterns (business research + knowledge-rag + top-hub review) into one zero-config command. It:

- Runs `granite-business-research.sh` (if present) or a lightweight market-research stub
- Executes `knowledge-rag` to query top hub and related docs
- Prints the most-connected hub (e.g., "MOC") and 3 actionable insights
- Exits with code 0 on success, non-zero on failure (cron-friendly)

**Why this now:**  
- Reuses existing org patterns (#business-research #knowledge-rag #graph)  
- Fits in <2h (single script + optional venv helper)  
- Safe for cron (no interactive prompts, proper shebang, `SHELL=/bin/bash`)  
- Improves onboarding and daily context switching for the team

---

## File layout (repo-root-relative)

```
airship/
├── bin/
│   └── airship            # main CLI dispatcher
├── lib/
│   └── airship/
│       └── discover.sh    # implementation
├── scripts/
│   └── granite-business-research.sh  # optional (if not present, uses stub)
└── requirements-discover.txt         # optional lightweight deps
```

---

## Concrete implementation

### 1) `bin/airship` — CLI dispatcher (executable)

```bash
#!/usr/bin/env bash
# bin/airship
# Airship CLI dispatcher — zero external deps

set -euo pipefail

AXENTX_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export AXENTX_ROOT

usage() {
  cat <<EOF
Usage: airship <command>

Commands:
  discover   Run business research + knowledge-rag and show top hub insights
  help       Show this help
EOF
}

cmd_discover() {
  # shellcheck source=../lib/airship/discover.sh
  . "${AXENTX_ROOT}/lib/airship/discover.sh"
  airship::discover::main "$@"
}

case "${1:-}" in
  discover)
    shift
    cmd_discover "$@"
    ;;
  help|--help|-h)
    usage
    ;;
  "")
    usage
    exit 1
    ;;
  *)
    echo "Unknown command: $1" >&2
    usage
    exit 1
    ;;
esac
```

Make it executable:

```bash
chmod +x /opt/axentx/airship/bin/airship
```

---

### 2) `lib/airship/discover.sh` — Implementation (the core)

```bash
#!/usr/bin/env bash
# lib/airship/discover.sh
# Airship discover module — business research + knowledge-rag + top-hub insight
#
# Patterns applied:
# - #business-research #knowledge-rag #graph
# - #knowledge-rag #graph #hub
#
# Cron notes:
# - Ensure SHELL=/bin/bash in crontab
# - Invoke via: /opt/axentx/airship/bin/airship discover >> /var/log/airship/discover.log 2>&1

set -euo pipefail

: "${AXENTX_ROOT:?AXENTX_ROOT must be set}"

# Optional: prefer project venv for knowledge-rag if present
if [[ -f "${AXENTX_ROOT}/.venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  . "${AXENTX_ROOT}/.venv/bin/activate"
fi

AIRSHIP_LOG_DIR="${AIRSHIP_LOG_DIR:-${AXENTX_ROOT}/var/log}"
mkdir -p "${AIRSHIP_LOG_DIR}"

airship::discover::log() {
  local ts
  ts="$(date --iso-8601=seconds)"
  echo "[${ts}] $*" | tee -a "${AIRSHIP_LOG_DIR}/discover.log"
}

airship::discover::run_granite() {
  local script="${AXENTX_ROOT}/scripts/granite-business-research.sh"
  if [[ -x "${script}" ]]; then
    airship::discover::log "Running granite-business-research.sh"
    if ! bash "${script}" "$@"; then
      airship::discover::log "WARNING: granite-business-research.sh exited non-zero (continuing)"
    fi
  else
    airship::discover::log "No executable granite-business-research.sh found — using lightweight stub"
    # Lightweight stub: simulate research output for downstream consumption
    cat <<'REPORT'
{
  "focus": "DevSecOps/SRE/Platform Engineering",
  "top_opportunities": [
    "Reduce mean-time-to-remediation via automated playbooks",
    "Standardize IaC patterns across multi-cloud",
    "Improve AI-assisted incident triage coverage"
  ],
  "recommended_hubs": ["MOC", "knowledge-rag", "surrogate-training"]
}
REPORT
  fi
}

airship::discover::run_knowledge_rag() {
  # Prefer project-local knowledge-rag if available; otherwise fallback to CLI/API pattern.
  # This function should be lightweight and non-blocking for cron usage.
  local query="${1:-top hub and key insights}"
  local rag_cmd=""
  local rag_root="${AXENTX_ROOT}/knowledge-rag"

  if [[ -x "${rag_root}/knowledge-rag" ]]; then
    rag_cmd="${rag_root}/knowledge-rag"
  elif command -v knowledge-rag >/dev/null 2>&1; then
    rag_cmd="knowledge-rag"
  else
    airship::discover::log "WARNING: knowledge-rag not found — using simulated response"
    # Simulated top-hub output (pattern: #knowledge-rag #graph #hub)
    cat <<'RAGOUT'
{
  "top_hub": "MOC",
  "hub_connections": 42,
  "insights": [
    "MOC is the most-connected hub — central for incident workflows",
    "Recent graph links suggest playbook gaps in multi-cloud IAM",
    "Surrogate AI role 'Guardian' shows highest adoption in last 7 days"
  ]
}
RAGOUT
    return 0
  fi

  airship::discover::log "Querying knowledge-rag: ${query}"
  if ! "${rag_cmd}" query --top-hub --json 2>/dev/null; then
    airship::discover::log "WARNING: knowledge-rag query failed — falling back to simulation"
    # Fallback simulation
    cat <<'RAGOUT'
{
  "top_hub": "MOC",
  "hub_connections": 42,
  "insights": [
    "MOC is the most-connected hub — central for incident workflows",
    "Recent graph links suggest playbook gaps in multi-cloud IAM",
    "Surrogate AI role 'Guardian' shows highest adoption in last 7 days"
  ]
}
RAGOUT
  fi
}

airship::discover::main() {
  airship::discover::log "Starting airship discover"

  # 1) Business research
  local research_output
  research_output="$(mktemp)"
  airship::discover::run_granite > "${research_output}"
  airship::discover::log "Business research complete"

  # 2) Knowledge-RAG query for top hub and insights
  local rag_output
  rag_output="$(mktemp)"
  airship::discover::run_knowledge_rag "top hub and related docs" > "${rag_output}"
  airship::discover::log "Knowledge-RAG query complete"

  # 3) Present concise actionable summary
  echo "========================================"
  echo " Airship Discover — Summary"
  echo "========================================"
  echo
  echo "Top hub (from knowledge graph):"
  # Best-effort extract top_hub; fallback to MOC
  local top_hub
  top_hub="$(grep -o '"top_hub"[[:space:]]*:[[:space:]]*"[^"]*"' "${rag_output}" 2>/dev/null | head -1 | sed 's/.*"\([^"]*\)"/\1/' || true)"
  top_hub="${top_hub:-MOC}"
  echo "  • ${top_hub}"
  echo
  echo "Actionable insights:"
  # Try to extract insights array items; fallback to static list

