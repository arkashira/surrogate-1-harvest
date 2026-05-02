# vanguard / frontend

## Final Synthesized Implementation

**File:** `/opt/axentx/vanguard/src/features/training/TrainingLauncher.tsx`

```tsx
import { useState, useEffect, useCallback, useRef } from 'react';
import { Teamspace, Studio, Machine } from '@lightningai/sdk';
import axios from 'axios';

type DatasetFile = { path: string; size: number; sha256?: string };
type Manifest = { dateFolder: string; files: DatasetFile[]; generatedAt: string };

const HF_REPO = 'datasets/axentx/surrogate-1';
const CDN_ROOT = `https://huggingface.co/${HF_REPO}/resolve/main`;
const STUDIO_NAME = 'vanguard-train';
const IDLE_TIMEOUT_MS = 5 * 60 * 1000; // 5m idle-guard for polling

const MACHINE_PRIORITY = [
  Machine.L40S,
  Machine.A100_40GB,
  Machine.A100_80GB,
] as const;

function shardRepoForWrite(slug: string): string {
  const siblings = 5;
  const idx = Array.from(slug).reduce((a, c) => a + c.charCodeAt(0), 0) % siblings;
  return `datasets/axentx/surrogate-1-shard-${idx}`;
}

async function fetchManifest(dateFolder: string): Promise<Manifest> {
  const res = await axios.get<Manifest>(`/api/v1/manifests/${dateFolder}.json`);
  return res.data;
}

async function findRunningStudio(name: string): Promise<Studio | null> {
  const studios = await Teamspace.studios();
  return studios.find((s) => s.name === name && s.status === 'Running') || null;
}

async function ensureRunningStudio(name: string): Promise<Studio> {
  const existing = await findRunningStudio(name);
  if (existing) return existing;

  for (const machine of MACHINE_PRIORITY) {
    try {
      const studio = await Teamspace.createStudio({
        name,
        machine,
        createOk: true,
      });
      if (studio) return studio;
    } catch {
      // machine unavailable or quota; try next
    }
  }
  throw new Error('No available machine for studio');
}

export function TrainingLauncher() {
  const [dateFolder, setDateFolder] = useState('2026-04-29');
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [studioStatus, setStudioStatus] = useState<'idle' | 'starting' | 'running' | 'error'>('idle');
  const [runStatus, setRunStatus] = useState<'pending' | 'running' | 'completed' | 'failed'>('pending');
  const [logs, setLogs] = useState<string[]>([]);
  const abortRef = useRef<AbortController | null>(null);
  const pollRef = useRef<NodeJS.Timeout | null>(null);

  const addLog = useCallback((msg: string) => {
    setLogs((prev) => [...prev.slice(-99), `${new Date().toISOString()} ${msg}`]);
  }, []);

  const loadManifest = useCallback(async () => {
    try {
      const m = await fetchManifest(dateFolder);
      setManifest(m);
      addLog(`Manifest loaded: ${m.files.length} files`);
    } catch (err) {
      console.error('Failed to load manifest', err);
      addLog('Failed to load manifest');
    }
  }, [dateFolder, addLog]);

  useEffect(() => {
    loadManifest();
  }, [loadManifest]);

  const stopPolling = useCallback(() => {
    if (pollRef.current) clearTimeout(pollRef.current);
    pollRef.current = null;
  }, []);

  const pollRun = useCallback(
    async (runId: string, studio: Studio, deadline: number) => {
      stopPolling();

      const tick = async () => {
        if (Date.now() > deadline) {
          setRunStatus('failed');
          addLog('Polling timed out (idle-guard)');
          return;
        }

        try {
          const updated = await studio.getRun(runId);
          setRunStatus(updated.status === 'running' ? 'running' : updated.status === 'completed' ? 'completed' : 'failed');
          addLog(`Run ${runId}: ${updated.status}`);

          if (updated.status === 'running' || updated.status === 'pending') {
            pollRef.current = setTimeout(tick, 8000);
          } else {
            stopPolling();
          }
        } catch (err) {
          setRunStatus('failed');
          addLog(`Polling error: ${err instanceof Error ? err.message : String(err)}`);
          stopPolling();
        }
      };

      pollRef.current = setTimeout(tick, 3000);
    },
    [addLog, stopPolling]
  );

  const launchTraining = async () => {
    if (!manifest) return;
    abortRef.current?.abort();
    abortRef.current = new AbortController();

    setStudioStatus('starting');
    setRunStatus('pending');
    setLogs([]);
    addLog('Launch requested');

    try {
      const studio = await ensureRunningStudio(STUDIO_NAME);
      setStudioStatus('running');
      addLog(`Studio ready: ${studio.id} (${studio.machine})`);

      const fileList = manifest.files.map((f) => `${CDN_ROOT}/${f.path}`);

      const run = await studio.run(
        {
          entryPoint: 'train.py',
          env: {
            FILE_LIST_JSON: JSON.stringify(fileList),
            HF_DATASET_REPO: HF_REPO,
          },
        },
        { signal: abortRef.current.signal }
      );

      addLog(`Run started: ${run.id}`);
      const deadline = Date.now() + IDLE_TIMEOUT_MS;
      pollRun(run.id, studio, deadline);
    } catch (err) {
      if (abortRef.current?.signal.aborted) {
        addLog('Launch aborted');
        return;
      }
      console.error('Launch failed', err);
      setStudioStatus('error');
      setRunStatus('failed');
      addLog(`Launch failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  };

  const cancelRun = () => {
    abortRef.current?.abort();
    stopPolling();
    setRunStatus('failed');
    addLog('Run cancelled');
  };

  useEffect(() => {
    return () => {
      abortRef.current?.abort();
      stopPolling();
    };
  }, [stopPolling]);

  return (
    <div className="p-4 max-w-xl space-y-4">
      <h2 className="text-lg font-semibold">Surrogate-1 Training Launcher</h2>

      <label className="block">
        Date folder:
        <input
          className="border rounded px-2 py-1 ml-2 w-40"
          value={dateFolder}
          onChange={(e) => setDateFolder(e.target.value)}
        />
      </label>

      <div className="text-sm text-gray-600">
        Manifest: {manifest ? `${manifest.files.length} files` : 'loading...'}
      </div>

      <div className="flex gap-2">
        <button
          onClick={launchTraining}
          disabled={!manifest || studioStatus === 'starting' || runStatus === 'running'}
          className="px-4 py-2 bg-blue-600 text-white rounded disabled:opacity-50"
        >
          {studioStatus === 'starting' ? 'Starting studio...' : 'Launch Training'}
        </button>

        {runStatus === 'running' && (
          <button onClick={cancelRun} className="px-3 py-2 border rounded">
            Cancel
          </button>
        )}
      </div>

      {studioStatus === 'error' && (
        <p className="text-red-600">Launch failed — check console and machine availability.</p>
      )}

      {runStatus === 'completed' && <p className="text-green-600">Run completed.</p>}

      {logs.length >
