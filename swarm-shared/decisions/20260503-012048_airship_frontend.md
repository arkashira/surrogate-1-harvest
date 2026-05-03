# airship / frontend

## Highest-Value Incremental Improvement
**Implement HF CDN-bypass dataset loader + Lightning Studio reuse in the training UI**  
- Eliminates HF API 429s and `pyarrow.CastError`s during dataset loading  
- Reduces Lightning quota burn by reusing running studios instead of recreating  
- Ships in <2h as frontend-only change (no backend required)

---

## Implementation Plan

### 1. Locate training UI components
- Find frontend files related to surrogate training (likely under `surrogate/` or `airship/frontend/`)
- Identify dataset loader and Lightning studio creation logic

### 2. Add CDN-bypass dataset loader
- Replace `load_dataset(streaming=True)` calls with CDN direct fetches
- Pre-list file paths once via HF API (cached), then embed list in training script
- Use `https://huggingface.co/datasets/{repo}/resolve/main/{path}` for zero-auth downloads

### 3. Implement Lightning Studio reuse
- Before creating studio, list running studios via `Teamspace.studios`
- Reuse if name matches and status is "Running"
- Add idle-stop check before `.run()` calls with auto-restart on L40S

### 4. UI polish
- Add status indicators for CDN vs API mode
- Show studio reuse status
- Display estimated quota savings

---

## Code Snippets

### CDN-bypass dataset loader (to embed in training script)
```python
# training/cdn_dataset_loader.py
import json
import requests
from pathlib import Path
from typing import List, Dict

def load_file_list_cached(repo: str, date_folder: str, cache_path: str = "file_list.json") -> List[str]:
    """Single API call to list files, cached to JSON for CDN-only training."""
    cache = Path(cache_path)
    if cache.exists():
        return json.loads(cache.read_text())
    
    # This runs once from Mac orchestration script after rate-limit window
    from huggingface_hub import list_repo_tree
    files = list_repo_tree(repo, path=date_folder, recursive=False)
    file_paths = [f.rfilename for f in files if f.rfilename.endswith(('.parquet', '.jsonl'))]
    
    cache.write_text(json.dumps(file_paths, indent=2))
    return file_paths

def stream_from_cdn(repo: str, file_paths: List[str], project_fn=None):
    """Zero-API dataset streaming via HF CDN."""
    base_url = f"https://huggingface.co/datasets/{repo}/resolve/main"
    
    for path in file_paths:
        url = f"{base_url}/{path}"
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        
        # Project to {prompt, response} only at parse time
        if path.endswith('.parquet'):
            import pyarrow.parquet as pq
            from io import BytesIO
            table = pq.read_table(BytesIO(resp.content))
            # Extract only needed columns to avoid CastError
            for batch in table.to_batches():
                if project_fn:
                    yield project_fn(batch)
                else:
                    yield batch
```

### Lightning Studio reuse (frontend integration)
```javascript
// frontend/src/services/lightningService.js
import { Lightning } from 'lightning-ai-sdk';

export class LightningService {
  static async getOrCreateStudio(name, machine = 'L40S') {
    const teamspace = await Lightning.Teamspace.current();
    const studios = await teamspace.studios();
    
    // Reuse running studio
    const running = studios.find(s => 
      s.name === name && s.status === 'Running'
    );
    
    if (running) {
      console.log(`Reusing studio: ${name}`);
      return running;
    }
    
    // Create new if none running
    console.log(`Creating studio: ${name}`);
    return await teamspace.studios.create({
      name,
      machine,
      cloud: 'lightning-lambda-prod' // H200 available
    });
  }

  static async safeRun(studio, script, args = []) {
    // Check idle stop before running
    if (studio.status !== 'Running') {
      console.log('Studio stopped, restarting...');
      await studio.start({ machine: 'L40S' });
    }
    
    return await studio.run(script, args);
  }
}
```

### Training UI component with CDN mode toggle
```jsx
// frontend/src/components/TrainingPanel.jsx
import React, { useState, useEffect } from 'react';
import { LightningService } from '../services/lightningService';

export default function TrainingPanel() {
  const [cdnMode, setCdnMode] = useState(true);
  const [studio, setStudio] = useState(null);
  const [status, setStatus] = useState('idle');

  useEffect(() => {
    // Load cached file list for CDN mode
    if (cdnMode) {
      fetch('/training/file_list.json')
        .then(r => r.json())
        .then(files => console.log(`CDN files loaded: ${files.length}`));
    }
  }, [cdnMode]);

  const startTraining = async () => {
    setStatus('starting');
    
    try {
      const studio = await LightningService.getOrCreateStudio('surrogate-training');
      setStudio(studio);
      
      const script = cdnMode 
        ? 'training/train_cdn.py' 
        : 'training/train_api.py';
      
      await LightningService.safeRun(studio, script, [
        '--cdn-mode', cdnMode.toString()
      ]);
      
      setStatus('running');
    } catch (error) {
      setStatus('error');
      console.error('Training failed:', error);
    }
  };

  return (
    <div className="training-panel">
      <h2>Surrogate Training</h2>
      
      <div className="mode-selector">
        <label>
          <input 
            type="checkbox" 
            checked={cdnMode}
            onChange={e => setCdnMode(e.target.checked)}
          />
          CDN Bypass Mode (No HF API 429s)
        </label>
      </div>

      <div className="status">
        Studio: {studio ? studio.name : 'None'} | 
        Status: {status} |
        Reuse: {studio?.status === 'Running' ? '✅' : '❌'}
      </div>

      <button onClick={startTraining} disabled={status === 'running'}>
        {status === 'running' ? 'Training...' : 'Start Training'}
      </button>

      {cdnMode && (
        <div className="hint">
          💡 CDN mode bypasses HF API rate limits. 
          File list cached from single API call.
        </div>
      )}
    </div>
  );
}
```

### Mac orchestration script (for pre-listing files)
```bash
#!/bin/bash
# scripts/update-file-list.sh
# Run this from Mac after rate-limit window clears

set -e
export SHELL=/bin/bash

REPO="your-dataset-repo"
DATE_FOLDER="batches/mirror-merged/$(date +%Y-%m-%d)"
OUTPUT="frontend/public/training/file_list.json"

python3 -c "
from huggingface_hub import list_repo_tree
import json

files = list_repo_tree('$REPO', path='$DATE_FOLDER', recursive=False)
file_paths = [f.rfilename for f in files if f.rfilename.endswith(('.parquet', '.jsonl'))]

with open('$OUTPUT', 'w') as f:
    json.dump(file_paths, f, indent=2)

print(f'Cached {len(file_paths)} files for CDN mode')
"
```

---

## Deployment Steps (<2h)

1. **Add CDN loader** (15 min): Create `training/cdn_dataset_loader.py`
2. **Update Lightning service** (20 min): Add studio reuse logic
3. **Modify training UI** (30 min): Add CDN toggle and status display
4. **Add orchestration script** (10 min): `update-file-list.sh` for Mac
5. **Test** (30 min): Verify CDN mode works and studio reuse
6. **Deploy** (15 min): Update frontend build

**Total: ~2 hours**
