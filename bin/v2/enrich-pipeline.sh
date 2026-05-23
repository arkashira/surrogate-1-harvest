#!/usr/bin/env bash
# Surrogate-1 v2 — Enrichment pipeline.
#
# After bulk-mirror or streaming-mirror writes raw rows, enrich them:
#   1. sanitize (lib/sanitize.py — pollution + PII + low-quality drop)
#   2. dedup (lib/dedup.py central SQLite store)
#   3. tag (categorize by domain via heuristic OR local LLM)
#   4. format (standardize {prompt, response, source, meta})
#   5. abstract-cot compress (if reasoning-heavy)
#   6. teachable filter (only keep 30-70% baseline accuracy if SFT data)
#
# Output: ~/.surrogate/data/v2/enriched/<source>-<date>.jsonl ready for training.
#
# Cron: every 60 min on offset 35.
#
# Run modes:
#   bash enrich-pipeline.sh                         # process all bulk-mirror/*.jsonl
#   bash enrich-pipeline.sh /path/to/file.jsonl     # process one file
set -uo pipefail
[[ -f "$HOME/.hermes/.env" ]] && { set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a; }

IN_DIR="$HOME/.surrogate/data/bulk-mirror"
OUT_DIR="$HOME/.surrogate/data/v2/enriched"
LOG="$HOME/.surrogate/logs/enrich-pipeline.log"
mkdir -p "$OUT_DIR" "$(dirname "$LOG")"
DATE=$(date +%Y%m%d)
START=$(date +%s)
DEADLINE=$((START + 3000))   # 50 min budget

if [[ -n "${1:-}" ]]; then
    FILES=("$1")
else
    # Process oldest unenriched files first
    FILES=()
    while IFS= read -r f; do
        bn=$(basename "$f" .jsonl)
        [[ -f "$OUT_DIR/${bn}-${DATE}.jsonl" ]] && continue
        FILES+=("$f")
    done < <(find "$IN_DIR" -name "*.jsonl" -size +1k 2>/dev/null | sort)
fi

