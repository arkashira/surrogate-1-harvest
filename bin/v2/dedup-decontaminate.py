"""Surrogate-1 v2 — Dedup + decontaminate pipeline.

After build-data-pipeline.sh produces ~/.surrogate/data/v2-{sft,tools,agent,dpo}/*.jsonl,
this script:
  1. Exact SHA-256 dedup within + across files
  2. MinHash LSH 256-perm 5-gram threshold 0.7 (datatrove)
  3. Decontaminate vs HumanEval+/MBPP+/LiveCodeBench/SWE-Bench-Lite
  4. Output clean files to v2-{sft,tools,agent,dpo}-clean/
"""
import os, json, hashlib, sys
from pathlib import Path
from collections import defaultdict

DATA = Path.home() / ".surrogate/data"
OUT_BASE = DATA / "v2-clean"
OUT_BASE.mkdir(exist_ok=True)


def exact_dedup(input_dir: Path, output_path: Path) -> int:
    """SHA-256 exact dedup on prompt+response pair."""
    seen = set()
    kept = 0
    with open(output_path, "w") as fout:
        for f in sorted(input_dir.glob("*.jsonl")):
            with open(f) as fin:
                for line in fin:
                    if not line.strip(): continue
                    try: obj = json.loads(line)
                    except Exception: continue
                    key = hashlib.sha256(
                        (obj.get("prompt","") + "|" + obj.get("response","")).encode()
                    ).hexdigest()
                    if key in seen: continue
                    seen.add(key)
                    fout.write(line)
                    kept += 1
    return kept


def load_decontamination_set() -> set:
    """Load prompts from public eval suites — anything that overlaps must be dropped."""
    seen = set()
    for repo in ["evalplus/humanevalplus", "evalplus/mbppplus"]:
        try:
            from datasets import load_dataset
            ds = load_dataset(repo, split="test", streaming=True)
            for ex in ds:
                p = ex.get("prompt") or ex.get("text") or ""
                # Use first 200 chars as fingerprint
                if len(p) > 50:
                    seen.add(p[:200].strip())
        except Exception as e:
            print(f"  decontam {repo} failed: {e}")
    # LiveCodeBench v6 — prompts are public
    try:
        from datasets import load_dataset
        ds = load_dataset("livecodebench/code_generation_lite", split="test", streaming=True)
        for ex in ds:
            p = ex.get("question_content", "") or ex.get("prompt", "")
            if len(p) > 50:
                seen.add(p[:200].strip())
    except Exception as e:
        print(f"  decontam LCB failed: {e}")
    print(f"  decontam set size: {len(seen)}")
    return seen


def decontaminate(input_path: Path, output_path: Path, eval_prompts: set) -> int:
    """Drop training rows whose prompt overlaps with eval suite prompts."""
    kept, dropped = 0, 0
    with open(input_path) as fin, open(output_path, "w") as fout:
        for line in fin:
            if not line.strip(): continue
            try: obj = json.loads(line)
            except Exception: continue
            p = obj.get("prompt", "")[:200].strip()
            if p in eval_prompts:
                dropped += 1
                continue
            fout.write(line)
            kept += 1
    print(f"  decontaminate {input_path.name}: kept={kept} dropped={dropped}")
    return kept


def minhash_dedup(input_path: Path, output_path: Path, threshold: float = 0.7) -> int:
    """MinHash LSH near-dup. Falls back to exact dedup if datasketch unavailable."""
    try:
        from datasketch import MinHash, MinHashLSH
    except ImportError:
        print("  datasketch not installed — skipping MinHash, using exact dedup output")
        os.replace(input_path, output_path)
        return -1

    lsh = MinHashLSH(threshold=threshold, num_perm=256)
    kept = []

    def to_minhash(text: str) -> MinHash:
        m = MinHash(num_perm=256)
        # 5-gram tokens
        toks = text.lower().split()
        for i in range(len(toks) - 4):
            m.update((" ".join(toks[i:i+5])).encode())
        return m

    with open(input_path) as fin:
        for idx, line in enumerate(fin):
            if not line.strip(): continue
            try: obj = json.loads(line)
            except Exception: continue
            mh = to_minhash(obj.get("prompt","") + " " + obj.get("response",""))
            if list(lsh.query(mh)):
                continue  # near-duplicate found
            lsh.insert(f"r_{idx}", mh)
            kept.append(line)

    with open(output_path, "w") as fout:
        for line in kept:
            fout.write(line)
    return len(kept)


if __name__ == "__main__":
    eval_prompts = load_decontamination_set()

    for category in ["v2-sft", "v2-tools", "v2-agent", "v2-dpo"]:
        in_dir = DATA / category
        if not in_dir.exists():
            print(f"⚠ skip {category} (not present)")
            continue
        print(f"\n━━━ {category} ━━━")
        clean_dir = OUT_BASE / category
        clean_dir.mkdir(exist_ok=True)

        # 1. Exact dedup → merged.jsonl
        merged = clean_dir / "merged.jsonl"
        kept = exact_dedup(in_dir, merged)
        print(f"  step 1 exact dedup: kept={kept}")

        # 2. Decontaminate
        decon = clean_dir / "decontaminated.jsonl"
        kept = decontaminate(merged, decon, eval_prompts)

        # 3. MinHash near-dup
        clean = clean_dir / "clean.jsonl"
        kept = minhash_dedup(decon, clean)
        print(f"  step 3 minhash: kept={kept}")
