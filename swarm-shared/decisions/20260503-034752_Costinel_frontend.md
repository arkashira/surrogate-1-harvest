# Costinel / frontend

## Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Add a non-blocking Top-Hub Signal Panel to Costinel frontend that surfaces the most-connected hub (e.g., "MOC") using CDN-first data baked at build time (zero HuggingFace API calls at runtime).

### Architecture (CDN-first)
- **Build step** (local/mac): `scripts/bake-top-hub.js` calls `list_repo_tree` once (after rate-limit window), saves `public/data/top-hub.json`
- **Runtime**: Dashboard loads `/data/top-hub.json` via CDN (no auth, no API)
- **UI**: Collapsible signal card on dashboard sidebar with hub name, connection count, and quick links to related docs

### Steps (≤2h)
1. Create `public/data/` directory and add `.gitkeep`
2. Add `scripts/bake-top-hub.js` (mac orchestration only)
3. Add `src/components/TopHubSignalPanel.jsx`
4. Wire into `src/pages/Dashboard.jsx`
5. Update `package.json` scripts: `"bake:top-hub": "node scripts/bake-top-hub.js"`
6. Run bake script and commit generated JSON

---

## 1) Create data directory

```bash
mkdir -p /opt/axentx/Costinel/public/data
touch /opt/axentx/Costinel/public/data/.gitkeep
```

---

## 2) Bake script (mac orchestration)

`/opt/axentx/Costinel/scripts/bake-top-hub.js`

```js
#!/usr/bin/env node
/**
 * Bake top-hub signal data (CDN-first).
 * Run from mac after rate-limit window clears.
 * Uses HF API once to list folder, then writes public/data/top-hub.json
 * Runtime dashboard fetches via CDN (no auth).
 */

import { HfApi } from "huggingface-hub";
import fs from "fs/promises";
import path from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const REPO = "axentx/knowledge-rag"; // adjust if different
const FOLDER = "top-hub"; // folder with hub artifacts
const OUT_PATH = path.resolve(__dirname, "..", "public", "data", "top-hub.json");

async function bake() {
  const api = new HfApi();
  try {
    // Single API call: list folder (non-recursive)
    const tree = await api.listRepoTree(REPO, { path: FOLDER, recursive: false });

    // Pick most recent file by name (assumes date prefix) or use first
    const files = tree
      .filter((t) => t.type === "file" && t.path.endsWith(".json"))
      .sort((a, b) => b.path.localeCompare(a.path));

    if (files.length === 0) {
      throw new Error(`No JSON files found in ${REPO}/${FOLDER}`);
    }

    const latestPath = files[0].path;
    // CDN URL (no auth)
    const cdnUrl = `https://huggingface.co/datasets/${REPO}/resolve/main/${latestPath}`;

    // Fetch file content via CDN (bypasses API rate limits)
    const res = await fetch(cdnUrl);
    if (!res.ok) throw new Error(`CDN fetch failed: ${res.status}`);
    const hubData = await res.json();

    // Normalize to minimal shape
    const baked = {
      hub: hubData.hub || hubData.name || "MOC",
      connections: Number(hubData.connections || hubData.degree || 0),
      description: hubData.description || "Top-connected hub in knowledge graph",
      relatedDocs: (hubData.related || []).slice(0, 6),
      updatedAt: hubData.updatedAt || new Date().toISOString(),
      sourceFile: latestPath,
      cdnUrl,
    };

    await fs.writeFile(OUT_PATH, JSON.stringify(baked, null, 2), "utf-8");
    console.log("✅ Baked top-hub data to", OUT_PATH);
    console.log(JSON.stringify(baked, null, 2));
  } catch (err) {
    console.error("❌ Bake failed:", err);
    process.exit(1);
  }
}

bake();
```

Make executable:

```bash
chmod +x /opt/axentx/Costinel/scripts/bake-top-hub.js
```

---

## 3) TopHubSignalPanel component

`/opt/axentx/Costinel/src/components/TopHubSignalPanel.jsx`

```jsx
import { useEffect, useState } from "react";
import { ExternalLink, Info, BookOpen } from "lucide-react";

export default function TopHubSignalPanel() {
  const [hub, setHub] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // CDN-first fetch (no auth)
    fetch("/data/top-hub.json", { cache: "no-store" })
      .then((r) => {
        if (!r.ok) throw new Error("Failed to load hub data");
        return r.json();
      })
      .then(setHub)
      .catch((e) => console.warn(e))
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div className="w-full max-w-sm rounded-lg border border-gray-200 bg-white/50 p-4 animate-pulse">
        <div className="h-4 w-24 bg-gray-200 rounded mb-2"></div>
        <div className="h-3 w-32 bg-gray-200 rounded"></div>
      </div>
    );
  }

  if (!hub) return null;

  return (
    <div className="w-full max-w-sm rounded-lg border border-amber-200 bg-gradient-to-br from-amber-50 to-orange-50 p-4 shadow-sm">
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-2">
          <Info className="h-5 w-5 text-amber-600" />
          <span className="font-semibold text-gray-800">Top-Hub Signal</span>
        </div>
        <a
          href={hub.cdnUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="text-xs text-gray-400 hover:text-gray-600"
          title="Source file"
        >
          <ExternalLink className="h-3 w-3" />
        </a>
      </div>

      <div className="mt-3">
        <div className="text-xl font-bold text-gray-900">{hub.hub}</div>
        <div className="text-sm text-gray-600">
          {hub.connections.toLocaleString()} connections
        </div>
        {hub.description && (
          <p className="mt-1 text-sm text-gray-600">{hub.description}</p>
        )}
      </div>

      {hub.relatedDocs && hub.relatedDocs.length > 0 && (
        <div className="mt-3">
          <div className="flex items-center gap-1 text-xs font-medium text-gray-500 mb-1">
            <BookOpen className="h-3 w-3" />
            Related docs
          </div>
          <ul className="space-y-1">
            {hub.relatedDocs.map((doc, i) => (
              <li key={i}>
                <a
                  href={doc.url || "#"}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-xs text-amber-700 hover:underline truncate block"
                >
                  {doc.title || doc.slug || `Doc ${i + 1}`}
                </a>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="mt-3 pt-2 text-xs text-gray-400 border-t border-amber-200">
        Updated {new Date(hub.updatedAt).toLocaleDateString()}
      </div>
    </div>
  );
}
```

---

##
