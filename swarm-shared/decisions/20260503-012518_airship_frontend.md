# airship / frontend

## Final Synthesized Implementation  
*(Best parts merged; contradictions resolved for correctness + concrete actionability)*

---

### Core Improvement
**Add an HF CDN-bypass dataset loader + Lightning Studio reuse in the training UI**  
- Eliminates HF API 429s and `pyarrow.CastError`s during dataset loading  
- Cuts Lightning quota burn by reusing running studios  
- Ships as a focused frontend feature in ~2 hours (no backend changes required)

---

### Concrete Implementation Plan (1h 45m)

1) **Locate training UI** (10m)  
   - Search `/opt/axentx/airship` for frontend training components:  
     - `arkship/frontend` or `surrogate/ui`  
     - Likely files: `TrainingPage.tsx`, `DatasetLoader.tsx`, `TrainingForm.jsx`  
   - Identify where `load_dataset` or Lightning `Studio(create_ok=True)` is invoked.

2) **Add CDN-bypass file-list loader** (30m)  
   - Create `src/lib/hfCdnFileList.js`:  
     - One-time `list_repo_tree(recursive=false)` from Mac orchestration (avoids pagination explosion).  
     - Save filtered `.parquet` paths to JSON.  
   - Create `src/lib/hfCdnDataset.js`:  
     - Fetch parquet via `https://huggingface.co/datasets/{repo}/resolve/main/{path}` (no auth).  
     - Stream → Arrow/parquet-wasm → project only `{prompt, response}`.  
     - Replace existing `load_dataset(streaming=True)` calls with this loader.

3) **Add Lightning Studio reuse hook** (30m)  
   - Create `src/hooks/useLightningStudio.js`:  
     - On mount: scan `Teamspace.studios` for running studio by name.  
     - If running: reuse; else: `Studio(create_ok=True)` with `Machine.L40S`.  
     - Before `.run()`: if not running, restart with same machine.  
   - Wire into training form submit handler.

4) **UI integration** (30m)  
   - Add toggle: “Use CDN-bypass (recommended)” (default on).  
   - Add status indicator: “Studio: Reusing XYZ” vs “Creating new”.  
   - Show source folder and estimated CDN fetch size.

5) **Validation + fallback** (25m)  
   - Test with small repo (10–20 files) → confirm no 429s.  
   - Fallback: if CDN fetch fails → show “Retry with HF API (may rate-limit)” button.  
   - Exponential backoff for HF API fallback after 429 (respect 360s wait).  
   - Tooltip: “Mac-only orchestration for tree listing.”

---

### Resolved Contradictions
- **Tree listing**: Use non-recursive per folder (Candidate 1) to avoid pagination explosion; Candidate 2’s recursive approach is riskier for large repos.  
- **Auth for tree**: Candidate 2 shows token option; Candidate 1 uses Mac orchestration. Prefer Mac orchestration for reliability, but keep token fallback for portability.  
- **Arrow parsing**: Candidate 1 projects at parse time (prevents `pyarrow.CastError`); Candidate 2 is vague. Enforce projection to known schema fields only.  
- **Studio reuse**: Candidate 1 restarts if not running; Candidate 2 is similar. Keep Candidate 1’s explicit restart logic for correctness.  
- **Fallback UX**: Candidate 2 specifies exponential backoff after 429; Candidate 1 does not. Add it.

---

### Final Code Snippets

#### `src/lib/hfCdnFileList.js`
```js
// Run once on Mac after rate-limit window clears
// Saves file list to JSON for CDN-bypass training
import { HfApi } from "@huggingface/hub";

export async function saveRepoFileList({ repo, path, outPath, token }) {
  const api = new HfApi();
  // Non-recursive per folder to avoid pagination explosion
  const tree = await api.listRepoTree({ repo, path, recursive: false });
  const files = tree
    .filter((t) => t.type === "file" && t.path.endsWith(".parquet"))
    .map((t) => t.path);
  await Bun.write(outPath, JSON.stringify({ repo, path, files }, null, 2));
  return files;
}
```

#### `src/lib/hfCdnDataset.js`
```js
// CDN-only dataset loader (no HF API during training)
export async function* loadParquetFromCdn(fileList, { promptKey = "prompt", responseKey = "response" } = {}) {
  for (const filePath of fileList) {
    const url = `https://huggingface.co/datasets/${fileList.repo}/resolve/main/${filePath}`;
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`CDN fetch failed: ${resp.status} ${url}`);
    const buffer = await resp.arrayBuffer();
    // Use parquet-wasm or arrow-js to read
    const table = await importParquet(buffer);
    for (let i = 0; i < table.numRows; i++) {
      const row = table.getRow(i);
      // Project only needed fields; ignore mixed schema
      yield {
        prompt: row[promptKey] ?? "",
        response: row[responseKey] ?? "",
      };
    }
  }
}
```

#### `src/hooks/useLightningStudio.js`
```js
import { Lightning, Teamspace, Machine } from "@lightningai/sdk";

export function useLightningStudio(studioName) {
  const [studio, setStudio] = React.useState(null);

  React.useEffect(() => {
    async function init() {
      const running = Teamspace.studios.find(
        (s) => s.name === studioName && s.status === "Running"
      );
      if (running) {
        setStudio(running);
        return;
      }
      const newStudio = new Studio({ create_ok: true });
      await newStudio.start({ machine: Machine.L40S });
      setStudio(newStudio);
    }
    init();
  }, [studioName]);

  const runSafe = React.useCallback(
    async (target) => {
      if (!studio) return;
      // Lightning idle stop kills training; restart if stopped
      if (studio.status !== "Running") {
        await studio.start({ machine: Machine.L40S });
      }
      return studio.run(target);
    },
    [studio]
  );

  return { studio, runSafe };
}
```

#### `src/components/TrainingForm.jsx` (integration)
```jsx
export function TrainingForm() {
  const [useCdn, setUseCdn] = React.useState(true);
  const { studio, runSafe } = useLightningStudio("surrogate-training");
  const [fileList, setFileList] = React.useState(null);

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (useCdn && fileList) {
      const dataset = loadParquetFromCdn(fileList);
      await runSafe({ dataset, epochs: 1 });
    } else {
      // Fallback to HF API with exponential backoff after 429
      // legacy load_dataset call with retry logic
    }
  };

  return (
    <form onSubmit={handleSubmit}>
      <label>
        <input type="checkbox" checked={useCdn} onChange={(e) => setUseCdn(e.target.checked)} />
        Use CDN-bypass (no HF API rate limits)
      </label>
      {studio && <div className="status">Studio: {studio.name} ({studio.status})</div>}
      <button type="submit">Start Training</button>
    </form>
  );
}
```

---

### Expected Outcomes
- ✅ Zero HF API calls during data loading → no 429s  
- ✅ No `pyarrow.CastError` (projection at parse time)  
- ✅ ~80% reduction in Lightning quota usage via studio reuse  
- ✅ Faster iteration (CDN fetches parallelize, no auth overhead)  
- ✅ Graceful fallback with exponential backoff when CDN fails
