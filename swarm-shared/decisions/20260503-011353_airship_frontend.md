# airship / frontend

## Analysis & Highest-Value Incremental Improvement

Based on the repo context (Arkship + Surrogate AI platform) and past patterns, the highest-value frontend improvement that can ship in <2h is:

**Implement HF CDN-bypass dataset loader UI with schema-safe projection and Lightning Studio integration status**

This directly addresses the recurring HF API 429 and `pyarrow.CastError` patterns while providing immediate user value for the Surrogate AI training pipeline.

## Implementation Plan

### 1. Create Dataset Loader Component (45 min)
- Location: `/opt/axentx/airship/frontend/src/components/DatasetLoader.jsx`
- Features: CDN URL construction, file list management, schema projection preview

### 2. Add Lightning Studio Status Panel (30 min)
- Location: `/opt/axentx/airship/frontend/src/components/StudioStatus.jsx`
- Features: Running status check, quota display, idle-stop warning

### 3. Integrate with Training Pipeline View (30 min)
- Location: `/opt/axentx/airship/frontend/src/views/TrainingPipeline.jsx`
- Features: Unified UI for dataset selection + studio management

### 4. Add Utility Functions (15 min)
- Location: `/opt/axentx/airship/frontend/src/utils/hf-cdn.js`
- Features: CDN URL builder, file list caching, schema projection

## Code Implementation

### 1. HF CDN Utility (`/opt/axentx/airship/frontend/src/utils/hf-cdn.js`)

```javascript
/**
 * HF CDN Bypass Utilities
 * Bypasses HF API rate limits by using CDN URLs directly
 * Pattern: HF CDN Bypass (2026-04-29)
 */

export const HF_CDN_BASE = 'https://huggingface.co/datasets';

/**
 * Build CDN URL for a dataset file (bypasses API auth)
 * @param {string} repo - Dataset repo (e.g., "username/dataset")
 * @param {string} filePath - Path to file in repo
 * @param {string} [revision='main'] - Git revision
 * @returns {string} CDN URL
 */
export const buildCdnUrl = (repo, filePath, revision = 'main') => {
  return `${HF_CDN_BASE}/${repo}/resolve/${revision}/${filePath}`;
};

/**
 * Generate manifest of files for CDN-only loading
 * Pre-lists files once to avoid API calls during training
 * @param {Array} fileTree - Repository tree from list_repo_tree
 * @param {string} dateFolder - Date folder path (e.g., "2026-04-29")
 * @returns {Object} Manifest with CDN URLs and metadata
 */
export const generateCdnManifest = (fileTree, dateFolder) => {
  const manifest = {
    generatedAt: new Date().toISOString(),
    dateFolder,
    files: [],
    totalFiles: 0,
    schemaProjection: { prompt: null, response: null }
  };

  // Filter to parquet files in date folder (schema-safe projection)
  const parquetFiles = fileTree.filter(file => 
    file.path.startsWith(dateFolder) && 
    file.path.endsWith('.parquet') &&
    !file.path.includes('enriched/') // Skip enriched mixed-schema files
  );

  manifest.files = parquetFiles.map(file => ({
    path: file.path,
    cdnUrl: buildCdnUrl(file.repo || 'dataset/repo', file.path),
    size: file.size,
    // Extract slug for deterministic repo selection (HF commit cap pattern)
    slug: file.path.split('/').pop().replace('.parquet', ''),
    // Determine sibling repo (0-5) for load distribution
    siblingRepo: hashSlugToRepo(file.path)
  }));

  manifest.totalFiles = manifest.files.length;
  return manifest;
};

/**
 * Deterministic sibling repo selection (HF commit cap pattern)
 * Distributes writes across N sibling repos to avoid 128/hr limit
 * @param {string} slug - File slug
 * @param {number} [siblingCount=5] - Number of sibling repos
 * @returns {number} Sibling repo index (0 to siblingCount)
 */
export const hashSlugToRepo = (slug, siblingCount = 5) => {
  let hash = 0;
  for (let i = 0; i < slug.length; i++) {
    hash = ((hash << 5) - hash) + slug.charCodeAt(i);
    hash = hash & hash;
  }
  return Math.abs(hash) % (siblingCount + 1);
};

/**
 * Cache file list to avoid repeated API calls
 * @param {string} cacheKey - Cache key (e.g., "repo-dateFolder")
 * @param {Object} manifest - Generated manifest
 */
export const cacheFileList = (cacheKey, manifest) => {
  try {
    localStorage.setItem(
      `hf-cdn-cache-${cacheKey}`,
      JSON.stringify({
        ...manifest,
        cachedAt: new Date().toISOString()
      })
    );
  } catch (error) {
    console.warn('Failed to cache file list:', error);
  }
};

/**
 * Retrieve cached file list
 * @param {string} cacheKey - Cache key
 * @param {number} [maxAgeHours=24] - Cache validity in hours
 * @returns {Object|null} Cached manifest or null
 */
export const getCachedFileList = (cacheKey, maxAgeHours = 24) => {
  try {
    const cached = localStorage.getItem(`hf-cdn-cache-${cacheKey}`);
    if (!cached) return null;

    const { cachedAt, ...manifest } = JSON.parse(cached);
    const age = Date.now() - new Date(cachedAt).getTime();
    const maxAgeMs = maxAgeHours * 60 * 60 * 1000;

    return age < maxAgeMs ? manifest : null;
  } catch (error) {
    console.warn('Failed to retrieve cached file list:', error);
    return null;
  }
};
```

### 2. Dataset Loader Component (`/opt/axentx/airship/frontend/src/components/DatasetLoader.jsx`)

```jsx
import React, { useState, useEffect, useCallback } from 'react';
import { 
  buildCdnUrl, 
  generateCdnManifest, 
  cacheFileList, 
  getCachedFileList 
} from '../utils/hf-cdn';
import { Alert, Button, Card, List, Space, Tag, Typography, Spin, Input } from 'antd';
import { 
  CloudDownloadOutlined, 
  FileTextOutlined, 
  ReloadOutlined,
  InfoCircleOutlined 
} from '@ant-design/icons';

const { Text, Title } = Typography;

/**
 * Dataset Loader with HF CDN Bypass
 * Pattern: HF CDN Bypass + Schema-safe projection
 */
const DatasetLoader = ({ 
  repoName, 
  onFilesSelected, 
  dateFolder,
  disabled = false 
}) => {
  const [fileTree, setFileTree] = useState([]);
  const [manifest, setManifest] = useState(null);
  const [selectedFiles, setSelectedFiles] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [searchTerm, setSearchTerm] = useState('');

  const cacheKey = `${repoName}-${dateFolder}`;

  // Load cached manifest or fetch fresh
  const loadFileList = useCallback(async () => {
    setLoading(true);
    setError(null);

    try {
      // Check cache first (Pattern: pre-list file paths once)
      const cached = getCachedFileList(cacheKey, 24);
      if (cached) {
        setManifest(cached);
        setFileTree(cached.files);
        setLoading(false);
        return;
      }

      // Simulate API call to list_repo_tree (in real app, call backend)
      // This would be: await fetch(`/api/hf/tree?repo=${repoName}&path=${dateFolder}`)
      const mockTree = generateMockFileTree(repoName, dateFolder);
      setFileTree(mockTree);

      // Generate CDN manifest
      const newManifest = generateCdnManifest(mockTree, dateFolder);
      setManifest(newManifest);
      
      // Cache for future use
      cacheFileList(cacheKey, newManifest);
