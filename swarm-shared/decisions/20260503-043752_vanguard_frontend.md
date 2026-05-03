# vanguard / frontend

## Final synthesized solution

**Core problem**: No content-addressed, deterministic snapshot per date folder causes HF API 429s, non-reproducible epochs, and silent corruption.  
**Core fix**: Commit a `snapshot.json` per date folder containing `{date, repo, files: [{path, sha256}]}`; add a minimal frontend picker + server-side validation so training uses CDN-only, zero-API, integrity-checked fetches.

---

### 1. Directory and file layout (create)

```bash
mkdir -p /opt/axentx/vanguard/src/frontend/components
mkdir -p /opt/axentx/vanguard/src/frontend/lib
mkdir -p /opt/axentx/vanguard/data/snapshots/2026-04-29
```

- `src/frontend/lib/snapshot.ts` — types, loader, and server-side validator.
- `src/frontend/components/SnapshotPicker.tsx` — date picker + file list UI.
- `data/snapshots/YYYY-MM-DD/snapshot.json` — committed manifest per date.

---

### 2. Types and loader (frontend + server-safe)

`/opt/axentx/vanguard/src/frontend/lib/snapshot.ts`

```ts
export interface SnapshotFile {
  path: string;   // relative to dataset repo root, e.g. "2026-04-29/batch-0001.parquet"
  sha256: string; // lowercase hex
}

export interface Snapshot {
  date: string;   // YYYY-MM-DD
  repo: string;   // e.g. "datasets/myorg/vanguard-mirror"
  files: SnapshotFile[];
}

/**
 * Load snapshot.json for a date (frontend).
 * Uses no-store to avoid stale cached manifests.
 */
export async function loadSnapshot(date: string): Promise<Snapshot | null> {
  try {
    const res = await fetch(`/data/snapshots/${date}/snapshot.json`, { cache: "no-store" });
    if (!res.ok) return null;
    const json = await res.json();
    return json as Snapshot;
  } catch {
    return null;
  }
}

/**
 * Server-side (Node) full integrity validator.
 * Streams each file from CDN and verifies sha256.
 *
 * Returns counts and first failure for actionable logs.
 */
export async function validateSnapshotServerSide(
  snapshot: Snapshot,
  options?: { concurrency?: number; signal?: AbortSignal }
): Promise<{ valid: string[]; invalid: string[]; firstError?: string }> {
  const concurrency = options?.concurrency ?? 4;
  const files = [...snapshot.files];
  const valid: string[] = [];
  const invalid: string[] = [];

  async function validateOne(file: SnapshotFile): Promise<{ path: string; ok: boolean; error?: string }> {
    try {
      const url = `https://huggingface.co/datasets/${snapshot.repo}/resolve/main/${encodeURIComponent(file.path)}`;
      const res = await fetch(url, { signal: options?.signal });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);

      const reader = res.body?.getReader();
      if (!reader) throw new Error("ReadableStream not available");

      const hash = await crypto.subtle.digest("SHA-256", await streamToArrayBuffer(reader));
      const got = Array.from(new Uint8Array(hash))
        .map((b) => b.toString(16).padStart(2, "0"))
        .join("");

      if (got.toLowerCase() !== file.sha256.toLowerCase()) {
        throw new Error(`sha256 mismatch: expected ${file.sha256}, got ${got}`);
      }
      return { path: file.path, ok: true };
    } catch (err: any) {
      return { path: file.path, ok: false, error: err.message ?? String(err) };
    }
  }

  // Simple bounded concurrency
  const workers = Array.from({ length: concurrency }).map(async () => {
    while (files.length > 0) {
      if (options?.signal?.aborted) break;
      const file = files.shift()!;
      const result = await validateOne(file);
      if (result.ok) valid.push(result.path);
      else invalid.push(result.path);
      if (!result.ok && !options.signal?.aborted) {
        return { path: result.path, error: result.error };
      }
    }
    return null;
  });

  const firstError = (await Promise.all(workers)).find(Boolean);
  return { valid, invalid, firstError: firstError?.error };
}

async function streamToArrayBuffer(reader: ReadableStreamDefaultReader<Uint8Array>): Promise<ArrayBuffer> {
  const chunks: Uint8Array[] = [];
  let total = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    total += value.length;
  }
  const out = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    out.set(chunk, offset);
    offset += chunk.length;
  }
  return out.buffer;
}
```

---

### 3. Frontend picker component

`/opt/axentx/vanguard/src/frontend/components/SnapshotPicker.tsx`

```tsx
import React, { useEffect, useState } from "react";
import { loadSnapshot, Snapshot } from "../lib/snapshot";

interface SnapshotPickerProps {
  availableDates: string[];
  onSelectSnapshot?: (snapshot: Snapshot | null) => void;
}

export const SnapshotPicker: React.FC<SnapshotPickerProps> = ({
  availableDates,
  onSelectSnapshot,
}) => {
  const [selectedDate, setSelectedDate] = useState<string>("");
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!selectedDate) {
      setSnapshot(null);
      onSelectSnapshot?.(null);
      return;
    }
    setLoading(true);
    loadSnapshot(selectedDate)
      .then((s) => {
        setSnapshot(s);
        onSelectSnapshot?.(s);
      })
      .finally(() => setLoading(false));
  }, [selectedDate, onSelectSnapshot]);

  return (
    <div style={{ padding: 12, border: "1px solid #ddd", borderRadius: 6, maxWidth: 640 }}>
      <label style={{ display: "block", marginBottom: 8, fontWeight: 600 }}>
        Select date folder
        <select
          value={selectedDate}
          onChange={(e) => setSelectedDate(e.target.value)}
          style={{ marginLeft: 8, padding: 4 }}
        >
          <option value="">— choose date —</option>
          {availableDates.map((d) => (
            <option key={d} value={d}>
              {d}
            </option>
          ))}
        </select>
      </label>

      {loading && <div>Loading snapshot...</div>}

      {!loading && snapshot && (
        <div>
          <div style={{ marginBottom: 8 }}>
            <strong>Repo:</strong> {snapshot.repo}
          </div>
          <div style={{ marginBottom: 8 }}>
            <strong>Files ({snapshot.files.length}):</strong>
          </div>
          <ul style={{ maxHeight: 240, overflowY: "auto", paddingLeft: 16, margin: 0 }}>
            {snapshot.files.map((f) => (
              <li key={f.path} style={{ fontSize: 12, marginBottom: 2 }}>
                <code>{f.path}</code>{" "}
                <span style={{ color: "#666" }}>{f.sha256.slice(0, 12)}…</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {!loading && selectedDate && !snapshot && (
        <div style={{ color: "#a00" }}>No snapshot found for {selectedDate}</div>
      )}
    </div>
  );
};
```

---
