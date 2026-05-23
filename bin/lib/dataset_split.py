"""Deterministic train/val/test splitter for training pairs.

Random splits leak test → train across resumes; this hashes each row's
fingerprint to bucket it once and forever. As long as the fingerprint
is stable (same prompt + response) the row lands in the same split,
which means re-runs of the trainer never accidentally promote a test
row into the train set.

Buckets: 80 train / 10 val / 10 test (default). Configurable via
`split(..., ratios=(80, 10, 10))`. Ratios must sum to 100.

Fingerprint: md5(prompt + "|" + response/chosen + "|" + flavor)[:16].
We accept either flat dicts ({"prompt", "response"}) or DPO triples
({"prompt", "chosen", "rejected"}). Flavor is included so the same
prompt with different completion targets does not collide.
"""
from __future__ import annotations

import hashlib
from typing import Iterable, Sequence

DEFAULT_RATIOS = (80, 10, 10)  # train, val, test
SEED = 42


def _row_fingerprint(row: dict) -> str:
    flavor = str(row.get("flavor", "sft"))
    prompt = str(row.get("prompt", ""))
    target = (
        row.get("response")
        or row.get("chosen")
        or row.get("refined_proposal")
        or ""
    )
    payload = f"{flavor}|{prompt}|{target}".encode("utf-8", errors="replace")
    return hashlib.md5(payload).hexdigest()[:16]


def _bucket(fp: str, ratios: Sequence[int], seed: int) -> str:
    salt = f"{seed}:{fp}".encode("utf-8")
    n = int(hashlib.md5(salt).hexdigest()[:8], 16) % 100
    train_cut = ratios[0]
    val_cut = ratios[0] + ratios[1]
    if n < train_cut:
        return "train"
    if n < val_cut:
        return "val"
    return "test"


def split(
    rows: Iterable[dict],
    seed: int = SEED,
    ratios: Sequence[int] = DEFAULT_RATIOS,
) -> dict[str, list[dict]]:
    """Return {'train': [...], 'val': [...], 'test': [...]} buckets.

    Deterministic: identical input + seed always yields identical buckets.
    Every row is augmented with `_split` so downstream consumers can keep
    the assignment after concatenation.
    """
    if sum(ratios) != 100:
        raise ValueError(f"ratios must sum to 100, got {ratios} sum={sum(ratios)}")

    buckets: dict[str, list[dict]] = {"train": [], "val": [], "test": []}
    for row in rows:
        fp = _row_fingerprint(row)
        which = _bucket(fp, ratios, seed)
        # Annotate row in-place (caller often reuses these dicts).
        row["_split"] = which
        row["_fingerprint"] = fp
        buckets[which].append(row)
    return buckets


def assign(row: dict, seed: int = SEED, ratios: Sequence[int] = DEFAULT_RATIOS) -> str:
    """Single-row helper — return 'train' | 'val' | 'test'."""
    return _bucket(_row_fingerprint(row), ratios, seed)


if __name__ == "__main__":
    # Smoke test: 1000 fake rows, assert ~80/10/10 within tolerance.
    fakes = [
        {"flavor": "sft", "prompt": f"q-{i}", "response": f"a-{i}"}
        for i in range(1000)
    ]
    out = split(fakes)
    sizes = {k: len(v) for k, v in out.items()}
    print(f"sizes: {sizes}")
    assert 750 <= sizes["train"] <= 850, f"train out of band: {sizes['train']}"
    assert 70 <= sizes["val"] <= 130, f"val out of band: {sizes['val']}"
    assert 70 <= sizes["test"] <= 130, f"test out of band: {sizes['test']}"
    # Determinism check: split twice, same buckets.
    out2 = split([dict(r) for r in fakes])
    sizes2 = {k: len(v) for k, v in out2.items()}
    assert sizes == sizes2, "non-deterministic"
    print("OK (deterministic, balanced)")
