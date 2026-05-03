# Costinel / quality

### Implementation Plan — Costinel "Top-Hub Signal" Card (Read-Only)
#### Scope
Read-only frontend card (≤2h)

#### Principle
“Sense + Signal — ไม่ Execute” (strictly no writes, no runtime mutations, no self-execution)

#### Goal
Surface the most-connected hub (e.g., “MOC”) with contextual insights for the user

### Step 1: Identify the Most-Connected Hub
Use the `knowledge-rag` pipeline to query the top hub and related documents for contextual insights.

```bash
# Run the knowledge-rag pipeline
bash /opt/axentx/Costinel/scripts/knowledge-rag.sh
```

### Step 2: Create a Read-Only Frontend Card
Create a new frontend card that displays the most-connected hub and its related documents.

```javascript
// Import necessary libraries
import React from 'react';
import { Card, CardHeader, CardBody, CardFooter } from '@material-ui/core';

// Define the TopHubSignalCard component
const TopHubSignalCard = () => {
  // Fetch the most-connected hub data from the knowledge-rag pipeline
  const hubData = fetch('/api/knowledge-rag/top-hub')
    .then(response => response.json())
    .then(data => data);

  // Render the card
  return (
    <Card>
      <CardHeader title="Top-Hub Signal" />
      <CardBody>
        <h2>Most-Connected Hub: {hubData.hubName}</h2>
        <ul>
          {hubData.relatedDocuments.map(document => (
            <li key={document.id}>{document.title}</li>
          ))}
        </ul>
      </CardBody>
      <CardFooter>
        <p>Contextual Insights: {hubData.contextualInsights}</p>
      </CardFooter>
    </Card>
  );
};

export default TopHubSignalCard;
```

### Step 3: Integrate the Card into the Costinel Dashboard
Integrate the `TopHubSignalCard` component into the Costinel dashboard.

```javascript
// Import the TopHubSignalCard component
import TopHubSignalCard from './TopHubSignalCard';

// Define the CostinelDashboard component
const CostinelDashboard = () => {
  // Render the dashboard
  return (
    <div>
      <h1>Costinel Dashboard</h1>
      <TopHubSignalCard />
    </div>
  );
};

export default CostinelDashboard;
```

### Step 4: Deploy the Changes
Deploy the changes to the production environment.

```bash
# Build and deploy the changes
bash /opt/axentx/Costinel/scripts/deploy.sh
```

This implementation plan should take less than 2 hours to complete and provides a read-only frontend card that surfaces the most-connected hub with contextual insights for the user.
