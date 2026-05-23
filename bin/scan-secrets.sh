#!/usr/bin/env bash
# Pre-push secret scanner — gitleaks-style regex over staged + commit-range diff.
#
# Modes:
#   bin/scan-secrets.sh                # scan staged diff (pre-commit / pre-push)
#   bin/scan-secrets.sh --range A..B   # scan diff between two refs (CI)
#   bin/scan-secrets.sh --install-hook # install .git/hooks/pre-push
#
# Exit:
#   0 — clean
#   1 — secrets found (lists them with file:line) — DO NOT push
#   2 — bad invocation
set -uo pipefail

MODE="staged"
RANGE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --range)         MODE="range"; RANGE="$2"; shift 2 ;;
    --install-hook)  MODE="install"; shift ;;
    -h|--help)
      cat <<EOF
Usage: scan-secrets.sh [--range A..B] [--install-hook]
EOF
      exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Patterns — superset of bin/lib/pii_scrub.py with extras (private keys, cookies).
PATTERNS=(
  # AWS
  '(?:AKIA|ASIA)[0-9A-Z]{16}'
  '(?:aws_secret_access_key|AWS_SECRET_ACCESS_KEY)\s*[:=]\s*["'"'"']?[A-Za-z0-9/+=]{40}'
  # HF
  'hf_[A-Za-z0-9]{30,}'
  # GitHub
  'gh[pousr]_[A-Za-z0-9]{30,}'
  'github_pat_[A-Za-z0-9_]{20,}'
  # OpenAI / Anthropic
  'sk-[A-Za-z0-9]{32,}'
  'sk-ant-[A-Za-z0-9-]{40,}'
  # Slack
  'xox[baprs]-[A-Za-z0-9-]{10,}'
  # Discord webhooks
  'discord(?:app)?\.com/api/webhooks/[0-9]+/[A-Za-z0-9_-]+'
  # Google API
  'AIza[0-9A-Za-z_-]{35}'
  # Stripe
  '(?:sk|rk)_(?:test|live)_[A-Za-z0-9]{24,}'
  # JWT (header.body.sig — restrict by length to avoid false-positives)
  'eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}'
  # Private keys
  '-----BEGIN (?:RSA |OPENSSH |EC |DSA |PGP )?PRIVATE KEY( BLOCK)?-----'
  # Generic high-entropy assignments
  '(?:password|passwd|pwd|api[_-]?key|secret|token)\s*[:=]\s*["'"'"'][A-Za-z0-9+/=_-]{20,}["'"'"']'
)

# Allowlist patterns (false-positive killers).
ALLOWLIST=(
  '\[REDACTED_'
  'EXAMPLE'
  '<your[_-]'
  'xxxxxxxx'
  'placeholder'
  '\.lock\b'
  '\.snap\b'
)

# Build a single ERE-friendly OR (drops PCRE-only constructs).
build_pattern_or() {
  local IFS='|'; echo "$*"
}

PATTERN_OR=$(build_pattern_or "${PATTERNS[@]}")
ALLOW_OR=$(build_pattern_or "${ALLOWLIST[@]}")

if [[ "$MODE" == "install" ]]; then
  HOOK=".git/hooks/pre-push"
  if [[ ! -d ".git" ]]; then
    echo "not a git repo (run from repo root)" >&2
    exit 2
  fi
  cat > "$HOOK" <<'HOOK'
#!/usr/bin/env bash
# Auto-installed by bin/scan-secrets.sh
SCAN_PATH="$(git rev-parse --show-toplevel)/bin/scan-secrets.sh"
if [[ -x "$SCAN_PATH" ]]; then
  "$SCAN_PATH" --range "@{push}..HEAD" || exit 1
fi
exit 0
HOOK
  chmod +x "$HOOK"
  echo "installed: $HOOK"
  exit 0
fi

# Get the diff to scan.
if [[ "$MODE" == "range" ]]; then
  if [[ -z "$RANGE" ]]; then
    echo "missing range" >&2; exit 2
  fi
  DIFF_CMD="git diff --unified=0 $RANGE"
else
  DIFF_CMD="git diff --staged --unified=0"
fi

DIFF_OUT=$(eval "$DIFF_CMD" 2>/dev/null || true)
if [[ -z "$DIFF_OUT" ]]; then
  echo "no diff to scan"
  exit 0
fi

# Walk the diff line-by-line; track current file + line number.
HITS=0
CURRENT_FILE=""
CURRENT_LINE=0

while IFS= read -r line; do
  if [[ "$line" =~ ^\+\+\+\ b/(.+)$ ]]; then
    CURRENT_FILE="${BASH_REMATCH[1]}"
    continue
  fi
  if [[ "$line" =~ ^@@\ -[0-9]+(,[0-9]+)?\ \+([0-9]+) ]]; then
    CURRENT_LINE="${BASH_REMATCH[2]}"
    continue
  fi
  # Only inspect added lines.
  if [[ "$line" == +* && "$line" != "+++"* ]]; then
    payload="${line:1}"
    if echo "$payload" | grep -E -i "$ALLOW_OR" >/dev/null 2>&1; then
      CURRENT_LINE=$((CURRENT_LINE + 1))
      continue
    fi
    if echo "$payload" | grep -E "$PATTERN_OR" >/dev/null 2>&1; then
      MATCH=$(echo "$payload" | grep -E -o "$PATTERN_OR" | head -1)
      MASK="${MATCH:0:8}…${MATCH: -4}"
      echo "🚨 SECRET in $CURRENT_FILE:$CURRENT_LINE  → $MASK" >&2
      HITS=$((HITS + 1))
    fi
    CURRENT_LINE=$((CURRENT_LINE + 1))
  fi
done <<< "$DIFF_OUT"

if [[ "$HITS" -gt 0 ]]; then
  echo "" >&2
  echo "❌ $HITS secret(s) detected — push BLOCKED" >&2
  echo "   if false positive: add allowlist comment, or use 'git push --no-verify' (DANGEROUS)" >&2
  exit 1
fi

echo "✅ secret scan clean"
exit 0
