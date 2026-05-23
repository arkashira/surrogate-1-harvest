"""Push Surrogate-1 v2 datasets to HF Hub.

Two invocation modes:

A) Bulk dir mode (used by anchor cron-loop.sh M%30==4):
     python3 push-to-hub.py --src /data/v2/enriched \
                            --repo axentx/surrogate-1-training-pairs
   Globs *.jsonl in --src, uploads each as
   `enriched/<YYYY-MM-DD>/<basename>` to --repo. Idempotent on filename
   (HF replaces same-path on repush).

   This is how the cron picks up enrich-pipeline.sh output and lands it
   on the Hub. Files have richer schema than dataset-enrich.sh's bulk
   batches: {prompt, response, source, meta:{domain, tokens_est, ...}}.

B) Legacy curated mode (used by build-data-pipeline.sh phase=all):
     python3 push-to-hub.py
   Reads ~/.surrogate/data/v2-clean/v2-{sft,tools,agent,dpo}/clean.jsonl,
   converts {prompt,response} → chat_template `messages`, pushes to:
       axentx/surrogate-1-v2-train  (SFT)
       axentx/surrogate-1-v2-tools  (Stage 1.5)
       axentx/surrogate-1-v2-agent  (Stage 1.6)
       axentx/surrogate-1-v2-dpo    (Stage 2)
   Skips silently if v2-clean/* missing (build-data-pipeline.sh hasn't
   run yet).
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
from pathlib import Path

from huggingface_hub import HfApi, create_repo


PUSH_MAP = {
    "v2-sft":   "axentx/surrogate-1-v2-train",
    "v2-tools": "axentx/surrogate-1-v2-tools",
    "v2-agent": "axentx/surrogate-1-v2-agent",
    "v2-dpo":   "axentx/surrogate-1-v2-dpo",
}


def _hf_token() -> str | None:
    return (os.environ.get("HF_TOKEN")
            or os.environ.get("HUGGING_FACE_HUB_TOKEN")
            or os.environ.get("HUGGINGFACE_TOKEN"))


def push_bulk_dir(src: Path, repo_id: str, prefix: str = "enriched") -> int:
    """Mode A: glob src/*.jsonl → repo_id/{prefix}/{date}/{basename}."""
    token = _hf_token()
    api = HfApi(token=token)
    try:
        create_repo(repo_id, repo_type="dataset", private=False,
                    exist_ok=True, token=token)
    except Exception as e:
        print(f"  create_repo {repo_id} warn: {type(e).__name__}: {e}",
              file=sys.stderr)

    files = sorted(src.glob("*.jsonl"))
    if not files:
        print(f"  no jsonl in {src} — nothing to push")
        return 0

    date_tag = time.strftime("%Y-%m-%d")
    pushed = 0
    for fp in files:
        if fp.stat().st_size < 1024:
            continue  # skip empty/near-empty
        remote = f"{prefix}/{date_tag}/{fp.name}"
        try:
            api.upload_file(
                path_or_fileobj=str(fp),
                path_in_repo=remote,
                repo_id=repo_id,
                repo_type="dataset",
                commit_message=(f"enrich-pipeline: +{fp.name} "
                                f"({fp.stat().st_size//1024} KB)"),
            )
            print(f"  ✅ {fp.name} → {repo_id}:{remote}")
            pushed += 1
        except Exception as e:
            # Don't crash the cron — log and continue with next file
            print(f"  ❌ {fp.name}: {type(e).__name__}: {str(e)[:200]}",
                  file=sys.stderr)
    print(f"  pushed {pushed}/{len(files)} files → {repo_id}")
    return pushed


def push_curated_v2_clean() -> None:
    """Mode B: legacy v2-clean/v2-{sft,tools,agent,dpo}/clean.jsonl."""
    token = _hf_token()
    api = HfApi(token=token)
    data_root = Path.home() / ".surrogate/data/v2-clean"

    for category, repo_id in PUSH_MAP.items():
        src = data_root / category / "clean.jsonl"
        if not src.exists():
            print(f"⚠ skip {category}: {src} missing — "
                  f"build-data-pipeline.sh likely hasn't run")
            continue

        try:
            create_repo(repo_id, repo_type="dataset", private=True,
                        exist_ok=True, token=token)
        except Exception as e:
            print(f"  create_repo {repo_id} err: {e}")

        # Convert {prompt,response} → chat_template messages
        out_path = src.parent / "chat_template.jsonl"
        with open(src) as fin, open(out_path, "w") as fout:
            for line in fin:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                messages = [
                    {"role": "user", "content": obj.get("prompt", "")},
                    {"role": "assistant", "content": obj.get("response", "")},
                ]
                fout.write(json.dumps({"messages": messages},
                                      ensure_ascii=False) + "\n")

        try:
            api.upload_file(
                path_or_fileobj=str(out_path),
                path_in_repo="train.jsonl",
                repo_id=repo_id,
                repo_type="dataset",
                commit_message=(f"v2 build: {category} clean+sanitized+"
                                f"deduped+decontaminated"),
            )
            print(f"✅ pushed {category} → {repo_id}")
        except Exception as e:
            print(f"❌ push {repo_id} failed: {e}")

    print("\n✅ all curated datasets pushed (legacy mode)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path,
                    help="Directory to glob *.jsonl from (mode A)")
    ap.add_argument("--repo", type=str,
                    help="HF dataset repo_id to push to (mode A)")
    ap.add_argument("--prefix", type=str, default="enriched",
                    help="Subpath inside repo (mode A, default: enriched)")
    args = ap.parse_args()

    if args.src and args.repo:
        if not args.src.exists():
            print(f"  src {args.src} missing — nothing to push")
            return 0
        push_bulk_dir(args.src, args.repo, args.prefix)
        return 0

    if args.src or args.repo:
        print("ERROR: --src and --repo must be provided together",
              file=sys.stderr)
        return 2

    push_curated_v2_clean()
    return 0


if __name__ == "__main__":
    sys.exit(main())
