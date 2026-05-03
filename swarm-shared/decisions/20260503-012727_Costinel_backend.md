# Costinel / backend

## Final Implementation Plan: Top-hub Signal Panel

**Scope**: Frontend-only, read-only panel that surfaces the most-connected hub and its actionable proposals from the knowledge graph. Resilient to missing data, consistent with existing UI, and immediately deployable.

**Estimated effort**: 90–110 minutes (70m implementation + 20–40m polish/tests).

---

### 1) Component: `TopHubSignalPanel.tsx`

**Location**: `src/components/TopHubSignalPanel.tsx`

```tsx
import React, { useEffect, useState, useCallback } from 'react';
import { TrendingUp, AlertCircle, CheckCircle, Clock, ExternalLink } from 'lucide-react';

interface Proposal {
  id: string;
  title: string;
  summary: string;
  impact: 'high' | 'medium' | 'low';
  effort: 'low' | 'medium' | 'high';
  status: 'proposed' | 'review' | 'accepted' | 'rejected';
  roi?: number;
  due?: string;
}

interface HubSignal {
  hub: string;
  connections: number;
  proposals: Proposal[];
  lastUpdated: string;
}

const FALLBACK_SIGNAL: HubSignal = {
  hub: 'MOC',
  connections: 0,
  proposals: [],
  lastUpdated: new Date().toISOString(),
};

const TopHubSignalPanel: React.FC = () => {
  const [signal, setSignal] = useState<HubSignal | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchSignal = useCallback(async () => {
    const endpoints = [
      '/api/knowledge-rag/top-hub',
      '/data/top-hub-fallback.json',
    ];

    for (const url of endpoints) {
      try {
        const res = await fetch(url, { cache: 'no-store' });
        if (res.ok) {
          const data = await res.json();
          return {
            hub: data.hub || FALLBACK_SIGNAL.hub,
            connections: Number.isFinite(data.connections) ? Number(data.connections) : FALLBACK_SIGNAL.connections,
            proposals: Array.isArray(data.proposals) ? data.proposals.slice(0, 3) : [],
            lastUpdated: data.lastUpdated || new Date().toISOString(),
          } as HubSignal;
        }
      } catch {
        // continue to next endpoint
      }
    }

    throw new Error('All insight endpoints unavailable');
  }, []);

  useEffect(() => {
    let mounted = true;

    (async () => {
      try {
        const data = await fetchSignal();
        if (mounted) setSignal(data);
      } catch (err) {
        if (mounted) {
          setError(err instanceof Error ? err.message : 'Failed to load hub insight');
          setSignal(FALLBACK_SIGNAL);
        }
      } finally {
        if (mounted) setLoading(false);
      }
    })();

    return () => {
      mounted = false;
    };
  }, [fetchSignal]);

  const getImpactColor = (impact: Proposal['impact']) => {
    switch (impact) {
      case 'high':
        return 'text-red-700 bg-red-50 border-red-200';
      case 'medium':
        return 'text-amber-700 bg-amber-50 border-amber-200';
      default:
        return 'text-green-700 bg-green-50 border-green-200';
    }
  };

  const getEffortBadge = (effort: Proposal['effort']) => {
    switch (effort) {
      case 'low':
        return 'bg-blue-50 text-blue-700';
      case 'medium':
        return 'bg-amber-50 text-amber-700';
      default:
        return 'bg-gray-100 text-gray-700';
    }
  };

  const getStatusIcon = (status: Proposal['status']) => {
    switch (status) {
      case 'accepted':
        return <CheckCircle size={14} className="text-green-600" />;
      case 'rejected':
        return <AlertCircle size={14} className="text-red-500" />;
      case 'review':
        return <Clock size={14} className="text-amber-500" />;
      default:
        return <Clock size={14} className="text-gray-400" />;
    }
  };

  if (loading) {
    return (
      <div className="bg-white rounded-xl border border-gray-100 p-5 shadow-sm animate-pulse">
        <div className="flex items-center justify-between mb-4">
          <div className="h-5 w-28 bg-gray-200 rounded"></div>
          <div className="h-4 w-20 bg-gray-200 rounded"></div>
        </div>
        <div className="h-12 w-48 bg-gray-100 rounded-lg mb-4"></div>
        <div className="space-y-3">
          <div className="h-16 bg-gray-50 rounded-lg"></div>
          <div className="h-16 bg-gray-50 rounded-lg"></div>
        </div>
      </div>
    );
  }

  if (error && !signal) {
    return (
      <div className="bg-white rounded-xl border border-red-100 p-5 shadow-sm">
        <div className="flex items-center gap-2 text-red-600 mb-2">
          <AlertCircle size={18} />
          <span className="font-medium text-sm">Unable to load insights</span>
        </div>
        <p className="text-sm text-gray-500">{error}</p>
      </div>
    );
  }

  if (!signal) return null;

  return (
    <div className="bg-white rounded-xl border border-gray-100 p-5 shadow-sm">
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <div className="w-8 h-8 rounded-lg bg-indigo-50 flex items-center justify-center">
            <TrendingUp size={18} className="text-indigo-600" />
          </div>
          <div>
            <h3 className="font-semibold text-gray-900 text-sm">Top-hub Signal</h3>
            <p className="text-xs text-gray-500">Most-connected hub</p>
          </div>
        </div>
        <span className="text-xs text-gray-400">
          {new Date(signal.lastUpdated).toLocaleDateString(undefined, {
            month: 'short',
            day: 'numeric',
          })}
        </span>
      </div>

      {/* Hub Summary */}
      <div className="mb-4 p-3 bg-gradient-to-r from-indigo-50 to-purple-50 rounded-lg border border-indigo-100">
        <div className="flex items-baseline gap-2">
          <span className="text-2xl font-bold text-indigo-700">{signal.hub}</span>
          <span className="text-sm text-indigo-600 font-medium">
            {signal.connections.toLocaleString()} connections
          </span>
        </div>
        <p className="text-xs text-indigo-600 mt-1">Central node in cost governance graph</p>
      </div>

      {/* Proposals */}
      {signal.proposals.length > 0 ? (
        <div className="space-y-3">
          <h4 className="text-xs font-medium text-gray-400 uppercase tracking-wide">
            Actionable Proposals
          </h4>
          {signal.proposals.map((p) => (
            <div
              key={p.id}
              className="group relative p-3 rounded-lg border border-gray-50 hover:border-gray-200 hover:bg-gray-50/50 transition-all cursor-pointer"
            >
              <div className="flex items
