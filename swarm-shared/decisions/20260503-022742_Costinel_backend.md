# Costinel / backend

## Final Synthesized Implementation

I merged both proposals, kept the strongest parts, and resolved contradictions in favor of **correctness + concrete actionability**.

### Key Decisions & Resolutions

| Contradiction | Resolution |
|---------------|------------|
| **Payload shape**: Candidate 2 wanted `cdnPayloadUrl` and `description` on proposals; Candidate 1 wanted simpler fields. | Use Candidate 1’s shape (`rationale`, `priority`, `signal`) for the **immediate API response** because it’s concrete, UI-ready, and avoids extra network hops. Keep `cdnPayloadUrl` as an **optional extra field** only if you need lazy-loaded detail payloads later. |
| **File naming**: Candidate 1 used `{hubId}.json` (e.g., `MOC.json`); Candidate 2 implied `top-hub.json`. | Use `top-hub.json` as the **canonical default**, but allow `?hubId=` to load `{hubId}.json` if present. This is safer (no accidental file writes) and matches Candidate 1’s flexibility. |
| **Caching**: Candidate 1 used ETag/Last-Modified; Candidate 2 emphasized CDN-first. | Use explicit `Cache-Control` + ETag. Serve file from `public/knowledge/` so CDN (or reverse proxy) caches it automatically. No runtime DB/ML. |
| **Error handling**: Candidate 1 threw 404 on missing file; Candidate 2 implied graceful fallback. | Keep 404 for missing file (correctness). Log structured errors. Do **not** fallback to another hub silently—fail visibly so CI/ops can fix the file. |
| **Tests**: Both included tests. | Keep Candidate 1’s Jest example; it’s complete and minimal. |

---

## 1) Static metadata (canonical)

`public/knowledge/top-hub.json`
```json
{
  "hubId": "MOC",
  "hubLabel": "MOC",
  "hubDescription": "Master operational change — central hub for cross-cloud cost governance proposals.",
  "proposals": [
    {
      "id": "MOC-001",
      "title": "Standardize RI purchase cadence to quarterly",
      "signal": "high",
      "rationale": "Reduces unused reservation coverage and aligns with fiscal planning.",
      "priority": 1
    },
    {
      "id": "MOC-002",
      "title": "Enforce tag compliance for production accounts",
      "signal": "medium",
      "rationale": "Improves cost allocation accuracy and anomaly detection.",
      "priority": 2
    },
    {
      "id": "MOC-003",
      "title": "Introduce idle-resource detection pipeline",
      "signal": "medium",
      "rationale": "Surfaces low-utilization resources for safe rightsizing.",
      "priority": 3
    }
  ]
}
```

> Note: If you want per-hub files (e.g., `MOC.json`), place them in the same folder and request via `?hubId=MOC`.

---

## 2) DTOs

`src/modules/signals/dto/top-hub.dto.ts`
```ts
export type SignalLevel = 'low' | 'medium' | 'high';

export interface TopHubProposalDto {
  id: string;
  title: string;
  signal: SignalLevel;
  rationale: string;
  priority: number;
  // Optional: only include if you need lazy detail payloads
  cdnPayloadUrl?: string;
}

export interface TopHubSignalDto {
  hubId: string;
  hubLabel: string;
  hubDescription: string;
  proposals: TopHubProposalDto[];
}
```

---

## 3) Service

`src/modules/signals/services/top-hub-signal.service.ts`
```ts
import { Injectable, Logger } from '@nestjs/common';
import { readFile } from 'fs/promises';
import { join } from 'path';
import { TopHubSignalDto } from '../dto/top-hub.dto';

@Injectable()
export class TopHubSignalService {
  private readonly logger = new Logger(TopHubSignalService.name);
  private readonly basePath = join(process.cwd(), 'public', 'knowledge');

  async getTopHub(hubId = 'top-hub'): Promise<TopHubSignalDto> {
    // Normalize filename: top-hub.json for default, or {hubId}.json if provided
    const safeId = hubId === 'top-hub' ? 'top-hub' : hubId.toLowerCase();
    const filePath = join(this.basePath, `${safeId}.json`);

    try {
      const raw = await readFile(filePath, 'utf-8');
      const parsed = JSON.parse(raw) as TopHubSignalDto;

      // Basic schema validation
      if (!parsed?.hubId || !Array.isArray(parsed.proposals)) {
        throw new Error('Invalid top-hub file schema');
      }

      // Ensure proposals are sorted by priority ascending
      parsed.proposals.sort((a, b) => a.priority - b.priority);

      return parsed;
    } catch (err: any) {
      this.logger.error(`Failed to load top-hub file for hub=${hubId}`, err.message);
      throw err;
    }
  }
}
```

---

## 4) Controller

`src/modules/signals/controllers/top-hub.controller.ts`
```ts
import { Controller, Get, Query, Res, HttpException, HttpStatus, NotFoundException } from '@nestjs/common';
import { TopHubSignalService } from '../services/top-hub-signal.service';
import { TopHubSignalDto } from '../dto/top-hub.dto';
import { Response } from 'express';

@Controller('api/signals')
export class TopHubController {
  constructor(private readonly service: TopHubSignalService) {}

  @Get('top-hub')
  async topHub(
    @Query('hubId') hubId: string,
    @Res({ passthrough: true }) res: Response,
  ): Promise<TopHubSignalDto> {
    try {
      const payload = await this.service.getTopHub(hubId || 'top-hub');

      // CDN-friendly caching
      res.set({
        'Cache-Control': 'public, max-age=60, stale-while-revalidate=300',
        'Content-Type': 'application/json',
      });

      return payload;
    } catch (err: any) {
      if (err?.code === 'ENOENT') {
        throw new NotFoundException(`Top-hub data not found for hub=${hubId || 'top-hub'}`);
      }
      throw new HttpException('Unable to fetch top-hub signal', HttpStatus.INTERNAL_SERVER_ERROR);
    }
  }
}
```

---

## 5) Module registration

`src/modules/signals/signals.module.ts`
```ts
import { Module } from '@nestjs/common';
import { TopHubController } from './controllers/top-hub.controller';
import { TopHubSignalService } from './services/top-hub-signal.service';

@Module({
  controllers: [TopHubController],
  providers: [TopHubSignalService],
})
export class SignalsModule {}
```

Ensure `SignalsModule` is imported in your `AppModule`.

---

## 6) Unit test (Jest)

`test/top-hub.e2e-spec.ts`
```ts
import { Test } from '@nestjs/testing';
import { INestApplication } from '@nestjs/common';
import * as request from 'supertest';
import { TopHubController } from '../src/modules/signals/controllers/top-hub.controller';
import { TopHubSignalService } from '../src/modules/signals/services/top-hub-signal.service';

describe('TopHub (e2e)', () => {
  let app: INestApplication;

  beforeEach(async () => {
    const module = await Test.createTestingModule({
      controllers: [TopHubController],
      providers: [
        {
          provide: TopHubSignalService,
          useValue: {
            getTopHub: jest.fn().mockResolvedValue({
              hubId: 'MOC',
              hubLabel: 'MOC',
              hubDescription: 'Master operational change',
              proposals: [
                {
                  id: 'MOC-001',
                  title: 'Standardize RI purchase cadence to quarterly',
                  signal: 'high' as const,
