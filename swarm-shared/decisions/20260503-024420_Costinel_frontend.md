# Costinel / frontend

# Final Synthesis: Top-Hub Signal Panel for Costinel

## Core Objective
Add a **non-blocking Top-Hub Signal Panel** to the Costinel dashboard that defaults to the most-connected hub **MOC** (configurable) and displays the **top 3 actionable, cost-impact proposals**. Implementation must be achievable in **<2 hours**.

---

## Resolved Architecture (Correctness + Actionability)

### 1. Component Structure (React)
**Use Candidate 2’s functional approach** (cleaner, no axios dependency) **+ Candidate 3’s CSS styling** (production-ready visuals).

**File: `components/TopHubSignalPanel.js`**
```jsx
import React from 'react';
import './TopHubSignalPanel.css';

const TopHubSignalPanel = () => {
  const [hubName, setHubName] = React.useState('MOC');
  const [proposals, setProposals] = React.useState([]);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState(null);

  const fetchProposals = React.useCallback(async (hub) => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetch(`/api/proposals?hub=${encodeURIComponent(hub)}`);
      if (!response.ok) throw new Error('Failed to fetch proposals');
      const data = await response.json();
      setProposals(data.slice(0, 3));
    } catch (err) {
      setError(err.message);
      setProposals([]);
    } finally {
      setLoading(false);
    }
  }, []);

  React.useEffect(() => {
    fetchProposals(hubName);
  }, [hubName, fetchProposals]);

  return (
    <div className="top-hub-signal-panel">
      <div className="panel-header">
        <h2>Top-Hub Signal Panel</h2>
        <select 
          value={hubName} 
          onChange={(e) => setHubName(e.target.value)}
          className="hub-select"
        >
          <option value="MOC">MOC</option>
          {/* Extend with dynamic hub list if available */}
        </select>
      </div>

      {loading && <p className="status">Loading proposals...</p>}
      {error && <p className="status error">Error: {error}</p>}

      {!loading && !error && (
        <ul className="proposals-list">
          {proposals.length > 0 ? (
            proposals.map((proposal) => (
              <li key={proposal.id} className="proposal-item">
                <strong>{proposal.name}</strong>
                {proposal.costImpact && (
                  <span className="cost-impact">
                    Impact: {proposal.costImpact}
                  </span>
                )}
              </li>
            ))
          ) : (
            <li className="no-data">No proposals available</li>
          )}
        </ul>
      )}
    </div>
  );
};

export default TopHubSignalPanel;
```

**File: `components/TopHubSignalPanel.css`**
```css
.top-hub-signal-panel {
  background-color: #f7f7f7;
  padding: 20px;
  border: 1px solid #ddd;
  border-radius: 10px;
  box-shadow: 0 0 10px rgba(0, 0, 0, 0.1);
  margin: 16px;
  max-width: 500px;
}

.panel-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}

.panel-header h2 {
  margin: 0;
  font-size: 1.25rem;
}

.hub-select {
  padding: 6px 12px;
  border-radius: 6px;
  border: 1px solid #ccc;
  background: white;
}

.proposals-list {
  list-style: none;
  padding: 0;
  margin: 0;
}

.proposal-item {
  padding: 12px;
  border-bottom: 1px solid #ddd;
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.proposal-item:last-child {
  border-bottom: none;
}

.cost-impact {
  font-size: 0.875rem;
  color: #2e7d32;
  font-weight: 600;
}

.status {
  padding: 12px;
  color: #666;
  font-style: italic;
}

.error {
  color: #c62828;
}

.no-data {
  padding: 12px;
  color: #999;
}
```

---

### 2. Backend API (Minimal, Correct)
**Use Candidate 2’s endpoint structure** (clean Express) **with Candidate 1’s `knowledge-rag` concept renamed to a real endpoint**.

**File: `server/api.js`**
```js
const express = require('express');
const router = express.Router();

// Mock data store — replace with real DB or RAG call
const mockProposals = {
  MOC: [
    { id: 'm1', name: 'Optimize GPU Utilization', costImpact: '-$4.2k/mo' },
    { id: 'm2', name: 'Right-size Over-provisioned Nodes', costImpact: '-$2.8k/mo' },
    { id: 'm3', name: 'Enable Spot Instances for Batch Jobs', costImpact: '-$1.5k/mo' },
    { id: 'm4', name: 'Archive Cold Storage', costImpact: '-$0.9k/mo' }
  ],
  // Add other hubs as needed
};

router.get('/proposals', async (req, res) => {
  try {
    const hubName = req.query.hub || 'MOC';
    const proposals = mockProposals[hubName] || [];
    res.json(proposals);
  } catch (err) {
    res.status(500).json({ error: 'Internal server error' });
  }
});

module.exports = router;
```

---

### 3. Dashboard Integration
**File: `pages/Dashboard.js`**
```jsx
import React from 'react';
import TopHubSignalPanel from '../components/TopHubSignalPanel';

const Dashboard = () => {
  return (
    <div className="dashboard">
      {/* Existing dashboard widgets */}
      <div className="dashboard-grid">
        {/* ... other panels ... */}
        <TopHubSignalPanel />
      </div>
    </div>
  );
};

export default Dashboard;
```

---

## Key Improvements Over Candidates
| Issue | Resolution |
|-------|-----------|
| **Axios vs Fetch** | Use native `fetch` (no extra dependency). |
| **Error handling** | Added loading/error states (Candidates 1–3 omitted). |
| **Styling** | Included production CSS (Candidate 2 missing). |
| **API correctness** | Proper Express route + mock data (Candidate 1 used fake URL). |
| **Configurability** | Dropdown for hub selection, default MOC (all agreed). |
| **Non-blocking** | Panel is self-contained, no side effects on other widgets. |

---

## Implementation Checklist (<2h)
1. Create `components/TopHubSignalPanel.js` + CSS.
2. Add `/api/proposals` endpoint in backend.
3. Import panel into `Dashboard.js`.
4. Verify data flow: MOC → 3 proposals → styled list.
5. Test hub switch + error states.

**Estimated time:** 90 minutes (including styling and basic tests).
