#!/usr/bin/env python3
"""Bootstrap a reward model from accumulated verdict triples.

Reads training-shards/verdict.jsonl (proposal+reject_reason→refined),
formats as (chosen, rejected) pairs for DPO/RM training, pushes to HF
dataset axentx/surrogate-1-rm-bootstrap. The trainer (V20+) can fine-tune
a small reward head on this for offline scoring of future agent outputs."""
from __future__ import annotations
import datetime, json, os
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT","/opt/surrogate-1-harvest"))
SHARD = REPO_ROOT / "state" / "training-shards" / "verdict.jsonl"
OUT = REPO_ROOT / "state" / "training-shards" / "rm-bootstrap.jsonl"

def main():
    if not SHARD.exists():
        print("no verdict shard yet"); return
    n = 0
    with OUT.open("w") as out:
        for line in SHARD.read_text().splitlines():
            try: r = json.loads(line)
            except: continue
            chosen = r.get("response","")
            rejected = ""
            if "previous attempt was rejected" in r.get("prompt",""):
                # extract the rejected attempt from the prompt body
                p = r["prompt"]
                if "===" in p:
                    parts = p.split("===")
                    for i, part in enumerate(parts):
                        if "proposed this change" in part.lower() or i == 1:
                            rejected = part.strip()[:3000]; break
            if not (chosen and rejected): continue
            out.write(json.dumps({"prompt": "<see verdict triple>",
                                  "chosen": chosen[:3000],
                                  "rejected": rejected,
                                  "source": "verdict-triple"}) + "\n")
            n += 1
    print(f"wrote {n} RM pairs → {OUT}")

if __name__ == "__main__":
    main()
