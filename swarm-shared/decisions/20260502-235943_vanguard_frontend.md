# vanguard / frontend

## Final synthesized solution (chosen parts + corrections)

**Core diagnosis (merged, corrected)**
- Repeated HF repo enumeration on every training launch burns quota and risks 429.  
- No persisted per-date-folder manifest exists in the frontend layer, so training cannot run CDN-only and must re-query HF during data load.  
- Lightning Studio reuse is not surfaced; users create new studios and waste quota (no “attach to running” flow).  
- Idle-stop recovery is manual: if a studio stops, training dies and the user must notice and restart.  
- No visual indicator of CDN-bypass mode or last manifest generation time, so users can’t verify zero-API behavior.

**Chosen implementation scope**
- File: `/opt/axentx/vanguard/src/frontend/components/TrainingLauncher.vue` (main launcher).  
- Add “Generate HF manifest (date folder)” action, persist manifest to `src/frontend/manifests/`, wire training to consume manifest, add studio-reuse picker, and auto-recover idle-stopped studios.  
- Backend: minimal FastAPI endpoints to generate/read manifests and safely list HF folders.

**Key corrections vs contradictions**
- Use **non-recursive folder listing** for the date folder (one API call), then persist the file list. Do not recurse during generation; recurse only on-demand if deeper traversal is later required.  
- Manifest is a simple, deterministic JSON file keyed by date folder; do not embed large file metadata in frontend state.  
- Studio reuse picker only shows **Running** studios; “attach” means pass `studioId` to the launch API so backend can reuse the environment instead of creating a new one.  
- Auto-recovery: frontend monitors the studio after launch and issues a restart if status becomes `Stopped`. Keep polling interval conservative (60s) and clear on unmount.  
- CDN-bypass indicator: show last manifest generation time and file count so users can confirm zero-API mode.

---

## Frontend: TrainingLauncher.vue

```vue
<!-- /opt/axentx/vanguard/src/frontend/components/TrainingLauncher.vue -->
<template>
  <div class="training-launcher">
    <!-- Date folder selector -->
    <label>
      Date folder (YYYY-MM-DD):
      <input v-model="dateFolder" placeholder="2026-04-29" />
    </label>

    <!-- Manifest controls -->
    <div class="controls">
      <button @click="generateManifest" :disabled="manifestLoading || !dateFolder">
        {{ manifestLoading ? 'Generating...' : 'Generate HF manifest' }}
      </button>
      <span v-if="lastManifest" class="hint">
        Last manifest: {{ lastManifest }} ({{ manifestFileCount }} files)
      </span>
    </div>

    <!-- Studio reuse -->
    <label v-if="runningStudios.length">
      Reuse running studio:
      <select v-model="selectedStudioId">
        <option :value="null">New studio</option>
        <option v-for="s in runningStudios" :key="s.id" :value="s.id">
          {{ s.name }} ({{ s.machine }}) — {{ s.status }}
        </option>
      </select>
    </label>

    <!-- Launch -->
    <button @click="launchTraining" :disabled="!canLaunch">
      Launch training
    </button>

    <!-- Status -->
    <pre v-if="status" class="status">{{ status }}</pre>
  </div>
</template>

<script>
import axios from 'axios';

export default {
  name: 'TrainingLauncher',
  data() {
    return {
      dateFolder: '',
      manifestLoading: false,
      selectedStudioId: null,
      runningStudios: [],
      status: '',
      manifestCache: null,
      _pollInterval: null,
    };
  },
  computed: {
    canLaunch() {
      return this.dateFolder && this.manifestCache && Array.isArray(this.manifestCache.files) && this.manifestCache.files.length > 0;
    },
    manifestFileCount() {
      return (this.manifestCache && Array.isArray(this.manifestCache.files)) ? this.manifestCache.files.length : 0;
    },
    lastManifest() {
      if (!this.manifestCache || !this.manifestCache._generatedAt) return '';
      try {
        return new Date(this.manifestCache._generatedAt).toLocaleString();
      } catch {
        return '';
      }
    },
  },
  watch: {
    dateFolder: 'loadManifestForDate',
  },
  mounted() {
    this.fetchRunningStudios();
  },
  methods: {
    async fetchRunningStudios() {
      try {
        const res = await axios.get('/api/lightning/studios');
        this.runningStudios = (res.data || []).filter(s => s.status === 'Running');
      } catch (err) {
        console.warn('Could not fetch running studios', err);
        this.runningStudios = [];
      }
    },

    async loadManifestForDate() {
      if (!this.dateFolder) {
        this.manifestCache = null;
        return;
      }
      try {
        const res = await axios.get(`/api/manifest/${encodeURIComponent(this.dateFolder)}`);
        this.manifestCache = res.data;
      } catch {
        this.manifestCache = null;
      }
    },

    async generateManifest() {
      if (!this.dateFolder) return;
      this.manifestLoading = true;
      this.status = 'Listing HF folder (single API call)...';
      try {
        const res = await axios.post('/api/manifest/generate', {
          dateFolder: this.dateFolder,
        });
        this.manifestCache = res.data;
        this.status = `Manifest generated: ${res.data.files.length} files`;
      } catch (err) {
        this.status = `Error: ${err.message || err}`;
      } finally {
        this.manifestLoading = false;
      }
    },

    async launchTraining() {
      if (!this.canLaunch) return;
      this.status = 'Preparing training...';
      try {
        const payload = {
          manifest: this.manifestCache,
          dateFolder: this.dateFolder,
          studioId: this.selectedStudioId,
        };
        const res = await axios.post('/api/training/launch', payload);
        this.status = `Launched: ${res.data.runUrl || 'OK'}`;

        // Monitor and auto-recover studio if stopped
        if (res.data.studioId) {
          this.monitorStudio(res.data.studioId);
        }
      } catch (err) {
        this.status = `Launch failed: ${err.message || err}`;
      }
    },

    async monitorStudio(studioId) {
      if (!studioId) return;
      const poll = async () => {
        try {
          const res = await axios.get(`/api/lightning/studios/${studioId}`);
          if (res.data.status === 'Stopped') {
            this.status = 'Studio stopped — restarting...';
            await axios.post(`/api/lightning/studios/${studioId}/start`, {
              machine: res.data.machine || 'L40S',
            });
            this.status = 'Studio restarted.';
          } else {
            this.status = `Studio ${res.data.status}`;
          }
        } catch (err) {
          this.status = `Monitor error: ${err.message || err}`;
        }
      };
      this._pollInterval = setInterval(poll, 60000);
      await poll();
    },
  },
  beforeUnmount() {
    if (this._pollInterval) {
      clearInterval(this._pollInterval);
      this._pollInterval = null;
    }
  },
};
</script>

<style scoped>
.training-launcher { max-width: 720px; padding: 1rem; }
.controls { display:flex; gap:1rem; align-items:center; margin:0.5rem 0; }
.hint { color:#666; font-size:0.9rem; }
.status { background:#f6f6f6; padding:0.5rem; border-radius:4px; white-space:pre-wrap; }
</style>
```

---

## Backend: manifest endpoints (FastAPI)

```python
# /opt/axentx/vanguard/src/backend/api/manifest.py
import os
import json
from datetime import datetime, timezone
from fast
