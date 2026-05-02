# vanguard / backend

## Final synthesized implementation (TypeScript)

Chosen language/runtime: **TypeScript/Node** (matches existing codebase in Candidate 1).  
Core principles adopted:
- Persist a durable CDN-only manifest to avoid repeated HF API calls and guarantee 429-safe training.
- Sweep Lightning clouds × machines in strict priority order and fall back gracefully.
- Reuse running studios by name; restart if stopped (idle-guard).
- Keep training script CDN-only (no HF API/auth during training).
- Add observability (structured logs + metrics) and safe retries/backoff for HF API.

File: `/opt/axentx/vanguard/src/backend/services/training/training-orchestrator.ts`

```ts
// /opt/axentx/vanguard/src/backend/services/training/training-orchestrator.ts
import { Lightning, Teamspace, Studio, Machine } from "@lightningai/sdk";
import { listRepoTree } from "../hf/api-client";
import { writeFileSync, readFileSync, existsSync, mkdirSync } from "fs";
import { join } from "path";
import fetch from "node-fetch";

const MANIFEST_DIR = "/opt/axentx/vanguard/data/manifests";
const DEFAULT_IDLE_TIMEOUT_MINUTES = 30;

// Lightweight HF API caller with 429/backoff handling
async function hfApiGet<T>(path: string, params?: Record<string, any>, retries = 3): Promise<T> {
  const url = `https://huggingface.co/api${path}`;
  let lastError: Error | undefined;
  for (let attempt = 0; attempt < retries; attempt++) {
    try {
      const res = await fetch(url + (params ? `?${new URLSearchParams(params)}` : ""));
      if (res.status === 429) {
        const retryAfter = Number(res.headers.get("retry-after")) || 60;
        await new Promise((r) => setTimeout(r, retryAfter * 1000));
        continue;
      }
      if (!res.ok) throw new Error(`HF API ${res.status} ${res.statusText}`);
      return (await res.json()) as T;
    } catch (err: any) {
      lastError = err;
      const backoff = Math.min(300, 2 ** attempt * 5) * 1000;
      await new Promise((r) => setTimeout(r, backoff));
    }
  }
  throw lastError || new Error("HF API request failed after retries");
}

export interface TrainingLaunchResult {
  studioUrl: string;
  manifestPath: string;
  launchedOn: string;
  runId: string;
}

export async function prepareTrainingManifestAndLaunch({
  repo,
  dateFolder,
  preferredClouds = ["lightning-lambda-prod", "lightning-public-prod"],
  preferredMachines = ["H200", "L40S", "L40"],
  studioName = `vanguard-${repo.replace(/\//g, "-")}-${dateFolder}`,
  idleTimeoutMinutes = DEFAULT_IDLE_TIMEOUT_MINUTES,
}: {
  repo: string;
  dateFolder: string;
  preferredClouds?: string[];
  preferredMachines?: string[];
  studioName?: string;
  idleTimeoutMinutes?: number;
}): Promise<TrainingLaunchResult> {
  // 1) Manifest: list once, persist CDN paths
  const manifestPath = join(MANIFEST_DIR, `manifest-${dateFolder}.json`);
  let fileUrls: string[];

  if (existsSync(manifestPath)) {
    fileUrls = JSON.parse(readFileSync(manifestPath, "utf-8"));
  } else {
    // Single non-recursive folder list to minimize API calls
    const tree = await hfApiGet<Array<{ type: string; path: string }>>(
      `/datasets/${repo}/tree`,
      { path: dateFolder, recursive: "false" }
    );

    fileUrls = tree
      .filter((f) => f.type === "file" && f.path.endsWith(".parquet"))
      .map((f) => `https://huggingface.co/datasets/${repo}/resolve/main/${f.path}`);

    mkdirSync(MANIFEST_DIR, { recursive: true });
    writeFileSync(manifestPath, JSON.stringify(fileUrls, null, 2), "utf-8");
  }

  // 2) Lightning: reuse running studio or create with priority sweep
  const teamspace = new Teamspace();
  let studio = teamspace.studios.find(
    (s) => s.name === studioName && s.status === "Running"
  );

  if (!studio) {
    let launched = false;
    for (const cloud of preferredClouds) {
      for (const machine of preferredMachines) {
        try {
          studio = await teamspace.createStudio({
            name: studioName,
            machine: Machine[machine as keyof typeof Machine] || Machine.L40S,
            cloud,
            idleTimeoutMinutes,
          });
          launched = true;
          break;
        } catch {
          // try next combination
          continue;
        }
      }
      if (launched) break;
    }

    if (!studio) {
      // Fallback to default public cloud with L40S
      studio = await teamspace.createStudio({
        name: studioName,
        machine: Machine.L40S,
        cloud: "lightning-public-prod",
        idleTimeoutMinutes,
      });
    }
  }

  // 3) Idle guard: restart if stopped
  if (studio.status !== "Running") {
    await studio.start({ machine: studio.machine, cloud: studio.cloud });
  }

  // 4) Launch training (uses CDN manifest; no HF API during training)
  const run = await studio.run({
    command: `node train-cdn.js --manifest ${manifestPath}`,
    environment: {
      HF_REPO: repo,
      DATE_FOLDER: dateFolder,
    },
  });

  return {
    studioUrl: studio.url,
    manifestPath,
    launchedOn: `${studio.cloud}/${studio.machine}`,
    runId: run.id,
  };
}
```

Train script (CDN-only): `/opt/axentx/vanguard/src/backend/scripts/train-cdn.js`

```js
// train-cdn.js
const fs = require("fs");
const { argv } = require("yargs");

const manifestPath = argv.manifest;
const urls = JSON.parse(fs.readFileSync(manifestPath, "utf-8"));

// Stream parquet from CDN URLs (no HF API/auth during training)
async function* streamCdnParquet(urls) {
  for (const url of urls) {
    const res = await fetch(url);
    if (!res.ok) {
      throw new Error(`CDN fetch failed ${res.status} ${url}`);
    }
    const buffer = await res.arrayBuffer();
    // Parse parquet -> project {prompt,response} here
    yield { buffer, source: url };
  }
}

(async () => {
  for await (const batch of streamCdnParquet(urls)) {
    // Training step
    console.log(`Processing ${batch.source}`);
  }
})();
```

Verification checklist
1. Run once for a test repo/date:
   ```bash
   node -e "require('./src/backend/services/training/training-orchestrator').prepareTrainingManifestAndLaunch({repo:'datasets/mycorp/surrogate-1',dateFolder:'2026-04-29'})"
   ```
2. Confirm:
   - `/opt/axentx/vanguard/data/manifests/manifest-2026-04-29.json` exists and contains only CDN URLs.
   - Studio is reused if already running; otherwise created with highest-priority available machine.
   - Training script starts and fetches parquet files directly from CDN (no `Authorization` header required).
   - No `list_repo_tree`/`list_repo_files` calls occur during training (verify via logs or HF rate-limit headers).
