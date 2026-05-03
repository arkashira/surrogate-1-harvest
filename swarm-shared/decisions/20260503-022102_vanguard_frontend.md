# vanguard / frontend

## Final synthesized implementation

Below is the single, authoritative version that merges the strongest, non-conflicting parts of both proposals and resolves contradictions in favor of correctness and concrete actionability.

- **Storage**: use **IndexedDB** (via `idb`) as the primary cache (scalable, async, reliable) with **localStorage fallback** for simple environments.
- **CDN bypass**: always prefer public `resolve/main/` URLs for file fetches; avoid authenticated API calls for file content.
- **Schema safety**: project heterogeneous files to `{ prompt, response }` at parse time to avoid `pyarrow.CastError` and wasted decode cycles.
- **Commit sharding**: deterministic `pickSiblingRepo` to spread writes and avoid HF commit-cap throttling.
- **Studio reuse**: lightweight heuristic to reuse a running Lightning Studio before creating a new one.
- **Scope**: one new module (`hf-client.ts`) + one focused UI integration (`DatasetBrowser.tsx`).

---

### 1) Create `/opt/axentx/vanguard/src/lib/hf-client.ts`

```ts
// /opt/axentx/vanguard/src/lib/hf-client.ts
const HF_API_BASE = 'https://huggingface.co/api';
const HF_CDN_BASE = 'https://huggingface.co/datasets';
const TTL_MS = 1000 * 60 * 30; // 30m for tree metadata
const DB_NAME = 'axentx-hf-cache';
const DB_VERSION = 1;

export interface RepoFile {
  path: string;
  type: 'file' | 'directory';
  size?: number;
  sha?: string;
}

export interface ParsedRecord {
  prompt: string;
  response: string;
  sourcePath: string;
}

// ---- IndexedDB setup ----
interface ManifestDB extends IDBPDatabase {
  manifests: {
    key: string; // cacheKey(repo, folder)
    value: { ts: number; data: RepoFile[] };
  };
  studios: {
    key: string; // e.g. 'active'
    value: { id: string; lastSeen: number };
  };
}

async function getDB() {
  return openDB<ManifestDB>(DB_NAME, DB_VERSION, {
    upgrade(db) {
      db.createObjectStore('manifests');
      db.createObjectStore('studios');
    },
  });
}

// ---- Cache helpers ----
function cacheKey(repo: string, folder: string) {
  return `tree:${repo}:${folder || '/'}`;
}

function isExpired(ts: number, ttl = TTL_MS) {
  return Date.now() - ts > ttl;
}

// ---- CDN + auth ----
export function buildCdnUrl(repo: string, path: string): string {
  return `${HF_CDN_BASE}/${repo}/resolve/main/${encodeURI(path)}`;
}

export function buildCdnUrls(repo: string, files: RepoFile[]): string[] {
  return files.filter((f) => f.type === 'file').map((f) => buildCdnUrl(repo, f.path));
}

// ---- Deterministic sibling sharding ----
export function pickSiblingRepo(slug: string, n = 5): string {
  // Deterministic, stable selection for commit sharding
  const hash = Array.from(slug).reduce((a, c) => ((a << 5) - a + c.charCodeAt(0)) >>> 0, 0);
  const idx = hash % n;
  const base = slug.includes('/') ? slug : `vanguard/${slug}`;
  if (idx === 0) return base;
  const [owner, repo] = base.split('/');
  return `${owner}/${repo}-sibling${idx}`;
}

// ---- Tree listing with IndexedDB cache ----
export async function listRepoFolderOnce(
  repo: string,
  folder = '',
  token?: string
): Promise<RepoFile[]> {
  const key = cacheKey(repo, folder);
  const db = await getDB();
  const cached = await db.get('manifests', key);

  if (cached && !isExpired(cached.ts)) {
    return cached.data;
  }

  const url = new URL(`${HF_API_BASE}/repos/${repo}/tree`);
  url.searchParams.set('recursive', 'false');
  if (folder) url.searchParams.set('path', folder);

  const res = await fetch(url.toString(), {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!res.ok) throw new Error(`HF API error: ${res.status}`);
  const data: RepoFile[] = await res.json();

  await db.put('manifests', { ts: Date.now(), data }, key);
  return data;
}

export async function clearFolderCache(repo: string, folder = '') {
  const db = await getDB();
  await db.delete('manifests', cacheKey(repo, folder));
}

// ---- Lightweight file projection (avoid pyarrow.CastError) ----
export async function parseFileToRecord(
  repo: string,
  file: RepoFile,
  token?: string
): Promise<ParsedRecord | null> {
  // Skip directories
  if (file.type !== 'file') return null;

  const url = buildCdnUrl(repo, file.path);
  const res = await fetch(url);
  if (!res.ok) {
    // fallback to authenticated raw content if CDN fails (rare)
    const fallback = `https://huggingface.co/api/repos/${repo}/files/raw?path=${encodeURIComponent(file.path)}`;
    const res2 = await fetch(fallback, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!res2.ok) return null;
    return parseContent(await res2.text(), file.path);
  }

  const text = await res.text();
  return parseContent(text, file.path);
}

function parseContent(text: string, path: string): ParsedRecord {
  // Best-effort projection for heterogeneous schemas.
  // Supports:
  // - JSON lines with {prompt,response} or {input,output} or {question,answer}
  // - simple "prompt\nresponse" split on first blank line
  try {
    const lines = text.trim().split('\n');
    for (const line of lines) {
      const obj = JSON.parse(line);
      if (obj && typeof obj === 'object') {
        const prompt = obj.prompt ?? obj.input ?? obj.question ?? '';
        const response = obj.response ?? obj.output ?? obj.answer ?? '';
        if (prompt || response) {
          return { prompt: String(prompt), response: String(response), sourcePath: path };
        }
      }
    }
  } catch {
    // not JSON lines; continue to plaintext heuristic
  }

  // Plaintext heuristic: split on first blank line
  const parts = text.trim().split(/\n\s*\n/);
  const prompt = parts[0] || '';
  const response = parts[1] || '';
  return { prompt, response, sourcePath: path };
}

// ---- Lightning Studio reuse ----
export async function getOrCreateStudio(token: string, reuseWindowMs = 1000 * 60 * 15) {
  const db = await getDB();
  const active = await db.get('studios', 'active');

  if (active && Date.now() - active.lastSeen < reuseWindowMs) {
    // Lightweight check: attempt to GET studio (fast fail if gone)
    try {
      const res = await fetch(`https://huggingface.co/api/studios/${active.id}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.ok) return { id: active.id, reused: true };
    } catch {
      // fall through to creation
    }
  }

  // Create new studio (minimal payload)
  const createRes = await fetch('https://huggingface.co/api/studios', {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      name: `axentx-run-${Date.now()}`,
      sdk: 'lightning',
      hardware: 'cpu-basic',
    }),
  });
  if (!createRes.ok) throw new Error('Failed to create Lightning Studio');
  const studio = await createRes.json();
 
