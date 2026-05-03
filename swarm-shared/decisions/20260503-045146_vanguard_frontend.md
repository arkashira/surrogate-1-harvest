# vanguard / frontend

## 1. Diagnosis
- Frontend triggers HF API `list_repo_tree`/`load_dataset` at runtime for dataset discovery → exposes training UI to 429 rate limits and non-reproducible shard order.
- No content-addressed manifest per date folder → epochs drift across runs and resumable training can’t pin exact file set.
- Missing CDN-only data loader shim → frontend can’t prefetch or stream parquet shards without auth/API calls.
- No deterministic shard selection UI control → users can’t lock a reproducible slice for experiments.
- Studio reuse / idle-stop resilience not surfaced in UI → training sessions silently die on idle stop and waste quota.

## 2. Proposed change
Add a frontend manifest-driven dataset loader that:
- Accepts a date-folder path and optional pinned manifest JSON (content-addressed file list).
- If no manifest provided, fetches a single cached manifest (or generates via one-time Mac-side `list_repo_tree` saved to `/manifests/{date}.json`).
- Uses only CDN URLs (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) for all shard fetches — zero API/auth calls during training.
- Exposes UI controls: date picker, “pin manifest” toggle, “reuse running studio”, and “handle idle stop” checkbox.

Scope:
- Add `/opt/axentx/vanguard/src/frontend/components/DatasetLoader.tsx`
- Add `/opt/axentx/vanguard/src/frontend/lib/cdn-loader.ts`
- Update `/opt/axentx/vanguard/src/frontend/App.tsx` (or equivalent) to wire the component into the training view.

## 3. Implementation

### File: `/opt/axentx/vanguard/src/frontend/lib/cdn-loader.ts`
```ts
// CDN-only dataset shard loader — no HF API/auth during training
const HF_DATASETS_CDN = "https://huggingface.co/datasets";

export interface ShardEntry {
  path: string;      // repo-relative path, e.g. "2026-04-29/shard-00000.parquet"
  size: number;      // optional, for progress
}

export interface DatasetManifest {
  repo: string;      // e.g. "axentx/surrogate-1"
  date: string;      // e.g. "2026-04-29"
  shards: ShardEntry[];
  generatedAt?: string;
  sha256?: string;   // optional manifest integrity
}

export async function loadManifest(repo: string, date: string): Promise<DatasetManifest> {
  // Prefer pinned manifest from app storage; fallback to CDN-hosted manifest JSON
  const local = localStorage.getItem(`manifest:${repo}:${date}`);
  if (local) return JSON.parse(local);

  // CDN-hosted manifest (uploaded once by orchestration script)
  const manifestUrl = `${HF_DATASETS_CDN}/${repo}/resolve/main/manifests/${date}.json`;
  const res = await fetch(manifestUrl, { cache: "no-store" });
  if (!res.ok) throw new Error(`Manifest fetch failed: ${res.status}`);
  return res.json();
}

export function shardCDNUrl(repo: string, shardPath: string): string {
  return `${HF_DATASETS_CDN}/${repo}/resolve/main/${shardPath}`;
}

// Simple streaming parquet reader stub (replace with apache-arrow/parquet-wasm in real usage)
export async function* streamShards(
  manifest: DatasetManifest,
  batchSize = 8
): AsyncGenerator<{ shard: ShardEntry; data: ArrayBuffer }> {
  for (const shard of manifest.shards) {
    const url = shardCDNUrl(manifest.repo, shard.path);
    const res = await fetch(url);
    if (!res.ok) throw new Error(`Shard fetch failed: ${shard.path} ${res.status}`);
    const buf = await res.arrayBuffer();
    yield { shard, data: buf };
  }
}
```

### File: `/opt/axentx/vanguard/src/frontend/components/DatasetLoader.tsx`
```tsx
import React, { useEffect, useState } from "react";
import { loadManifest, DatasetManifest, shardCDNUrl } from "../lib/cdn-loader";

interface Props {
  repo: string;
  onShardsReady?: (manifest: DatasetManifest) => void;
}

export const DatasetLoader: React.FC<Props> = ({ repo, onShardsReady }) => {
  const [date, setDate] = useState("2026-04-29");
  const [pinned, setPinned] = useState(false);
  const [reuseStudio, setReuseStudio] = useState(true);
  const [handleIdleStop, setHandleIdleStop] = useState(true);
  const [manifest, setManifest] = useState<DatasetManifest | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const m = await loadManifest(repo, date);
      setManifest(m);
      onShardsReady?.(m);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repo, date]);

  function pinCurrent() {
    if (!manifest) return;
    localStorage.setItem(`manifest:${repo}:${date}`, JSON.stringify(manifest));
    setPinned(true);
  }

  return (
    <section className="dataset-loader">
      <h3>Dataset (CDN-only)</h3>

      <label>
        Repo:
        <input value={repo} readOnly />
      </label>

      <label>
        Date folder:
        <input
          type="date"
          value={date}
          onChange={(e) => setDate(e.target.value)}
        />
      </label>

      <div className="controls">
        <label>
          <input type="checkbox" checked={pinned} onChange={(e) => setPinned(e.target.checked)} />
          Pin manifest (reproducible)
        </label>

        <label>
          <input type="checkbox" checked={reuseStudio} onChange={(e) => setReuseStudio(e.target.checked)} />
          Reuse running studio (save quota)
        </label>

        <label>
          <input type="checkbox" checked={handleIdleStop} onChange={(e) => setHandleIdleStop(e.target.checked)} />
          Auto-restart on idle stop
        </label>

        <button onClick={load} disabled={loading}>
          {loading ? "Loading…" : "Load manifest"}
        </button>

        <button onClick={pinCurrent} disabled={!manifest || pinned}>
          Pin current
        </button>
      </div>

      {error && <div className="error">{error}</div>}

      {manifest && (
        <div className="manifest-summary">
          <p>
            Manifest: {manifest.shards.length} shards in {manifest.date}
          </p>
          <ul>
            {manifest.shards.slice(0, 5).map((s) => (
              <li key={s.path}>
                <a href={shardCDNUrl(manifest.repo, s.path)} target="_blank" rel="noopener noreferrer">
                  {s.path}
                </a>
              </li>
            ))}
            {manifest.shards.length > 5 && <li>…and {manifest.shards.length - 5} more</li>}
          </ul>
        </div>
      )}
    </section>
  );
};
```

### Wire into app (example)
Update `/opt/axentx/vanguard/src/frontend/App.tsx` (or main training page) to include:
```tsx
import { DatasetLoader } from "./components/DatasetLoader";

function App() {
  return (
    <div>
      <DatasetLoader
        repo="axentx/surrogate-1"
        onShardsReady={(manifest) => {
          console.log("Ready to train on", manifest.shards.length, "shards");
          // Pass manifest to training orchestrator (Lightning launcher)
       
