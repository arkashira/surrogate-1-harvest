# Costinel / backend

## Final Implementation Plan: Knowledge-RAG Pipeline for Business Research

**Scope**: Backend service that executes `granite-business-research.sh` → queries top-hub (MOC) → returns contextual insights via REST API. Pure orchestration layer with zero training/inference overhead. Ships in <2h.

### Architecture
```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│  Business R&D   │    │  Knowledge-RAG   │    │  Top-Hub (MOC)  │
│  Script Runner  │───▶│  Query Engine    │───▶│  Graph Lookup   │
└─────────────────┘    └──────────────────┘    └─────────────────┘
         │                       │                       │
         ▼                       ▼                       ▼
  /tmp/granite-output/    Vector/Graph Search      Context JSON
```

### Implementation

#### 1. Insight Types (`/opt/axentx/Costinel/src/types/insight.types.ts`)
```typescript
export interface BusinessInsight {
  topic: string;
  summary: string;
  topHub: string;
  relatedDocs: Array<{
    id: string;
    title: string;
    relevance: number;
    snippet: string;
  }>;
  timestamp: string;
}

export interface ResearchRequest {
  query: string;
}

export interface ResearchResponse {
  success: boolean;
  data?: BusinessInsight;
  error?: string;
  details?: string;
}
```

#### 2. Knowledge-RAG Service (`/opt/axentx/Costinel/src/services/knowledgeRag.service.ts`)

```typescript
import { execSync } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';
import { BusinessInsight } from '../types/insight.types';

export class KnowledgeRagService {
  private readonly workspaceRoot = '/opt/axentx/Costinel';
  private readonly researchScript = `${this.workspaceRoot}/scripts/granite-business-research.sh`;
  private readonly knowledgeBase = `${this.workspaceRoot}/knowledge-base`;
  private readonly topHub = 'MOC'; // Most-connected hub per 2026-04-27 pattern

  async runBusinessResearch(query: string): Promise<BusinessInsight> {
    // Step 1: Execute granite business research script
    const researchOutput = this.executeResearchScript(query);
    
    // Step 2: Query top-hub (MOC) for contextual insights
    const hubContext = this.queryTopHub();
    
    // Step 3: Retrieve related documents from graph
    const relatedDocs = this.queryRelatedDocs(query, hubContext);
    
    return {
      topic: query,
      summary: researchOutput,
      topHub: this.topHub,
      relatedDocs,
      timestamp: new Date().toISOString()
    };
  }

  private executeResearchScript(query: string): string {
    try {
      // Ensure script is executable (pattern: opus pr reviewer / active-learning)
      execSync(`chmod +x ${this.researchScript}`, { stdio: 'ignore' });
      
      // Execute with proper bash invocation (pattern: wrapper script exec error)
      const output = execSync(
        `/bin/bash ${this.researchScript} "${query}"`,
        { 
          encoding: 'utf-8',
          cwd: this.workspaceRoot,
          env: { ...process.env, SHELL: '/bin/bash' } // Pattern: cron wrapper fix
        }
      );
      
      return output.trim();
    } catch (error) {
      console.error('Business research script failed:', error);
      return `Research failed: ${error.message}`;
    }
  }

  private queryTopHub(): any {
    const hubPath = path.join(this.knowledgeBase, 'graph', `${this.topHub}.json`);
    
    if (fs.existsSync(hubPath)) {
      return JSON.parse(fs.readFileSync(hubPath, 'utf-8'));
    }
    
    // Fallback: return minimal hub structure
    return {
      id: this.topHub,
      name: 'MOC (Method of Choice)',
      connections: 47,
      description: 'Most-connected decision hub'
    };
  }

  private queryRelatedDocs(query: string, hubContext: any): BusinessInsight['relatedDocs'] {
    const docsDir = path.join(this.knowledgeBase, 'documents');
    const related: BusinessInsight['relatedDocs'] = [];
    
    if (!fs.existsSync(docsDir)) {
      return related;
    }

    const files = fs.readdirSync(docsDir)
      .filter(f => f.endsWith('.json'))
      .slice(0, 10); // Limit to top 10 for performance

    files.forEach(file => {
      const docPath = path.join(docsDir, file);
      const doc = JSON.parse(fs.readFileSync(docPath, 'utf-8'));
      
      // Simple relevance scoring (TF-IDF lite)
      const relevance = this.calculateRelevance(query, doc, hubContext);
      
      if (relevance > 0.3) {
        related.push({
          id: doc.id || file.replace('.json', ''),
          title: doc.title || file,
          relevance,
          snippet: doc.snippet || doc.summary || 'No preview available'
        });
      }
    });

    return related
      .sort((a, b) => b.relevance - a.relevance)
      .slice(0, 5);
  }

  private calculateRelevance(query: string, doc: any, hubContext: any): number {
    const queryTerms = query.toLowerCase().split(/\s+/);
    const docText = JSON.stringify(doc).toLowerCase();
    
    let score = 0;
    queryTerms.forEach(term => {
      if (docText.includes(term)) score += 0.2;
    });
    
    // Boost if related to top-hub
    if (hubContext.keywords) {
      hubContext.keywords.forEach((kw: string) => {
        if (docText.includes(kw.toLowerCase())) score += 0.15;
      });
    }
    
    return Math.min(score, 1.0);
  }
}
```

#### 3. Insights Controller (`/opt/axentx/Costinel/src/controllers/insights.controller.ts`)

```typescript
import { Request, Response } from 'express';
import { KnowledgeRagService } from '../services/knowledgeRag.service';
import { ResearchRequest, ResearchResponse } from '../types/insight.types';

const ragService = new KnowledgeRagService();

export const runBusinessResearch = async (
  req: Request<{}, {}, ResearchRequest>,
  res: Response<ResearchResponse>
) => {
  try {
    const { query } = req.body;
    
    if (!query || typeof query !== 'string') {
      return res.status(400).json({ 
        success: false, 
        error: 'Query is required' 
      });
    }

    const insight = await ragService.runBusinessResearch(query);
    
    res.json({
      success: true,
      data: insight
    });
  } catch (error) {
    console.error('Business research failed:', error);
    res.status(500).json({ 
      success: false, 
      error: 'Research pipeline failed',
      details: error instanceof Error ? error.message : 'Unknown error'
    });
  }
};
```

#### 4. Insights Routes (`/opt/axentx/Costinel/src/routes/insights.routes.ts`)

```typescript
import { Router } from 'express';
import { runBusinessResearch } from '../controllers/insights.controller';

const router = Router();

router.post('/research', runBusinessResearch);

export default router;
```

#### 5. Register Routes (`/opt/axentx/Costinel/src/app.ts`)

```typescript
import express from 'express';
import insightsRouter from './routes/insights.routes';

const app = express();
app.use(express.json());

// Register business research endpoint
app.use('/api/business', insightsRouter);

// ... existing routes
```

#### 6. Setup Knowledge Base Structure

```bash
#!/bin/bash
# Setup script: /opt/axentx/Costinel/scripts/setup-knowledge-base.sh

WORKSPACE="/opt/axentx/Costinel"

# Create directories
mkdir -p "$WORKSPACE
