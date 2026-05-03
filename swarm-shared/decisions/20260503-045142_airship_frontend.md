# airship / frontend

## Final Implementation (Synthesized)

**Core insight**: Both candidates converge on the same critical path—**generate once, embed everywhere**. The winning strategy is a **TrainingManifestGenerator** that produces a self-contained `training-manifest.json` with CDN-resolved URLs, eliminating HF API calls during Lightning training and preventing quota waste via Studio status visibility.

---

### 1. Frontend Component: `/opt/axentx/airship/frontend/src/components/TrainingManifestGenerator.tsx`

```tsx
import { useState, useEffect } from 'react';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Loader2, Play, Check, AlertCircle, RefreshCw, Download } from 'lucide-react';

interface TrainingFile {
  path: string;
  size: number;
  type: 'file';
  sha256?: string;
}

interface StudioStatus {
  name: string;
  status: 'running' | 'stopped' | 'starting';
  machine: string;
  uptime?: number;
}

interface ManifestGeneration {
  repo: string;
  folder: string;
  files: TrainingFile[];
  generatedAt: string;
  cdnUrls: string[];
  totalSize: number;
  config: {
    batch_size: number;
    num_workers: number;
    pin_memory: boolean;
  };
}

export function TrainingManifestGenerator() {
  const [isGenerating, setIsGenerating] = useState(false);
  const [studioStatus, setStudioStatus] = useState<StudioStatus | null>(null);
  const [manifest, setManifest] = useState<ManifestGeneration | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    checkStudioStatus();
    const interval = setInterval(checkStudioStatus, 30000);
    return () => clearInterval(interval);
  }, []);

  const checkStudioStatus = async () => {
    try {
      const response = await fetch('/api/surrogate/studio/status');
      if (!response.ok) throw new Error('Status check failed');
      const data = await response.json();
      setStudioStatus(data);
    } catch (err) {
      console.error('Studio status unavailable:', err);
    }
  };

  const generateManifest = async () => {
    setIsGenerating(true);
    setError(null);

    try {
      const response = await fetch('/api/surrogate/training/manifest', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          repo: 'axentx/surrogate-datasets',
          folder: `batches/mirror-merged/${new Date().toISOString().split('T')[0]}`,
          strategy: 'cdn-bypass',
          format: 'parquet'
        })
      });

      if (!response.ok) {
        const err = await response.json();
        throw new Error(err.message || 'Manifest generation failed');
      }

      const data: ManifestGeneration = await response.json();
      setManifest(data);

      // Auto-download for Lightning Studio import
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `training-manifest-${Date.now()}.json`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      window.URL.revokeObjectURL(url);

    } catch (err) {
      setError(err instanceof Error ? err.message : 'Generation failed');
    } finally {
      setIsGenerating(false);
    }
  };

  const restartStudio = async () => {
    try {
      await fetch('/api/surrogate/studio/restart', { method: 'POST' });
      await new Promise(r => setTimeout(r, 3000));
      await checkStudioStatus();
    } catch (err) {
      setError('Studio restart failed');
    }
  };

  const isStudioHealthy = studioStatus?.status === 'running';

  return (
    <Card className="p-6 max-w-2xl">
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <h3 className="text-lg font-semibold">Surrogate-1 Training Pipeline</h3>
          {studioStatus && (
            <Badge variant={
              studioStatus.status === 'running' ? 'default' : 
              studioStatus.status === 'starting' ? 'secondary' : 'destructive'
            }>
              {studioStatus.status.toUpperCase()}
            </Badge>
          )}
        </div>

        {error && (
          <Alert variant="destructive">
            <AlertCircle className="h-4 w-4" />
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}

        {studioStatus?.status === 'stopped' && (
          <Alert>
            <AlertCircle className="h-4 w-4" />
            <AlertDescription>
              Lightning Studio is stopped. Training will fail. 
              <Button variant="link" onClick={restartStudio} className="p-0 h-auto ml-1">
                Restart now
              </Button>
            </AlertDescription>
          </Alert>
        )}

        <div className="flex gap-2">
          <Button 
            onClick={generateManifest}
            disabled={isGenerating || !isStudioHealthy}
            className="min-w-[180px]"
          >
            {isGenerating ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                Generating...
              </>
            ) : (
              <>
                <Download className="mr-2 h-4 w-4" />
                Generate Manifest
              </>
            )}
          </Button>
          
          <Button 
            variant="outline" 
            onClick={checkStudioStatus}
            disabled={isGenerating}
            size="sm"
          >
            <RefreshCw className="mr-2 h-4 w-4" />
            Refresh
          </Button>
        </div>

        {manifest && (
          <div className="bg-muted/50 p-4 rounded-lg space-y-3">
            <div className="flex items-center gap-2 text-sm">
              <Check className="h-4 w-4 text-green-600" />
              <span className="font-medium">Manifest Ready</span>
              <Badge variant="outline" className="ml-auto">
                {manifest.files.length} files
              </Badge>
            </div>
            
            <div className="grid grid-cols-2 gap-4 text-xs text-muted-foreground">
              <div>Total: {(manifest.totalSize / 1024 / 1024).toFixed(1)} MB</div>
              <div>CDN URLs: {manifest.cdnUrls.length}</div>
              <div>Generated: {new Date(manifest.generatedAt).toLocaleString()}</div>
              <div>Workers: {manifest.config.num_workers}</div>
            </div>

            <div className="text-xs bg-blue-50 text-blue-800 p-2 rounded border border-blue-200">
              <strong>Zero-API mode:</strong> Manifest embeds CDN URLs. Lightning training will fetch directly—no HuggingFace API calls during training.
            </div>
          </div>
        )}
      </div>
    </Card>
  );
}
```

---

### 2. Backend API: `/opt/axentx/airship/backend/src/routes/surrogate.ts`

```typescript
import { Router } from 'express';
import { HFDataIngestionService } from '../services/hf-ingestion';
import { LightningStudioService } from '../services/lightning-studio';

const router = Router();
const hfService = new HFDataIngestionService();
const studioService = new LightningStudioService();

/**
 * Generate training manifest with CDN bypass (zero API calls during training)
 * POST /api/surrogate/training/manifest
 */
router.post('/training/manifest', async (
