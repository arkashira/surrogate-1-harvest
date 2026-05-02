# Costinel / backend

**Implementation Plan — Costinel Top-Hub Signal (Backend)**

**Scope:** Highest-value, read-only, <

**Endpoint:** `GET /api/v1/cost-anomaly/signal/top-hub`  
**Guiding principle:** “Sense + Signal — ไม่ Execute” (no side effects, no mutations).

### 1. Architecture (backend)

- **API Gateway:** Use a lightweight API gateway like `github.com/gorilla/mux` to handle incoming requests and route them to the appropriate handler.
- **Service Discovery:** Use a service discovery mechanism like `github.com/Netflix/zuul` to find the Top-Hub Service.
- **Top-Hub Service:** Use the existing Top-Hub Service to retrieve the top hub data.

### 2. Implementation (backend)

```python
import requests
from fastapi import APIRouter

# Define the API endpoint
router = APIRouter()

@router.get("/api/v1/cost-anomaly/signal/top-hub")
async def get_top_hub_signal():
    # Call the Top-Hub Service to retrieve the top hub data
    top_hub_data = await get_top_hub_data()
    return top_hub_data

# Define the Top-Hub Service
def get_top_hub_data():
    # Retrieve the top hub data from the database
    top_hub_data = retrieve_top_hub_data_from_database()
    return top_hub_data

# Define the database schema
from sqlalchemy import create_engine, Column, Integer, String, Text
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()

class TopHub(Base):
    __tablename__ = 'top_hub'
    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=False)

# Define the database connection
engine = create_engine('postgresql://user:password@host:port/dbname')
Base.metadata.create_all(engine)

# Define the function to retrieve top hub data from the database
def retrieve_top_hub_data_from_database():
    # Retrieve the top hub data from the database
    session = Session(bind=engine)
    top_hub_data = session.query(TopHub).first()
    session.close()
    return top_hub_data
```

### 3. Testing

```python
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from main import app

class TestGetTopHubSignal(unittest.TestCase):
    @patch('main.get_top_hub_data')
    def test_get_top_hub_signal(self, mock_get_top_hub_data):
        # Mock the top hub data
        mock_top_hub_data = {'name': 'Top Hub', 'description': 'This is the top hub'}
        
        # Set the mock top hub data
        mock_get_top_hub_data.return_value = mock_top_hub_data
        
        # Create a test client
        client = TestClient(app)
        
        # Send a GET request to the API endpoint
        response = client.get('/api/v1/cost-anomaly/signal/top-hub')
        
        # Assert that the response status code is 200
        self.assertEqual(response.status_code, 200)
        
        # Assert that the response JSON is the mock top hub data
        self.assertEqual(response.json(), mock_top_hub_data)
```

### 4. Deployment

```bash
# Define the Dockerfile
FROM python:3.9-slim

# Set the working directory
WORKDIR /app

# Copy the requirements file
COPY requirements.txt .

# Install the dependencies
RUN pip install -r requirements.txt

# Copy the application code
COPY . .

# Expose the port
EXPOSE 8000

# Run the command to start the application
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

```bash
# Build the Docker image
docker build -t costinel-top-hub-signal .

# Run the Docker container
docker run -p 8000:8000 costinel-top-hub-signal
```

This implementation plan provides a high-value, read-only endpoint that retrieves the top hub signal from the Top-Hub Service. The implementation uses the `fastapi` framework to create a RESTful API endpoint and the `sqlalchemy` library to interact with the database. The testing uses the `unittest` framework to test the `get_top_hub_signal` function. The deployment uses `docker` to deploy the application.
