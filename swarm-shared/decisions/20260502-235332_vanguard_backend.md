# vanguard / backend

## Final consolidated solution

**Chosen approach**: merge Candidate 1’s concrete, working code with Candidate 2’s operational safeguards (sibling-repo sharding, durable backend manifest, deterministic fallback, and endpoint contract). Where they conflict, prefer correctness + production-grade actionability.

### 1. Diagnosis (merged)
- Repeated authenticated `list_repo_tree`/`list_repo_files` on HF burns quota and risks 429.
- No durable file manifest → training cannot guarantee CDN-only fetches and re-lists on every run.
- No sibling-repo sharding for HF ingestion commits → ingestion hits 128/hr/repo cap and blocks continuous data flow.
- Launcher does not sweep Lightning clouds × sizes in priority order → jobs stall instead of falling back.
- No idle-guard or studio-reuse before `.run()` → Lightning idle-stop kills training and wastes quota on repeated studio creation.

### 2. High-level design
- Add backend services:
  - `hf-manifest.ts`: generate/persist file manifests; sibling-repo sharding for HF writes.
  - `lightning-launcher.ts`: sweep clouds × sizes; reuse running studios; idle-guard `.run()`.
- Expose one endpoint:
  - `POST /api/training/prepare` → `{ repo, dateFolder }` → returns `{ manifestId, fileCount, studioUrl, machine }`.
- Training script uses `--manifest` (local path or S3) and loads exclusively via CDN URLs.

### 3. Implementation

#### File: `/opt/axentx/vanguard/src/backend/services/hf-manifest.ts`
```ts
import { listRepoTree } from "./hfApi";
import fs from "fs/promises";
import path from "path";
import crypto from "crypto";

const MANIFEST_DIR = path.resolve(process.cwd(), "data/manifests");
const CACHE_TTL_MS = 24 * 60 * 60 * 1000; // 1 day

async function ensureDir(dir: string) {
  await fs.mkdir(dir, { recursive: true });
}

/**
 * Deterministic sibling-repo selection for HF writes to avoid 128/hr/repo cap.
 * siblings example: ["company-ds-ingest-0", "company-ds-ingest-1", ...]
 */
export function siblingRepoFor(slug: string, siblings: string[]): string {
  if (!siblings?.length) throw new Error("siblings must be non-empty");
  const hash = crypto.createHash("sha256").update(slug).digest("hex");
  const idx = Number.parseInt(hash.slice(0, 8), 16) % siblings.length;
  return siblings[idx];
}

/**
 * Generate or reuse a persisted file manifest for a dataset repo + folder.
 * Uses a single authenticated listRepoTree call (recursive=false) per folder.
 * Manifest format:
 * {
 *   repo: string,
 *   folder: string,
 *   files: string[],
 *   generatedAt: number,
 *   manifestId: string
 * }
 */
export async function generateFileManifest(
  repo: string,
  folder: string,
  options?: { ttlMs?: number; siblings?: string[] }
): Promise<{ manifestId: string; repo: string; folder: string; files: string[]; generatedAt: number }> {
  await ensureDir(MANIFEST_DIR);
  const ttlMs = options?.ttlMs ?? CACHE_TTL_MS;
  const slug = `${repo.replace(/\//g, "__")}__${folder.replace(/\//g, "_")}`;
  const manifestId = crypto.createHash("sha256").update(slug).digest("hex").slice(0, 16);
  const manifestPath = path.join(MANIFEST_DIR, `${manifestId}.json`);

  try {
    const raw = await fs.readFile(manifestPath, "utf8");
    const parsed = JSON.parse(raw);
    if (
      parsed.repo === repo &&
      parsed.folder === folder &&
      Date.now() - parsed.generatedAt < ttlMs
    ) {
      return parsed;
    }
  } catch {
    // no valid cache
  }

  // Single API call: non-recursive per folder (avoids pagination explosion)
  const tree = await listRepoTree(repo, folder, { recursive: false });
  const files = tree
    .filter((f) => f.type === "file" && f.path.endsWith(".parquet"))
    .map((f) => f.path);

  const manifest = { repo, folder, files, generatedAt: Date.now(), manifestId };
  await fs.writeFile(manifestPath, JSON.stringify(manifest, null, 2), "utf8");
  return manifest;
}
```

#### File: `/opt/axentx/vanguard/src/backend/services/lightning-launcher.ts`
```ts
import { Lightning, Teamspace, Studio, Machine } from "lightning-ai";
import fs from "fs/promises";
import path from "path";

const MANIFEST_DIR = path.resolve(process.cwd(), "data/manifests");

async function ensureDir(dir: string) {
  await fs.mkdir(dir, { recursive: true });
}

/**
 * Reuse running studio or start a new one with priority machine sweep.
 * Priority: Lightning-lambda-prod (H200) -> lightning-public-prod (L40S) -> fallback sizes.
 */
export async function reuseOrStartStudio(
  name: string,
  projectName = "vanguard-training",
  idleTimeoutMs = 30 * 60 * 1000
): Promise<Studio> {
  const teamspace = new Teamspace();
  const running = teamspace.studios.find(
    (s) => s.name === name && s.status === "Running"
  );
  if (running) return running;

  const candidates = [
    { cloud: "lightning-lambda-prod", machine: Machine.H200 },
    { cloud: "lightning-public-prod", machine: Machine.L40S },
    { cloud: "lightning-public-prod", machine: Machine.L40 },
    { cloud: "lightning-public-prod", machine: Machine.A100 },
  ];

  for (const { cloud, machine } of candidates) {
    try {
      const studio = new Studio({
        name,
        projectName,
        cloud,
        machine,
        idleTimeoutMs,
        createOk: true,
      });
      await studio.start();
      return studio;
    } catch (err) {
      console.warn(`Failed to start on ${cloud}/${machine}:`, err);
      continue;
    }
  }

  throw new Error("No available machine could be started across priority sweep.");
}

/**
 * Guard studio.run() against idle-stop deaths.
 * If studio is stopped, restart it before running the command.
 */
export async function runWithIdleGuard(
  studio: Studio,
  command: string[],
  opts?: { restartOnStop?: boolean }
) {
  const restartOnStop = opts?.restartOnStop ?? true;

  if (studio.status === "Stopped" && restartOnStop) {
    console.log("Studio stopped (idle). Restarting before run...");
    await studio.start();
  }

  return studio.run(command);
}

/**
 * High-level launcher for surrogate-1 training using CDN-only fetches.
 * Expects manifest already generated and available at MANIFEST_DIR.
 */
export async function launchSurrogate1Training(
  repo: string,
  folder: string,
  manifestId: string
) {
  const manifestPath = path.join(MANIFEST_DIR, `${manifestId}.json`);
  const raw = await fs.readFile(manifestPath, "utf8");
  const manifest = JSON.parse(raw);
  if (!manifest.files?.length) throw new Error("No parquet files in manifest.");

  const studio = await reuseOrStartStudio("vanguard-surrogate1");

  // Training script must use manifest and load exclusively via CDN URLs.
  await runWithIdleGuard(studio, [
    "python",
    "train_surrogate1.py",
    "--manifest",
    manifestPath,
    "--repo",
    repo,
    "--epochs",
    "3",
  ]);
}
```

#### File: `/opt/axentx/vanguard/src/backend/routes/training.ts` (or equivalent)
```ts
import express from "express";
import { generateFileManifest } from "../services/hf-manifest";
import { launchSurrogate1Training } from "../services/light
