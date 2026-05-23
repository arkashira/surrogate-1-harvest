#!/usr/bin/env bash
# Daily scrape of public SRE postmortem corpus → training pairs.
# Sources:
#   1. github.com/danluu/post-mortems (curated list of ~600 incidents)
#   2. github.com/snakescott/awesome-tech-postmortems (~200 entries)
#   3. github.com/dastergon/awesome-sre (curated SRE references)
#
# Strategy: fetch the README markdown, extract incident titles + outbound links,
# fetch a sample of the linked postmortems, generate (incident → root-cause + lessons) pairs.
# Cap: 30 new pairs/day to keep cost low. Sliding offset so we don't re-process.
set -uo pipefail
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a

LOG="$HOME/.surrogate/logs/scrape-sre-postmortems.log"
PAIRS="$HOME/.surrogate/training-pairs.jsonl"
SEEN="$HOME/.surrogate/state/postmortems-seen.txt"
mkdir -p "$(dirname "$LOG")" "$(dirname "$SEEN")"
touch "$SEEN"

echo "[$(date +%H:%M:%S)] SRE postmortem scrape start" | tee -a "$LOG"

python3 - "$PAIRS" "$SEEN" >> "$LOG" 2>&1 <<'PYEOF'
import sys, json, urllib.request, urllib.parse, re, time, os
from datetime import datetime
sys.path.insert(0, os.path.expanduser("~/.surrogate/bin/lib"))
try:
    from dedup import DedupStore
    HAS_DEDUP = True
except ImportError:
    HAS_DEDUP = False

pairs_path, seen_path = sys.argv[1], sys.argv[2]

# Load already-seen URLs
seen = set()
if os.path.exists(seen_path):
    with open(seen_path) as f:
        seen = {l.strip() for l in f if l.strip()}

# Use HF Inference Provider router for summarization (cheap, free)
hf_token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")

def summarize(title: str, raw_text: str) -> str:
    """Summarize a postmortem into root cause + lessons via LLM."""
    if not hf_token:
        return f"[Postmortem: {title}]\n\n{raw_text[:1500]}"
    body = {
        "model": "meta-llama/Llama-3.3-70B-Instruct",
        "messages": [{"role":"user","content":
            f"Summarize this engineering incident postmortem into:\n"
            f"1. **Incident**: 1 sentence\n"
            f"2. **Impact**: 1 sentence\n"
            f"3. **Root cause**: 1-2 sentences\n"
            f"4. **Lessons learned**: 3-5 bullets, each ≤ 1 sentence\n\n"
            f"Title: {title}\n\nText:\n{raw_text[:6000]}"
        }],
        "temperature": 0.3, "max_tokens": 800,
    }
    try:
        req = urllib.request.Request(
            "https://router.huggingface.co/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type":"application/json","Authorization":f"Bearer {hf_token}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)["choices"][0]["message"]["content"]
    except Exception as e:
        return f"[Postmortem: {title}]\n\n{raw_text[:1500]}\n\n(summary fail: {type(e).__name__})"

# Fetch danluu's postmortem index
sources = [
    "https://raw.githubusercontent.com/danluu/post-mortems/master/README.md",
    "https://raw.githubusercontent.com/snakescott/awesome-tech-postmortems/main/README.md",
]
all_links: list[tuple[str,str]] = []
for src_url in sources:
    try:
        req = urllib.request.Request(src_url, headers={"User-Agent":"Surrogate-1"})
        with urllib.request.urlopen(req, timeout=30) as r:
            md = r.read().decode("utf-8", errors="ignore")
        # Extract markdown links: [title](url)
        for m in re.finditer(r'\[([^\]]+)\]\((https?://[^\s\)]+)\)', md):
            title, url = m.group(1).strip(), m.group(2).strip()
            if "github.com/danluu" in url or "github.com/snakescott" in url:
                continue
            if url in seen: continue
            all_links.append((title, url))
    except Exception as e:
        print(f"  source fail {src_url}: {type(e).__name__}")

print(f"  found {len(all_links)} unseen postmortem links")

# Cap: 30 new pairs/day to avoid blowing rate limits
import random
random.shuffle(all_links)
processed = 0
errors = 0
for title, url in all_links[:50]:
    if processed >= 30: break
    try:
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0 Surrogate-1"})
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read(800_000).decode("utf-8", errors="ignore")
        # Strip HTML
        text = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.S | re.I)
        text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()[:8000]
        if len(text) < 500:
            with open(seen_path, "a") as f: f.write(url + "\n")
            continue
        summary = summarize(title, text)
        if not summary or len(summary) < 200:
            errors += 1
            continue
        prompt = f"Tell me about the {title} incident — what happened, why, and what to learn from it."
        if HAS_DEDUP and not DedupStore.is_new(prompt, source="sre-postmortem"):
            with open(seen_path, "a") as f: f.write(url + "\n")
            continue
        pair = {
            "ts": time.time(),
            "source": "sre-postmortem",
            "url": url, "title": title,
            "prompt": prompt,
            "response": summary,
        }
        with open(pairs_path, "a") as f:
            f.write(json.dumps(pair, ensure_ascii=False) + "\n")
        with open(seen_path, "a") as f:
            f.write(url + "\n")
        processed += 1
        time.sleep(2)  # rate-limit polite
    except Exception as e:
        errors += 1
        with open(seen_path, "a") as f: f.write(url + "\n")
print(f"[done] {processed} new SRE postmortem pairs (errors: {errors})")
PYEOF

echo "[$(date +%H:%M:%S)] SRE postmortem scrape done" | tee -a "$LOG"
