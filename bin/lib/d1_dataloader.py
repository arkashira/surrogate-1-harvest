"""Streaming dataloader for D1-backed training pairs.

Paginates through `axentx/surrogate-1-training-pairs` via the cursor
worker (CF Worker fronting D1). Yields one pair at a time so callers
never hold the full dataset in memory.

Cursor service contract (already deployed):
  GET  {CURSOR_SERVICE_URL}/cursor/peek?dataset=axentx/surrogate-1-training-pairs&page_size=N
       returns {"rows": [...], "next_cursor": "<opaque>", "page_size": N}
  POST {CURSOR_SERVICE_URL}/cursor/advance
       body: {"dataset": "...", "cursor": "<opaque>", "page_size": N}
       returns next page; idempotent for the (dataset, cursor) pair.

Auth: X-Auth-Token header from CURSOR_AUTH_TOKEN.

Usage (plain Python loop):
    from lib.d1_dataloader import D1Dataloader
    loader = D1Dataloader(dataset="axentx/surrogate-1-training-pairs")
    for row in loader:
        train_step(row)

Usage (Hugging Face datasets wrapper):
    from datasets import IterableDataset
    from lib.d1_dataloader import D1Dataloader, as_hf_iterable_dataset
    ds = as_hf_iterable_dataset(D1Dataloader(...))
    # Now usable with `Trainer(train_dataset=ds, ...)`
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Iterator

DEFAULT_DATASET = "axentx/surrogate-1-training-pairs"
DEFAULT_PAGE_SIZE = 256
DEFAULT_TIMEOUT_S = 30
DEFAULT_RETRIES = 4
DEFAULT_BACKOFF_S = 2.0


class CursorServiceError(RuntimeError):
    """Raised when the cursor worker returns a non-2xx after retries."""


class D1Dataloader:
    """Iterate rows from the cursor service one at a time.

    Resumable via `cursor` argument — pass the value persisted from
    `loader.last_cursor` to resume from the same position.
    """

    def __init__(
        self,
        dataset: str = DEFAULT_DATASET,
        page_size: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
        max_rows: int | None = None,
        service_url: str | None = None,
        auth_token: str | None = None,
    ) -> None:
        self.dataset = dataset
        self.page_size = page_size
        self._cursor = cursor
        self._max_rows = max_rows
        self._yielded = 0
        self.service_url = (
            service_url
            or os.environ.get("CURSOR_SERVICE_URL")
            or "https://cursor.axentx.workers.dev"
        ).rstrip("/")
        self.auth_token = auth_token or os.environ.get("CURSOR_AUTH_TOKEN", "")
        if not self.auth_token:
            raise RuntimeError("CURSOR_AUTH_TOKEN missing — required for D1Dataloader")

    @property
    def last_cursor(self) -> str | None:
        """Resumable token — persist this if you need to restart mid-epoch."""
        return self._cursor

    def __iter__(self) -> Iterator[dict]:
        return self._iterate()

    def _iterate(self) -> Iterator[dict]:
        while True:
            page = self._fetch_page()
            rows = page.get("rows") or []
            if not rows:
                return
            for row in rows:
                yield row
                self._yielded += 1
                if self._max_rows is not None and self._yielded >= self._max_rows:
                    return
            next_cursor = page.get("next_cursor")
            if not next_cursor or next_cursor == self._cursor:
                return
            self._cursor = next_cursor

    def _fetch_page(self) -> dict:
        last_err: Exception | None = None
        for attempt in range(DEFAULT_RETRIES):
            try:
                if self._cursor is None:
                    return self._call(
                        "GET",
                        f"/cursor/peek?dataset={self.dataset}&page_size={self.page_size}",
                    )
                return self._call(
                    "POST",
                    "/cursor/advance",
                    body={
                        "dataset": self.dataset,
                        "cursor": self._cursor,
                        "page_size": self.page_size,
                    },
                )
            except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError) as exc:
                last_err = exc
                time.sleep(DEFAULT_BACKOFF_S * (2 ** attempt))
        raise CursorServiceError(f"cursor service failed after retries: {last_err}")

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{self.service_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "X-Auth-Token": self.auth_token,
            "Content-Type": "application/json",
            "User-Agent": "surrogate-1-d1-dataloader/1.0",
        }
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT_S) as resp:
            payload = resp.read().decode("utf-8")
        return json.loads(payload)


def as_hf_iterable_dataset(loader: D1Dataloader):
    """Wrap a D1Dataloader as a Hugging Face IterableDataset.

    Lazy import so this module is usable in environments where
    `datasets` is not installed (e.g. during PII scrubbing CI).
    """
    from datasets import IterableDataset  # type: ignore

    def _gen():
        yield from loader

    return IterableDataset.from_generator(_gen)


if __name__ == "__main__":
    # Smoke test: tiny page_size, max_rows=3, print without crashing.
    loader = D1Dataloader(page_size=4, max_rows=3)
    n = 0
    try:
        for row in loader:
            print(json.dumps({k: v for k, v in row.items() if k in ("flavor", "id")}))
            n += 1
    except CursorServiceError as exc:
        print(f"smoke skipped (service unreachable): {exc}")
    print(f"yielded {n} rows; resume cursor={loader.last_cursor}")
