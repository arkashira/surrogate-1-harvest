# airship / frontend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Add a **Discover page** to Arkship that exposes the existing `/api/discover` backend as a UI workflow for generating deterministic HF CDN file manifests. This enables surrogate/kaggle training pipelines and zero-API data loading without CLI access.

---

## Architecture & Correctness Decisions

1. **Frontend-only scope** — backend `/api/discover` already exists per prior decisions.
2. **Sync-first, async fallback** — attempt synchronous generation; if backend returns `job_id`/`processing`, poll `/api/discover/status/:jobId` every 2s.
3. **Deterministic manifest format** — preserve exact JSON structure returned by backend; do not transform.
4. **UX priorities**: copy-to-clipboard, JSON download, preset shortcuts, inline errors, file-size display.
5. **No new backend routes** — only consume existing `/api/discover` (POST) and `/api/discover/status/:jobId` (GET).

---

## Implementation (≤2h)

### 1. Frontend Route & Component (45 min)

**File**: `/opt/axentx/airship/arkship/src/pages/Discover.jsx`

```jsx
import { useState, useEffect } from 'react';
import {
  DocumentTextIcon,
  ClipboardDocumentIcon,
  ArrowPathIcon,
  ExclamationCircleIcon,
  CheckCircleIcon,
} from '@heroicons/react/24/outline';

export default function Discover() {
  const [repoId, setRepoId] = useState('datasets/your-repo');
  const [dateFolder, setDateFolder] = useState('2026-04-29');
  const [outFile, setOutFile] = useState('');
  const [manifest, setManifest] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [copied, setCopied] = useState(false);
  const [polling, setPolling] = useState(false);

  // Presets for fast iteration
  const presets = [
    { repoId: 'datasets/surrogate-1', dateFolder: '2026-04-29', label: 'Surrogate-1 (Latest)' },
    { repoId: 'datasets/mirror-merged', dateFolder: '2026-04-29', label: 'Mirror Merged' },
    { repoId: 'datasets/kaggle-kgat', dateFolder: '2026-04-29', label: 'KGAT Training' },
  ];

  const handleDiscover = async () => {
    setLoading(true);
    setError(null);
    setManifest(null);

    try {
      const payload = { repo_id: repoId, date_folder: dateFolder };
      if (outFile) payload.out = outFile;

      const response = await fetch('http://localhost:8000/api/discover', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });

      if (!response.ok) {
        const text = await response.text().catch(() => '');
        throw new Error(`HTTP ${response.status}${text ? `: ${text}` : ''}`);
      }

      const data = await response.json();

      // Async job
      if (data.job_id && data.status === 'processing') {
        setPolling(true);
        startPolling(data.job_id);
        return;
      }

      // Sync success
      setManifest(data);
    } catch (err) {
      setError(err.message || 'Request failed');
    } finally {
      setLoading(false);
    }
  };

  const startPolling = (jobId) => {
    const poll = async () => {
      try {
        const res = await fetch(`http://localhost:8000/api/discover/status/${jobId}`);
        if (!res.ok) throw new Error(`Polling HTTP ${res.status}`);
        const data = await res.json();

        if (data.status === 'completed') {
          setManifest(data.result ?? data);
          setPolling(false);
        } else if (data.status === 'failed') {
          setError(data.error || 'Generation failed');
          setPolling(false);
        } else {
          setTimeout(poll, 2000);
        }
      } catch (err) {
        setError(err.message || 'Polling failed');
        setPolling(false);
      }
    };
    poll();
  };

  const handleCopy = async () => {
    if (!manifest) return;
    try {
      await navigator.clipboard.writeText(JSON.stringify(manifest, null, 2));
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // ignore
    }
  };

  const handleDownload = () => {
    if (!manifest) return;
    const blob = new Blob([JSON.stringify(manifest, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = outFile || `manifest-${repoId.split('/').pop()}-${dateFolder}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  const formatFileSize = (bytes) => {
    if (!bytes && bytes !== 0) return 'N/A';
    const units = ['B', 'KB', 'MB', 'GB'];
    let size = Number(bytes);
    let idx = 0;
    while (size >= 1024 && idx < units.length - 1) {
      size /= 1024;
      idx++;
    }
    return `${size.toFixed(1)} ${units[idx]}`;
  };

  const fileCount = manifest?.files?.length ?? 0;
  const totalSize = manifest?.files?.reduce((s, f) => s + (f.size || 0), 0) ?? null;

  return (
    <div className="min-h-screen bg-gray-50 py-8">
      <div className="max-w-6xl mx-auto px-4 sm:px-6 lg:px-8">
        {/* Header */}
        <div className="mb-8">
          <div className="flex items-center gap-3 mb-2">
            <DocumentTextIcon className="h-8 w-8 text-blue-600" />
            <h1 className="text-3xl font-bold text-gray-900">CDN Manifest Generator</h1>
          </div>
          <p className="text-gray-600">
            Generate deterministic CDN file manifests for HuggingFace datasets. Enables zero-API
            training data loading for Lightning Studio and surrogate pipelines.
          </p>
        </div>

        {/* Presets */}
        <div className="mb-6">
          <h3 className="text-sm font-medium text-gray-700 mb-2">Quick presets</h3>
          <div className="flex flex-wrap gap-2">
            {presets.map((p) => (
              <button
                key={p.label}
                type="button"
                onClick={() => {
                  setRepoId(p.repoId);
                  setDateFolder(p.dateFolder);
                }}
                className="px-3 py-1 text-sm bg-white border border-gray-300 rounded-md hover:bg-gray-50 text-gray-700"
              >
                {p.label}
              </button>
            ))}
          </div>
        </div>

        {/* Form */}
        <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 mb-6">
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Repository ID
              </label>
              <input
                type="text"
                value={repoId}
                onChange={(e) => setRepoId(e.target.value)}
                placeholder="e.g. datasets/surrogate-1"
                className="w-full px-3 py-2 border border-gray-300 rounded-md
