# airship / discovery

## Implementation Plan: `airship discover` Zero-config CLI

**Highest-value incremental improvement**: Ship a single-command discovery surface that:
1. Executes business research (`granite-business-research.sh`)
2. Runs knowledge-rag to surface top hub + MOC insights
3. Emits strategic context for Arkship/Surrogate platform decisions
4. Follows past patterns (bash wrapper, proper shebang, executable, cron-safe)

**Time estimate**: 90–110 min (script + integration + smoke test)

---

### 1. Create CLI entrypoint (Bash wrapper)

`/opt/axentx/airship/bin/airship-discover`

```bash
#!/usr/bin/env bash
# airship-discover — Zero-config discovery CLI
# Usage: airship-discover [--quiet|--verbose] [--no-rag] [--no-research]
#
# Patterns applied:
# - #bash #script-error #wrapper
# - #business-research #knowledge-rag #graph #hub
# - Surrogate-1 training lessons: avoid heavy compute on Mac; orchestrate only

set -euo pipefail
IFS=$'\n\t'

# ---- Configuration ----
AIRSHIP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${AIRSHIP_ROOT}/var/log"
LOG_FILE="${LOG_DIR}/airship-discover-$(date +%Y%m%d-%H%M%S).log"

# ---- Helpers ----
log() {
  local level="$1"
  shift
  local message="$*"
  local ts
  ts="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  echo "${ts} [${level}] ${message}" | tee -a "${LOG_FILE}"
}

info()  { log "INFO"  "$@"; }
warn()  { log "WARN"  "$@"; }
error() { log "ERROR" "$@"; }

# ---- Flags ----
QUIET=false
VERBOSE=false
DO_RESEARCH=true
DO_RAG=true

while [[ $# -gt 0 ]]; do
  case "$1" in
    --quiet)       QUIET=true; shift ;;
    --verbose)     VERBOSE=true; shift ;;
    --no-research) DO_RESEARCH=false; shift ;;
    --no-rag)      DO_RAG=false; shift ;;
    --help|-h)
      echo "Usage: $(basename "$0") [--quiet|--verbose] [--no-research] [--no-rag]"
      echo ""
      echo "Zero-config discovery for Arkship + Surrogate platform."
      echo ""
      echo "Patterns applied:"
      echo "  - business research + knowledge-rag + top-hub/MOC insight"
      exit 0
      ;;
    *)
      error "Unknown option: $1"
      exit 1
      ;;
  esac
done

if [[ "$QUIET" == true ]]; then
  exec 1>/dev/null
fi

if [[ "$VERBOSE" == true ]]; then
  set -x
fi

# ---- Preconditions ----
mkdir -p "${LOG_DIR}"

info "airship-discover started (root: ${AIRSHIP_ROOT})"

# ---- 1) Business research ----
if [[ "$DO_RESEARCH" == true ]]; then
  RESEARCH_SCRIPT="${AIRSHIP_ROOT}/scripts/granite-business-research.sh"
  if [[ -x "${RESEARCH_SCRIPT}" ]]; then
    info "Running business research: ${RESEARCH_SCRIPT}"
    if bash "${RESEARCH_SCRIPT}" >> "${LOG_FILE}" 2>&1; then
      info "Business research completed"
    else
      warn "Business research exited non-zero (see ${LOG_FILE})"
    fi
  else
    warn "Research script not found or not executable: ${RESEARCH_SCRIPT}"
  fi
fi

# ---- 2) Knowledge RAG: top hub + MOC ----
if [[ "$DO_RAG" == true ]]; then
  RAG_SCRIPT="${AIRSHIP_ROOT}/scripts/knowledge-rag.sh"
  if [[ -x "${RAG_SCRIPT}" ]]; then
    info "Running knowledge-rag (top hub + MOC insight)"

    # Query top hub
    info "Querying top-connected hub..."
    if bash "${RAG_SCRIPT}" --query "top hub" --format concise >> "${LOG_FILE}" 2>&1; then
      info "Top hub query completed"
    else
      warn "Top hub query failed (non-fatal)"
    fi

    # Query MOC (most-connected) insight per pattern
    info "Querying MOC (most-connected) insight..."
    if bash "${RAG_SCRIPT}" --query "MOC" --format concise >> "${LOG_FILE}" 2>&1; then
      info "MOC insight completed"
    else
      warn "MOC query failed (non-fatal)"
    fi

    # Strategic context for Arkship/Surrogate
    info "Querying strategic context for Arkship + Surrogate platform..."
    if bash "${RAG_SCRIPT}" \
      --query "Arkship Surrogate platform strategic context DevOps AI" \
      --format concise \
      >> "${LOG_FILE}" 2>&1; then
      info "Strategic context query completed"
    else
      warn "Strategic context query failed (non-fatal)"
    fi
  else
    warn "RAG script not found or not executable: ${RAG_SCRIPT}"
  fi
fi

info "airship-discover finished (log: ${LOG_FILE})"
```

Make executable:

```bash
chmod +x /opt/axentx/airship/bin/airship-discover
```

---

### 2. Convenience symlink (global command)

```bash
ln -sf /opt/axentx/airship/bin/airship-discover /usr/local/bin/airship-discover
```

---

### 3. Cron-safe invocation template

If scheduling via cron (e.g., nightly discovery), set `SHELL=/bin/bash` and invoke via `bash` for robustness:

```cron
SHELL=/bin/bash
0 6 * * * /usr/local/bin/airship-discover --quiet >> /opt/axentx/airship/var/log/cron-discover.log 2>&1
```

---

### 4. Optional: integrate into Makefile (if present)

```make
discover:
	@/usr/local/bin/airship-discover --verbose

discover-quiet:
	@/usr/local/bin/airship-discover --quiet
```

---

### 5. Smoke test

```bash
# Dry run (verbose)
airship-discover --verbose

# Quiet run
airship-discover --quiet

# Without RAG (research only)
airship-discover --no-rag
```

Expected outcome:
- Runs `granite-business-research.sh` (if present/executable)
- Runs `knowledge-rag.sh` for top hub, MOC, and strategic context
- Logs to `var/log/airship-discover-*.log`
- Exit 0 unless fatal precondition fails

---

### Patterns applied

- `#bash #script-error #wrapper` — proper shebang, `set -euo pipefail`, executable, invoked via `bash`
- `#business-research #knowledge-rag #graph` — runs research then RAG queries
- `#hub` — explicit top-hub + MOC insight queries
- Cron-safe (`SHELL=/bin/bash`) and quiet/verbose modes for automation
- No heavy compute on Mac — orchestrates only (per surrogate-1 lessons)
