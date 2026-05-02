# vanguard / frontend

## Final Synthesized Implementation

**Chosen approach:** Combine Candidate 1’s concrete caching/sharding code with Candidate 2’s hook-based structure, priority machine sweep, and idle-guard UX.  
**File:** `/opt/axentx/vanguard/src/features/training/TrainingLauncher.tsx`

```tsx
// /opt/axentx/vanguard/src/features/training/TrainingLauncher.tsx
import React, { useEffect, useState, useCallback, useMemo } from 'react';
import { Lightning, Teamspace, Studio, Machine } from '@lightningai/sdk';
import axios from 'axios';

type FileEntry = { path: string; size: number };

// ---- CDN-only file list (persisted by backend or pre-listed) ----
function useCdnFileList(dateFolder: string, repo: string) {
  const [files, setFiles] = useState<FileEntry[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const controller = new AbortController();
    // Primary: backend-provided manifest (avoids frontend HF API calls)
    axios
      .get<{ files: FileEntry[] }>(`/api/manifests/${repo}/${dateFolder}.json`, {
        signal: controller.signal,
      })
      .then((res) => {
        setFiles(res.data.files);
        setLoading(false);
      })
      .catch(() => {
        setLoading(false); // fail-fast handled by UI
      });
    return () => controller.abort();
  }, [repo, dateFolder]);

  const cdnUrls = useMemo(
    () =>
      files.map(
        (f) =>
          `https://huggingface.co/datasets/${repo}/resolve/main/${dateFolder}/${encodeURIComponent(
            f.path
          )}`
      ),
    [files, dateFolder, repo]
  );

  return { files, cdnUrls, loading };
}

// ---- Deterministic repo sharding for HF commit cap ----
function pickShardRepo(baseRepo: string, siblings: string[] = [], slug: string): string {
  if (!siblings.length) return baseRepo;
  let hash = 0;
  for (let i = 0; i < slug.length; i++) {
    hash = ((hash << 5) - hash) + slug.charCodeAt(i);
    hash |= 0;
  }
  const idx = Math.abs(hash) % siblings.length;
  return siblings[idx];
}

// ---- Cloud/size priority sweep (H200 first, then L40S, then A100) ----
const MACHINE_PRIORITY: Machine[] = [
  Machine.H200,
  Machine.L40S,
  Machine.A100_80GB,
  Machine.A100_40GB,
];

function usePreferredMachine() {
  const [preferred, setPreferred] = useState<Machine>(Machine.L40S);
  const [tried, setTried] = useState<Set<Machine>>(new Set());

  const nextMachine = useCallback(() => {
    const remaining = MACHINE_PRIORITY.filter((m) => !tried.has(m));
    return remaining[0] || Machine.L40S;
  }, [tried]);

  useEffect(() => {
    setPreferred(nextMachine());
  }, [nextMachine]);

  const markTried = useCallback(
    (m: Machine) => {
      setTried((s) => new Set(s).add(m));
      setPreferred(nextMachine());
    },
    [nextMachine]
  );

  return { preferred, markTried };
}

// ---- Reuse running studio; restart if stopped ----
function useLightningStudio(
  name: string,
  preferredMachine: Machine = Machine.L40S
) {
  const [studio, setStudio] = useState<Studio | null>(null);
  const [status, setStatus] = useState<'idle' | 'starting' | 'running' | 'stopped'>(
    'idle'
  );
  const [error, setError] = useState<string | null>(null);

  const findRunning = useCallback(async (): Promise<Studio | null> => {
    try {
      const teamspace = await Teamspace.current();
      const found = teamspace.studios.find(
        (s) => s.name === name && s.status === 'Running'
      );
      return found || null;
    } catch {
      return null;
    }
  }, [name]);

  const ensureRunning = useCallback(async (): Promise<Studio | null> => {
    setStatus('starting');
    setError(null);
    try {
      let target = await findRunning();
      if (!target) {
        const teamspace = await Teamspace.current();
        target = await teamspace.createStudio({
          name,
          machine: preferredMachine,
          createOk: true,
        });
      }
      if (target.status !== 'Running') {
        await target.start({ machine: preferredMachine });
      }
      setStudio(target);
      setStatus('running');
      return target;
    } catch (err: any) {
      setError(err?.message || 'Studio start failed');
      setStatus('stopped');
      return null;
    }
  }, [name, preferredMachine, findRunning]);

  // Periodic idle/health check
  useEffect(() => {
    if (!studio) return;
    const id = setInterval(async () => {
      try {
        const updated = await studio.fetch();
        setStatus(updated.status === 'Running' ? 'running' : 'stopped');
      } catch {
        setStatus('stopped');
      }
    }, 30_000);
    return () => clearInterval(id);
  }, [studio]);

  return { studio, status, error, ensureRunning };
}

// ---- Main component ----
export default function TrainingLauncher({
  config,
}: {
  config: {
    datasetRepo: string;
    datasetFolder: string;
    studioName: string;
    slug: string;
    siblingRepos?: string[];
    baseRepo: string;
    epochs: number;
  };
}) {
  const { cdnUrls, loading: manifestLoading } = useCdnFileList(
    config.datasetFolder,
    config.datasetRepo
  );
  const { preferred, markTried } = usePreferredMachine();
  const { studio, status, error, ensureRunning } = useLightningStudio(
    config.studioName,
    preferred
  );

  const runTraining = useCallback(async () => {
    if (manifestLoading || !cdnUrls.length) return;

    const active = await ensureRunning();
    if (!active) return;

    const shard = pickShardRepo(config.baseRepo, config.siblingRepos, config.slug);

    try {
      const run = await active.run({
        command: `python train.py --data-urls '${JSON.stringify(
          cdnUrls
        )}' --epochs ${config.epochs}`,
        environment: {
          HF_REPO_SHARD: shard,
        },
      });

      // If machine fails, try next in priority
      run.on('error', (err: any) => {
        if (err?.message?.includes('Unsupported') || err?.message?.includes('Unavailable')) {
          markTried(preferred);
        }
      });
    } catch (err: any) {
      if (err?.message?.includes('Unsupported') || err?.message?.includes('Unavailable')) {
        markTried(preferred);
      }
    }
  }, [
    manifestLoading,
    cdnUrls,
    ensureRunning,
    config,
    preferred,
    markTried,
  ]);

  const idleWarning = status === 'running' ? (
    <div style={{ color: '#b8860b', fontSize: 12 }}>
      ⚠ Studio may auto-stop due to idle timeout. Restart if training fails.
    </div>
  ) : null;

  return (
    <div>
      {idleWarning}
      <button
        onClick={runTraining}
        disabled={
          manifestLoading ||
          !cdnUrls.length ||
          status === 'starting' ||
          status === 'running'
        }
      >
        {(() => {
          if (manifestLoading) return 'Loading manifest...';
          if (!cdnUrls.length) return 'No data available';
          if (status === 'starting') return
