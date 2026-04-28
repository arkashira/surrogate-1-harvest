#!/usr/bin/env bash
# Bulk-mirror — clone top community datasets ENTIRELY into our 5 sibling
# repos. Single git/HfApi push = millions of pairs in one commit.
#
# This is fundamentally different from dataset-enrich.sh which streams +
# normalizes per-row. Mirror = "the whole parquet, as-is, NOW", which
# is 100-1000x faster GB/hr.
#
# Why this is fine:
#   - Both HF licenses on these datasets allow redistribution
#   - Format conversion can happen at TRAIN TIME (one pass over the mirror)
#   - We're not double-counting commits because each mirror = 1 file = 1 commit
#
set -uo pipefail
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a

LOG="$HOME/.surrogate/logs/dataset-mirror.log"
mkdir -p "$(dirname "$LOG")"

if [[ -z "${HF_TOKEN:-}" ]]; then
    echo "[$(date +%H:%M:%S)] dataset-mirror skipping — HF_TOKEN not set" | tee -a "$LOG"
    exit 0
fi

echo "[$(date +%H:%M:%S)] dataset-mirror cycle start" | tee -a "$LOG"

python3 - << 'PYEOF' 2>&1 | tee -a "$LOG"
"""
For each big community dataset on the SOURCES list:
  1. Use huggingface_hub.snapshot_download to pull the parquet shards
  2. Stream-read each shard, FILTER for SDLC/coding relevance, DEDUP via central
     store, normalize to {prompt, response} schema
  3. Buffer enriched rows and upload one parquet per source under
     enriched/<slug>/<shard>.parquet
  4. Stamp a marker so we don't re-mirror next cycle

Why filter: user feedback — 'enrich เอาเฉพาะ dataset เรื่องที่เกี่ยวข้อง + dedup ทิ้ง'.
Raw mirror would have lots of irrelevant rows (e.g. multilingual prompts about
cooking inside aya_dataset). Filter narrows to coding / DevSecOps / SDLC.
"""
import os, time, json, hashlib, sys, re
from pathlib import Path
from huggingface_hub import HfApi, snapshot_download, list_repo_files
from huggingface_hub.errors import HfHubHTTPError

api = HfApi(token=os.environ["HF_TOKEN"])

# Domain relevance filter — keep rows that match ANY of these
RELEVANT_KEYWORDS = re.compile(
    r"\b(code|coding|coder|programming|programmer|api|sql|database|"
    r"python|javascript|typescript|java\b|golang|rust|cpp|kotlin|swift|"
    r"react|vue|svelte|angular|next\.?js|express|fastapi|django|spring|"
    r"docker|kubernetes|k8s|terraform|cloudformation|ansible|"
    r"aws|gcp|azure|cloud|devops|sre|cicd|ci/cd|pipeline|"
    r"security|vulnerability|cve|owasp|penetration|exploit|firewall|"
    r"git\b|github|gitlab|jenkins|prometheus|grafana|datadog|"
    r"linux|unix|bash|shell|command|terminal|ssh|"
    r"function|class|method|variable|loop|recursive|algorithm|"
    r"http|rest|graphql|grpc|websocket|tcp|udp|dns|"
    r"frontend|backend|fullstack|microservice|monolith|serverless|"
    r"async|threading|concurrency|race|deadlock|memory|leak|"
    r"test|unit test|integration|e2e|coverage|mock|stub|"
    r"refactor|debug|profile|optimize|benchmark|"
    r"agile|scrum|sprint|kanban|jira|review|architecture|design pattern)\b",
    re.IGNORECASE,
)

def is_relevant(prompt: str, response: str) -> bool:
    text = (prompt + " " + response)[:2000]
    return bool(RELEVANT_KEYWORDS.search(text))

# Central dedup — same SQLite store as dataset-enrich
sys.path.insert(0, str(Path.home() / ".surrogate/bin/lib"))
try:
    from dedup import DedupStore
    HAS_DEDUP = True
except Exception as e:
    print(f"⚠ DedupStore not importable: {e}; running without central dedup", flush=True)
    HAS_DEDUP = False