[[ ${#FILES[@]} -eq 0 ]] && { echo "[$(date +%H:%M:%S)] no files to enrich" >> "$LOG"; exit 0; }

echo "[$(date +%H:%M:%S)] enrich start — ${#FILES[@]} file(s)" | tee -a "$LOG"

n_total=0
for f in "${FILES[@]}"; do
    NOW=$(date +%s)
    (( NOW > DEADLINE )) && { echo "[$(date +%H:%M:%S)] deadline" | tee -a "$LOG"; break; }
    bn=$(basename "$f" .jsonl)
    out="$OUT_DIR/${bn}-${DATE}.jsonl"
    echo "[$(date +%H:%M:%S)] $bn" | tee -a "$LOG"

    F_IN="$f" F_OUT="$out" python3 - <<'PYEOF' 2>>"$LOG"
import json, os, sys, hashlib, re
from pathlib import Path
sys.path.insert(0, str(Path.home() / ".surrogate/bin/lib"))
sys.path.insert(0, str(Path.home() / ".surrogate/bin/v2"))
from sanitize import filter_pair
try: from dedup import DedupStore; HAS_DEDUP = True
except Exception: HAS_DEDUP = False

# Domain detector (reuse inference-augment if available)
def detect_domain(prompt: str, response: str = "") -> str:
    text = (prompt + " " + response).lower()
    rules = [
        ("sec-iam",      ["iam:","policy","principal","least privilege","assume role"]),
        ("sec-secrets",  ["secret","api key","token","password","credentials"]),
        ("sec-cve",      ["cve-","vulnerability","exploit","remediation","patch"]),
        ("devops-tf",    ["terraform","resource \"","provider \"","tflint",".tf"]),
        ("devops-k8s",   ["kubernetes","kubectl","kind: deployment","helm","kustomize"]),
        ("devops-cdk",   ["aws-cdk","cdk synth","Stack","CfnOutput"]),
        ("ci-github",    ["github actions",".github/workflows","uses: actions/"]),
        ("sre-runbook",  ["runbook","incident","on-call","page","escalation"]),
        ("sre-slo",      ["sli","slo","error budget","latency p99"]),
        ("data-sql",     ["select ","from ","join ","where ","create table"]),
        ("ai-eng",       ["embedding","rag","vector","lora","fine-tune","vllm"]),
        ("api-rest",     ["rest api","openapi","endpoint","GET /","POST /"]),
        ("test-pytest",  ["pytest","@pytest.fixture","assert ","unittest"]),
        ("debug-traceback",["traceback","stack trace","valueerror","typeerror"]),
        ("perf-profile", ["profile","bottleneck","latency","throughput","cprofile"]),
        ("docs-api",     ["api documentation","endpoint reference","sdk"]),
        ("arch-adr",     ["adr","trade-off","decision record","architecture"]),
        ("cloud-cost",   ["cost","spend","savings plan","reserved instance"]),
        ("compliance",   ["soc 2","iso 27001","hipaa","pci-dss","gdpr"]),
        ("code-python",  ["def ","import ","python",".py","async def"]),
        ("code-typescript",["typescript",".ts","interface ","tsconfig"]),
        ("math",         ["theorem","lemma","integral","derivative","equation"]),
        ("reasoning",    ["chain-of-thought","step by step","let me think"]),
    ]
    best, best_n = "general", 0
    for dom, kws in rules:
        n = sum(1 for k in kws if k in text)
        if n > best_n:
            best, best_n = dom, n
    return best if best_n >= 2 else "general"

n_in = n_kept = n_drop = 0
domains = {}
with open(os.environ["F_IN"]) as fin, open(os.environ["F_OUT"], "w") as fout:
    for line in fin:
        n_in += 1
        try: d = json.loads(line)
        except Exception: continue

        # Normalize fields
        prompt = d.get("prompt") or d.get("instruction") or d.get("question") or ""
        response = d.get("response") or d.get("answer") or d.get("output") or ""
        source = d.get("source") or d.get("dataset") or "unknown"

        # Re-sanitize (in case original mirror missed some patterns)
        v = filter_pair(prompt, response)
        if not v["keep"]:
            n_drop += 1
            continue

        # Re-dedup against central store
        if HAS_DEDUP and not DedupStore.is_new(prompt, source=f"enrich-{source}"):
            n_drop += 1
            continue

        # Domain tag
        domain = detect_domain(prompt, response)
        domains[domain] = domains.get(domain, 0) + 1

        # Token estimate
        tokens_est = (len(prompt) + len(response)) // 4

        out_row = {
            "prompt": prompt,
            "response": response,
            "source": source,
            "meta": {
                "domain": domain,
                "tokens_est": tokens_est,
                "len_prompt": len(prompt),
                "len_response": len(response),
                "enriched_at": int(__import__("time").time()),
            },
        }
        fout.write(json.dumps(out_row, ensure_ascii=False) + "\n")
        n_kept += 1

print(f"  in={n_in} kept={n_kept} drop={n_drop}", file=sys.stderr)
print(f"  domains: {sorted(domains.items(), key=lambda x: -x[1])[:8]}",
      file=sys.stderr)
PYEOF
    n_total=$((n_total + 1))
done

echo "[$(date +%H:%M:%S)] enrich done — $n_total file(s)" | tee -a "$LOG"

# Push enriched files to HF dataset repo every 5 batches
if (( n_total > 0 && n_total % 5 == 0 )); then
    bash "$HOME/.surrogate/bin/push-training-to-hf.sh" >> "$LOG" 2>&1 || true
fi

# Discord notify
if [[ -n "${DISCORD_WEBHOOK:-}" && $n_total -gt 3 ]]; then
    curl -s -X POST -H "Content-Type: application/json" \
        -d "{\"content\":\"🧪 enrich-pipeline: enriched ${n_total} bulk-mirror files this tick\"}" \
        "$DISCORD_WEBHOOK" >/dev/null 2>&1 || true
fi
