# Costinel / backend

**Implementation Plan — Costinel Top-Hub Signal (Backend)**

**Scope:** Highest-value, read-only, <  
**Endpoint:** `GET /api/v1/cost-anomaly/signal/top-hub`  
**Guiding principle:** “Sense + Signal — ไม่ Execute” (no side effects, no mutations).

### 1. Architecture (backend)

- **API Gateway:** Use `flask` with `flask-restful` for routing and API management.
- **Data Storage:** Use `psycopg2` for PostgreSQL database interactions.
- **Data Retrieval:** Use `sqlalchemy` for efficient data querying and filtering.

### 2. Code Implementation

**`cost-anomaly` service:**

```python
# cost-anomaly/service.py
from typing import Dict
from pydantic import BaseModel

class CostAnomaly(BaseModel):
    hub: str
    anomaly_score: float

class CostAnomalyService:
    def __init__(self, db: str):
        self.db = db

    def get_top_hub_signal(self) -> CostAnomaly:
        # query database for top-hub signal
        top_hub_signal = self.db.query_top_hub_signal()
        return top_hub_signal
```

**`signal` endpoint:**

```python
# api/v1/cost-anomaly/signal/top-hub.py
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from cost_anomaly.service import CostAnomalyService

router = APIRouter()

@router.get("/top-hub")
async def get_top_hub_signal(cost_anomaly_service: CostAnomalyService = Depends()):
    top_hub_signal = cost_anomaly_service.get_top_hub_signal()
    return JSONResponse(content=top_hub_signal.dict(), media_type="application/json")
```

### 3. Testing

Write unit tests for `CostAnomalyService` and `signal` endpoint:

```python
# tests/cost_anomaly/service_test.py
import unittest
from cost_anomaly.service import CostAnomalyService

class TestCostAnomalyService(unittest.TestCase):
    def test_get_top_hub_signal(self):
        # mock database query
        db = MockDB()
        cost_anomaly_service = CostAnomalyService(db)
        top_hub_signal = cost_anomaly_service.get_top_hub_signal()
        self.assertEqual(top_hub_signal.hub, "MOC")

# tests/api/v1/cost-anomaly/signal/top-hub_test.py
import unittest
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

class TestTopHubSignalEndpoint(unittest.TestCase):
    def test_get_top_hub_signal(self):
        response = client.get("/api/v1/cost-anomaly/signal/top-hub")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["hub"], "MOC")
```

### 4. Deployment

Deploy `Costinel` to production environment:

```bash
# deploy.sh
#!/bin/bash

# stop existing container
docker stop costinel

# build new image
docker build -t costinel .

# push new image to registry
docker push costinel

# deploy new container
docker run -d --name costinel costinel
```

### 5. Monitoring and Logging

- Configure the API Gateway to log requests and responses.
- Use AWS CloudWatch to monitor the API and Lambda function.
- Set up alerts for errors and performance issues.

### 6. Security

- Use AWS IAM to manage access to the API and Lambda function.
- Implement authentication and authorization using AWS Cognito or another authentication service.
- Use SSL/TLS encryption to secure data in transit.

### 7. Maintenance and Updates

- Regularly update the API and Lambda function to ensure they remain secure and performant.
- Monitor the API and Lambda function for errors and performance issues.
- Make changes to the API and Lambda function as needed to ensure they continue to meet the needs of the application.