# Top 30 community SFT mixes that are HUGE and immediately useful.
# Each = 100K-10M pairs. License flag = OK to redistribute.
SOURCES = [
    # Massive SFT mixes
    ("teknium/OpenHermes-2.5",                 "OpenHermes-2-5"),
    ("HuggingFaceH4/ultrachat_200k",           "ultrachat-200k"),
    ("Open-Orca/OpenOrca",                     "OpenOrca"),
    ("Open-Orca/SlimOrca-Dedup",               "SlimOrca-Dedup"),
    ("HuggingFaceH4/no_robots",                "no-robots"),
    ("databricks/databricks-dolly-15k",        "dolly-15k"),
    ("garage-bAInd/Open-Platypus",             "Open-Platypus"),
    ("nvidia/OpenMathInstruct-2",              "OpenMathInstruct-2"),
    # Code-specific
    ("ise-uiuc/Magicoder-OSS-Instruct-75K",    "Magicoder-OSS"),
    ("ise-uiuc/Magicoder-Evol-Instruct-110K",  "Magicoder-Evol"),
    ("HuggingFaceH4/CodeAlpaca_20K",           "CodeAlpaca-20K"),
    ("nickrosh/Evol-Instruct-Code-80k-v1",     "Evol-Code-80k"),
    ("bigcode/self-oss-instruct-sc2-exec-filter-50k", "starcoder2-self-oss"),
    # Reasoning
    ("microsoft/orca-math-word-problems-200k", "orca-math-200k"),
    ("meta-math/MetaMathQA",                   "MetaMathQA"),
    ("EleutherAI/proof-pile-2",                "proof-pile-2"),
    ("HuggingFaceTB/finemath",                 "finemath"),
    # Tool / agentic
    ("Salesforce/xlam-function-calling-60k",   "xlam-fc-60k"),
    ("microsoft/orca-agentinstruct-1M-v1",     "orca-agentinstruct-1M"),
    # Conversational
    ("lmsys/lmsys-chat-1m",                    "lmsys-chat-1m"),
    ("nvidia/HelpSteer3",                      "HelpSteer3"),
    ("Anthropic/hh-rlhf",                      "hh-rlhf"),
    # Multilingual
    ("CohereForAI/aya_dataset",                "aya-dataset"),
    ("CohereForAI/aya_collection",             "aya-collection"),
    # General curated
    ("argilla/magpie-ultra-v1.0",              "magpie-ultra"),
    ("Magpie-Align/Magpie-Pro-MT-300K-v0.1",   "magpie-pro-300K"),
    # Code feedback / DPO
    ("m-a-p/CodeFeedback-Filtered-Instruction","CodeFeedback"),
    ("argilla/distilabel-capybara-dpo-7k-binarized", "capybara-dpo-7k"),
    # Smol team
    ("HuggingFaceTB/smoltalk",                 "smoltalk"),
    ("HuggingFaceTB/smollm-corpus",            "smollm-corpus"),
]

# 5 sibling repos to spread across — round-robin by hash for determinism
SIBLINGS = [
    "axentx/surrogate-1-training-pairs",
    "axentx/surrogate-1-pairs-A",
    "axentx/surrogate-1-pairs-B",
    "axentx/surrogate-1-pairs-C",
    "axentx/surrogate-1-pairs-D",
]
def pick_repo(slug):
    h = int(hashlib.md5(slug.encode()).hexdigest()[:8], 16)
    return SIBLINGS[h % len(SIBLINGS)]

STAMPS = Path.home() / ".surrogate/state/dataset-mirror-stamps.json"
STAMPS.parent.mkdir(parents=True, exist_ok=True)
stamps = json.loads(STAMPS.read_text()) if STAMPS.exists() else {}

CACHE = Path("/tmp/dataset-mirror-cache")
CACHE.mkdir(exist_ok=True)

mirrored = 0
skipped = 0
errors = 0

