# Costinel / backend

**Final Implementation Plan: Costinel Top-Hub Signal (Backend)**

**Scope:** Highest-value, read-only, <  
**Endpoint:** `GET /api/v1/cost-anomaly/signal/top-hub`  
**Guiding principle:** “Sense + Signal — ไม่ Execute” (no side effects, no mutations).

### 1. Architecture (backend)

- **API Gateway:** Use existing `fastapi` instance at `/api/v1/`
- **Service:** Create new service `cost-anomaly` with `uvicorn` worker
- **Database:** Use existing `PostgreSQL` instance with `sqlalchemy` ORM

### 2. Endpoint Implementation

- **`GET /api/v1/cost-anomaly/signal/top-hub`**:
  - **Path Parameters:** None
  - **Query Parameters:** `date` (optional, default: current date)
  - **Response:** JSON object with top hub signal data

### 3. Business Logic

- **Get top hub signal data**:
  - Use existing `cost_anomaly` service to fetch top hub signal data
  - Filter data by `date` query parameter (if provided)
  - Return top hub signal data as JSON object

### 4. Error Handling

- **Catch all exceptions**:
  - Return JSON error response with error message and status code

### 5. Testing

- **Unit tests**:
  - Test `cost_anomaly` service to ensure correct top hub signal data retrieval
- **Integration tests**:
  - Test API endpoint to ensure correct response and error handling

### 6. Deployment

- **Update `docker-compose.yml`**:
  - Add new service `cost-anomaly` with `uvicorn` worker
- **Update `requirements.txt`**:
  - Add required dependencies for `cost-anomaly` service

### 7. Monitoring

- **Add monitoring**:
  - Use existing monitoring tools to monitor `cost-anomaly` service

**Code Snippets:**

```python
# cost_anomaly.py
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

app = FastAPI()

engine = create_engine('postgresql://user:password@host:port/dbname')
Session = sessionmaker(bind=engine)

@app.get('/api/v1/cost-anomaly/signal/top-hub')
def get_top_hub_signal(date: str = None):
    session = Session()
    top_hub_signal = session.query(TopHubSignal).filter_by(date=date).first()
    return {'top_hub_signal': top_hub_signal}
```

```python
# main.py
from fastapi import FastAPI
from uvicorn import run

app = FastAPI()

@app.get('/api/v1/cost-anomaly/signal/top-hub')
def get_top_hub_signal():
    return get_top_hub_signal(date='2022-01-01')
```

```bash
# docker-compose.yml
version: '3'
services:
  cost-anomaly:
    build: .
    environment:
      - DATABASE_URL=postgresql://user:password@host:port/dbname
    depends_on:
      - database
    ports:
      - "8000:8000"
```

```bash
# requirements.txt
fastapi
uvicorn
sqlalchemy
```

**API Documentation:**

```markdown
# Costinel Top-Hub Signal API

## GET /api/v1/cost-anomaly/signal/top-hub

### Parameters

* `date` (optional, default: current date)

### Response

* JSON object with top hub signal data
```

**API Endpoints:**

* `GET /api/v1/cost-anomaly/signal/top-hub`

**API Methods:**

* `get_top_hub_signal(date: str = None)`

**API Implementation:**

* `cost_anomaly.py`
* `main.py`

**API Testing:**

* Unit tests: `test_cost_anomaly.py`
* Integration tests: `test_api.py`

**API Deployment:**

* `docker-compose.yml`
* `requirements.txt`

**API Monitoring:**

* Prometheus
* Grafana
