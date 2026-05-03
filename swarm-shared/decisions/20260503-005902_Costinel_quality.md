# Costinel / quality

## Implementation Plan — Costinel “Top-Hub Signal” Card (≤2h, frontend-only)

### Scope & Constraints
- **Pure frontend** — no backend, no new APIs, no auth/infra changes.
- **Read-only** — Sense + Signal only.
- **Timeboxed ≤2h** — minimal, high-value UI.
- **Graceful fallback** — works without network or when hub data is unavailable.
- **Reuse existing patterns** — consistent with Costinel design tokens and component conventions.

---

### 1. Component Design (React + TypeScript)

**File**: `src/components/TopHubSignalCard/TopHubSignalCard.tsx`  
**Responsibilities**:
- Display the most-connected hub (e.g., "MOC") with contextual insights.
- Show signal strength, last updated, and quick actions (view details, dismiss).
- Support loading, error, and empty states.
- Use existing design tokens (colors, spacing, typography).

**Props**:
```ts
interface TopHubSignalCardProps {
  hubName?: string;
  signalStrength?: number; // 0-100
  lastUpdated?: string; // ISO
  insights?: string[];
  onDismiss?: () => void;
  onViewDetails?: () => void;
}
```

---

### 2. Mock Data Strategy (for demo/dev)

- Use a static JSON file `src/data/top-hub-signal.json` for local development.
- In production, expect data to be injected via `window.__COSTINEL_TOP_HUB__` (global) or fetched by parent shell (out of scope here).
- Fallback to hardcoded defaults if no data.

**Example mock**:
```json
{
  "hubName": "MOC",
  "signalStrength": 87,
  "lastUpdated": "2026-05-03T00:53:05Z",
  "insights": [
    "Highest cross-account linkage (12 accounts)",
    "Detected 3 idle RDS instances in us-east-1",
    "Potential savings: $2,400/mo via RI recommendations"
  ]
}
```

---

### 3. UI/UX Details

**Layout**:
- Compact card (max-width 480px) with elevated shadow.
- Header: Hub name + signal badge (color-coded: green ≥70, yellow ≥40, red <40).
- Body: Bulleted insights (max 3).
- Footer: Timestamp + action buttons (small, muted).

**Accessibility**:
- Semantic HTML (`<article>`, `<header>`, `<ul>`).
- ARIA labels for dismiss/view buttons.
- Keyboard navigable.

**Animations**:
- Subtle fade-in on mount.
- Pulse animation on signal badge if strength ≥80.

---

### 4. Code Snippets

#### Component Shell
```tsx
// src/components/TopHubSignalCard/TopHubSignalCard.tsx
import React from 'react';
import './TopHubSignalCard.css';

interface TopHubSignalCardProps {
  hubName?: string;
  signalStrength?: number;
  lastUpdated?: string;
  insights?: string[];
  onDismiss?: () => void;
  onViewDetails?: () => void;
}

export const TopHubSignalCard: React.FC<TopHubSignalCardProps> = ({
  hubName = 'Unknown Hub',
  signalStrength = 0,
  lastUpdated,
  insights = [],
  onDismiss,
  onViewDetails,
}) => {
  const getSignalColor = (strength: number) => {
    if (strength >= 70) return 'var(--signal-high)';
    if (strength >= 40) return 'var(--signal-medium)';
    return 'var(--signal-low)';
  };

  const formatTime = (iso?: string) => {
    if (!iso) return '';
    const date = new Date(iso);
    return date.toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  };

  return (
    <article className="top-hub-card" aria-label="Top hub signal">
      <header className="top-hub-header">
        <h3 className="top-hub-title">{hubName}</h3>
        <span
          className="top-hub-badge"
          style={{ backgroundColor: getSignalColor(signalStrength) }}
          aria-label={`Signal strength: ${signalStrength}%`}
        >
          {signalStrength}%
        </span>
      </header>

      {insights.length > 0 && (
        <ul className="top-hub-insights" aria-label="Key insights">
          {insights.slice(0, 3).map((item, i) => (
            <li key={i} className="top-hub-insight-item">• {item}</li>
          ))}
        </ul>
      )}

      <footer className="top-hub-footer">
        <span className="top-hub-time" aria-label="Last updated">
          Updated {formatTime(lastUpdated)}
        </span>
        <div className="top-hub-actions">
          {onViewDetails && (
            <button
              className="top-hub-btn top-hub-btn--secondary"
              onClick={onViewDetails}
              aria-label="View details"
            >
              View
            </button>
          )}
          {onDismiss && (
            <button
              className="top-hub-btn top-hub-btn--tertiary"
              onClick={onDismiss}
              aria-label="Dismiss"
            >
              Dismiss
            </button>
          )}
        </div>
      </footer>
    </article>
  );
};
```

#### Styles (CSS)
```css
/* src/components/TopHubSignalCard/TopHubSignalCard.css */
.top-hub-card {
  max-width: 480px;
  padding: 16px;
  background: var(--bg-card);
  border-radius: 12px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
  font-family: var(--font-sans);
  animation: fadeIn 0.3s ease;
}

.top-hub-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}

.top-hub-title {
  margin: 0;
  font-size: 1.125rem;
  font-weight: 600;
  color: var(--text-primary);
}

.top-hub-badge {
  padding: 4px 10px;
  border-radius: 99px;
  font-size: 0.875rem;
  font-weight: 700;
  color: white;
  animation: pulse 2s infinite;
}

.top-hub-insights {
  margin: 0 0 12px;
  padding-left: 16px;
  color: var(--text-secondary);
  font-size: 0.875rem;
  line-height: 1.5;
}

.top-hub-insight-item {
  margin-bottom: 6px;
}

.top-hub-footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 0.75rem;
  color: var(--text-muted);
}

.top-hub-actions {
  display: flex;
  gap: 8px;
}

.top-hub-btn {
  padding: 4px 10px;
  border-radius: 6px;
  font-size: 0.75rem;
  font-weight: 500;
  cursor: pointer;
  border: none;
  transition: opacity 0.2s;
}

.top-hub-btn:hover {
  opacity: 0.85;
}

.top-hub-btn--secondary {
  background: var(--accent);
  color: white;
}

.top-hub-btn--tertiary {
  background: transparent;
  color: var(--text-muted);
  border: 1px solid var(--border);
}

@keyframes fadeIn {
  from { opacity: 0; transform: translateY(4px); }

