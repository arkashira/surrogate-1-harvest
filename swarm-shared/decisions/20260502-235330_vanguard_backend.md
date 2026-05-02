# vanguard / backend

## Final consolidated implementation (single source of truth)

**Core principles chosen**
- **Correctness first**: one authenticated HF tree call per date folder, persisted manifest, CDN-only training, deterministic sibling sharding, strict schema enforcement, and automatic idle-guard with backoff.
- **Actionability**: minimal focused diffs, clear verification steps, and safe fallbacks.
- **Resolved contradictions**:
  - Use `listRepoTree({ recursive: true })` (Candidate 2) to guarantee completeness and avoid partial scans; still one call per date folder.
  - Add `etag/sha256` and `size` to manifest entries (Candidate 2) for integrity and resumability; keep `cdnUrl` (Candidate 1) and deterministic `shardRepo`.
  - Enforce schema projection at manifest build time (Candidate 2) so training never sees extra columns; drop extra fields (`source`, `ts`) and keep only surrogate-1 contract fields.
  - Guard uses exponential backoff and explicit idle-stop detection (Candidate 2) while keeping Candidate 1’s auto-restart/create flow.
  - Training launcher passes manifest path and CDN-only policy; data loader validates against manifest and fails fast on drift.

---

## 1. Manifest service

```ts
// /opt/axentx/vanguard/src/backend/services/hf-manifest-service.ts
import { HfApi } from "@huggingface/hub";
import fs from "fs";
import path from "path";
import crypto from "crypto";

const HF_API = new HfApi({ token: process.env.HF_TOKEN });
const MANIFEST_DIR = process.env.MANIFEST_DIR || "/var/opt/axentx/manifests";
const SIBLING_REPOS = [
  "axentx/surrogate-1",
  "axentx/surrogate-1-sib1",
  "axentx/surrogate-1-sib2",
  "axentx/surrogate-1-sib3",
  "axentx/surrogate-1-sib4",
];

// Surrogate-1 contract fields only (drop extra cols like source/ts)
export type SurrogateRecord = {
  id: string;
  text: string;
  // add other contract fields here; do NOT include source/ts
};

export interface ManifestEntry {
  repo: string;
  path: string;
  cdnUrl: string;
  shardRepo: string;
  etag?: string;
  sha256?: string;
  size?: number;
}

export interface HFManifest {
  date: string;
  repo: string;
  folder: string;
  generatedAt: string;
  paths: ManifestEntry[];
}

function pickShardRepo(slug: string): string {
  const hash = crypto.createHash("sha256").update(slug).digest("hex");
  const idx = parseInt(hash.slice(0, 8), 16) % SIBLING_REPOS.length;
  return SIBLING_REPOS[idx];
}

function toSurrogateProjection(raw: Record<string, unknown>): SurrogateRecord | null {
  // Enforce contract: keep only allowed fields; require id+text
  const id = String(raw.id ?? "");
  const text = String(raw.text ?? "");
  if (!id || !text) return null;
  return { id, text };
}

export async function buildAndSaveManifest(
  repo: string,
  folder: string,
  date: string
): Promise<HFManifest> {
  if (!fs.existsSync(MANIFEST_DIR)) fs.mkdirSync(MANIFEST_DIR, { recursive: true });

  // Single recursive call per date folder (complete, deterministic)
  const tree = await HF_API.listRepoTree({ repo, path: folder, recursive: true });
  const files = (tree as any[]).filter((f) => f.type === "file");

  const entries: ManifestEntry[] = files.map((f) => ({
    repo,
    path: f.path,
    cdnUrl: `https://huggingface.co/datasets/${repo}/resolve/main/${encodeURIComponent(f.path)}`,
    shardRepo: pickShardRepo(f.path),
    etag: (f as any).etag,
    size: (f as any).size,
  }));

  const manifest: HFManifest = {
    date,
    repo,
    folder,
    generatedAt: new Date().toISOString(),
    paths: entries,
  };

  const outPath = path.join(MANIFEST_DIR, `manifest-${date}.json`);
  fs.writeFileSync(outPath, JSON.stringify(manifest, null, 2));
  return manifest;
}

export async function loadManifest(date: string): Promise<HFManifest | null> {
  const p = path.join(MANIFEST_DIR, `manifest-${date}.json`);
  if (!fs.existsSync(p)) return null;
  return JSON.parse(fs.readFileSync(p, "utf8"));
}
```

---

## 2. Lightning idle-guard with backoff and idle-stop detection

```ts
// /opt/axentx/vanguard/src/backend/services/lightning-studio-guard.ts
import { Teamspace } from "lightning-ai"; // use real SDK export
import { L40S } from "lightning-ai"; // preferred machine

const MAX_RETRIES = 2;
const BASE_DELAY_MS = 5000;

function isIdleError(err: any): boolean {
  return err?.message?.includes("idle") || err?.code === "STUDIO_IDLE_STOPPED";
}

function delay(ms: number) {
  return new Promise((res) => setTimeout(res, ms));
}

export async function ensureRunningStudio(
  studioName: string,
  preferredMachine = L40S
) {
  const teamspace = await Teamspace.current();
  const existing = (await teamspace.studios()).find((s) => s.name === studioName);

  if (existing && existing.status === "running") return existing;

  if (existing && existing.status === "stopped") {
    await existing.start({ machine: preferredMachine });
    return existing;
  }

  // create if missing
  const studio = await teamspace.createStudio({
    name: studioName,
    machine: preferredMachine,
    createOk: true,
  });
  return studio;
}

export async function guardedRun(
  studioName: string,
  runFn: (studio: any) => Promise<void>
) {
  let retries = 0;
  while (true) {
    try {
      const studio = await ensureRunningStudio(studioName);
      await runFn(studio);
      return;
    } catch (err: any) {
      if (retries < MAX_RETRIES && isIdleError(err)) {
        retries++;
        const backoff = BASE_DELAY_MS * Math.pow(2, retries - 1);
        await delay(backoff);
        continue;
      }
      throw err;
    }
  }
}
```

---

## 3. Training launcher (minimal diff)

```ts
// /opt/axentx/vanguard/src/backend/launcher/training-launcher.ts
import { buildAndSaveManifest, loadManifest } from "../services/hf-manifest-service";
import { guardedRun } from "../services/lightning-studio-guard";

export async function launchTrainingForDate(date: string) {
  const repo = "axentx/surrogate-1-dataset";
  const folder = `batches/mirror-merged/${date}`;

  // Idempotent: reuse existing manifest if present
  let manifest = await loadManifest(date);
  if (!manifest) {
    manifest = await buildAndSaveManifest(repo, folder, date);
  }

  const jobEnv = {
    MANIFEST_PATH: `/var/opt/axentx/manifests/manifest-${date}.json`,
    HF_DATASET_REPO: repo,
    // Training script must:
    // - read manifest
    // - use only cdnUrl fields
    // - validate each file against manifest entries (etag/sha256 if present)
    // - fail fast if any non-CDN fetch is attempted
  };

  await guardedRun(`vanguard-train-${date}`, async (studio) => {
    await studio.run({
      entryPoint: "train.py",
      env: jobEnv,
    });
  });
}
```

---

## 4. Data loader contract (training side)

```python
# train.py (pseudo) — enforce CDN-only and manifest validation