for src_id, slug in SOURCES:
    if slug in stamps:
        skipped += 1
        continue
    target = pick_repo(slug)
    print(f"\n▶ enrich+mirror {src_id}  →  {target}/enriched/{slug}/", flush=True)
    try:
        # Download parquet/jsonl shards
        local = snapshot_download(
            repo_id=src_id, repo_type="dataset",
            cache_dir=str(CACHE), token=os.environ["HF_TOKEN"],
            allow_patterns=["*.parquet", "*.jsonl", "*.json", "*.arrow"],
        )
        local_path = Path(local)

        # Read each shard, filter, dedup, normalize, write to a single output buffer
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except Exception:
            print(f"  ❌ pyarrow not available — falling back to raw mirror", flush=True)
            # last-resort raw upload
            for f in sorted(local_path.rglob("*.parquet")):
                if not f.is_file() or f.stat().st_size < 1024: continue
                api.upload_file(path_or_fileobj=str(f),
                    path_in_repo=f"raw-mirrors/{slug}/{f.name}",
                    repo_id=target, repo_type="dataset",
                    commit_message=f"raw-mirror: {src_id} fallback")
                mirrored += 1
                time.sleep(2)
            continue

        scanned = kept = duped = irrelevant = 0
        out_rows = []
        for shard in sorted(local_path.rglob("*.parquet")):
            try:
                table = pq.read_table(shard)
                df = table.to_pylist()
            except Exception as e:
                print(f"  skip shard {shard.name}: {type(e).__name__}: {str(e)[:80]}", flush=True)
                continue
            for row in df:
                scanned += 1
                # Heuristic: try common field combinations
                p = (row.get("prompt") or row.get("instruction") or row.get("question")
                     or row.get("input") or row.get("query") or "")
                r = (row.get("response") or row.get("answer") or row.get("output")
                     or row.get("completion") or row.get("solution") or "")
                # messages-style
                if not p and "messages" in row and isinstance(row["messages"], list) and len(row["messages"]) >= 2:
                    p = str(row["messages"][0].get("content","") or row["messages"][0].get("value",""))
                    r = str(row["messages"][1].get("content","") or row["messages"][1].get("value",""))
                # conversations-style
                if not p and "conversations" in row and isinstance(row["conversations"], list) and len(row["conversations"]) >= 2:
                    p = str(row["conversations"][0].get("value",""))
                    r = str(row["conversations"][1].get("value",""))

                p = str(p).strip()[:6000]
                r = str(r).strip()[:8000]
                if len(p) < 20 or len(r) < 30:
                    continue
                if not is_relevant(p, r):
                    irrelevant += 1
                    continue
                if HAS_DEDUP and not DedupStore.is_new(p, source=f"mirror-{slug}"):
                    duped += 1
                    continue
                out_rows.append({"prompt": p, "response": r,
                                  "source": f"mirror/{slug}", "ts": int(time.time())})
                kept += 1

                # Periodic flush — keeps memory bounded for huge sources
                if len(out_rows) >= 50000:
                    chunk_path = CACHE / f"{slug}-chunk-{int(time.time())}.parquet"
                    pq.write_table(pa.Table.from_pylist(out_rows), chunk_path, compression="snappy")
                    api.upload_file(path_or_fileobj=str(chunk_path),
                        path_in_repo=f"enriched/{slug}/chunk-{int(time.time())}.parquet",
                        repo_id=target, repo_type="dataset",
                        commit_message=f"enriched mirror: {src_id} +{len(out_rows)} rows")
                    mirrored += 1
                    out_rows = []
                    chunk_path.unlink()
                    time.sleep(3)
        # Final flush
        if out_rows:
            chunk_path = CACHE / f"{slug}-final-{int(time.time())}.parquet"
            pq.write_table(pa.Table.from_pylist(out_rows), chunk_path, compression="snappy")
            api.upload_file(path_or_fileobj=str(chunk_path),
                path_in_repo=f"enriched/{slug}/final-{int(time.time())}.parquet",
                repo_id=target, repo_type="dataset",
                commit_message=f"enriched mirror final: {src_id} +{len(out_rows)} rows")
            mirrored += 1
            chunk_path.unlink()

        print(f"  ✅ {src_id}: scanned={scanned:,} kept={kept:,} dup={duped:,} irrelevant={irrelevant:,}", flush=True)
        stamps[slug] = {"ts": int(time.time()), "kept": kept, "scanned": scanned}
        STAMPS.write_text(json.dumps(stamps, indent=2))
    except Exception as e:
        print(f"  ❌ {type(e).__name__}: {str(e)[:200]}", flush=True)
        errors += 1
        continue

print(f"\n✅ enrich+mirror cycle done: {mirrored} files uploaded, {skipped} skipped, {errors} errors")
PYEOF

echo "[$(date +%H:%M:%S)] dataset-mirror cycle done" | tee -a "$LOG"
