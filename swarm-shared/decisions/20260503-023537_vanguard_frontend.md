# vanguard / frontend

To synthesize the best parts of multiple AI proposals and combine the strongest insights into one final answer, let's analyze the provided information and identify key points from both candidates.

**Key Issues Identified:**

1. **Authenticated API Calls**: Both candidates highlight the issue of the frontend triggering authenticated HF API calls (`list_repo_tree`, `load_dataset`) on every preview/training launch, which burns quota and risks 429 errors.
2. **Lack of CDN Bypass**: Data loads route through `/api/` instead of using public CDN URLs, which is inefficient.
3. **No Frontend Manifest Cache**: Each launch re-enumerates repo files via API instead of using a static file list, leading to unnecessary API calls.
4. **No Offline-First Approach**: The UX blocks if the API is throttled, instead of using a locally cached manifest and CDN fetches.
5. **Missing Lightweight ETag/Last-Modified Check**: Unnecessary re-fetches occur when the manifest hasn't changed.

**Proposed Solutions:**

1. **Implement a Frontend Manifest Cache**: Both candidates suggest creating a manifest cache to store the file list, reducing the need for frequent API calls.
2. **Use CDN Bypass Loader**: Implement a CDN bypass loader to fetch data directly from public CDN URLs instead of going through the `/api/` endpoint.
3. **Offline-First Approach**: Implement an offline-first approach by using a locally cached manifest and CDN fetches when the API is throttled or unavailable.
4. **Lightweight ETag/Last-Modified Check**: Add a lightweight ETag/Last-Modified check to prevent unnecessary re-fetches when the manifest hasn't changed.

**Synthesized Solution:**

To address the identified issues and incorporate the proposed solutions, the following steps can be taken:

1. **Create a Manifest Cache**: Develop a manifest cache system that stores the file list for each date folder. This can be achieved by creating a `manifest.json` file that contains the file list and is updated periodically.
2. **Implement CDN Bypass Loader**: Develop a CDN bypass loader that fetches data directly from public CDN URLs instead of going through the `/api/` endpoint.
3. **Offline-First Approach**: Implement an offline-first approach by using a locally cached manifest and CDN fetches when the API is throttled or unavailable.
4. **Add Lightweight ETag/Last-Modified Check**: Add a lightweight ETag/Last-Modified check to prevent unnecessary re-fetches when the manifest hasn't changed.
5. **Update Frontend Code**: Update the frontend code to use the manifest cache, CDN bypass loader, and offline-first approach.

**Example Code:**

To illustrate the synthesized solution, here's an example code snippet that demonstrates the implementation of the manifest cache, CDN bypass loader, and offline-first approach:
```javascript
// manifest.ts
const REPO = 'your-org/your-dataset-repo';
const DATE_PREFIX = 'batches/mirror-merged';
const CDN_ROOT = `https://huggingface.co/datasets/${REPO}/resolve/main`;

interface ManifestEntry {
  path: string;
  size: number;
  type: 'file' | 'dir';
}

const STORAGE_KEY = `hf-manifest:${REPO}:${DATE_PREFIX}`;
let embeddedManifest: ManifestEntry[] = [];

export function setEmbeddedManifest(data: ManifestEntry[]) {
  embeddedManifest = data;
}

export async function fetchManifest(dateFolder: string): Promise<ManifestEntry[]> {
  const path = `${DATE_PREFIX}/${dateFolder}`;
  try {
    const res = await fetch(`/api/hf/list?repo=${REPO}&path=${encodeURIComponent(path)}`);
    if (!res.ok) throw new Error('API failed');
    const tree = await res.json();
    const files = flattenTree(tree).filter((n) => n.type === 'file');
    localStorage.setItem(`${STORAGE_KEY}:${dateFolder}`, JSON.stringify(files));
    return files;
  } catch (err) {
    console.warn('HF API failed, using cached manifest', err);
    const cached = localStorage.getItem(`${STORAGE_KEY}:${dateFolder}`);
    return cached ? JSON.parse(cached) : embeddedManifest;
  }
}

export function getFileCDNUrl(path: string): string {
  return `${CDN_ROOT}/${path}`;
}
```

```javascript
// training.js
import { fetchManifest, getFileCDNUrl } from './manifest';

async function renderPreview() {
  const dateFolder = '2026-05-03';
  const manifest = await fetchManifest(dateFolder);
  const files = manifest.filter((file) => file.type === 'file');
  const dataset = await Promise.all(files.map((file) => {
    const url = getFileCDNUrl(file.path);
    return fetch(url).then((res) => res.json());
  }));
  renderTable(dataset);
}
```
This example code snippet demonstrates the implementation of the manifest cache, CDN bypass loader, and offline-first approach. The `fetchManifest` function fetches the manifest for a given date folder and stores it in local storage. The `getFileCDNUrl` function returns the CDN URL for a given file path. The `renderPreview` function uses the manifest cache and CDN bypass loader to fetch the dataset and render the preview table.

By synthesizing the best parts of multiple AI proposals and combining the strongest insights, we can create a more efficient and robust solution that addresses the identified issues and improves the overall user experience.
