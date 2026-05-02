# Costinel / backend

**Costinel Top-Hub Signal (Backend) Implementation Plan**

**Scope:** Highest-value, read-only, < 
**Endpoint:** `GET /api/v1/cost-anomaly/signal/top-hub`  
**Guiding principle:** “Sense + Signal — ไม่ Execute” (no single-point-of-failure)

**Implementation Steps:**

1. **Knowledge-Rag Pipeline Integration**
	* Integrate knowledge-rag pipeline to query top hub and related docs for contextual insights
	* Use existing market analysis script (e.g., granite-business-research.sh) to retrieve relevant data
2. **Top-Hub Doc Insight**
	* Review the most-connected hub (e.g., "MOC") before planning tasks
	* Use knowledge-rag to retrieve top hub and related docs
3. **API Endpoint Creation**
	* Create a new API endpoint `GET /api/v1/cost-anomaly/signal/top-hub` to retrieve top hub signal
	* Implement API endpoint using existing knowledge-rag pipeline and top-hub doc insight
4. **Testing and Validation**
	* Test API endpoint with sample data to ensure correct functionality
	* Validate API endpoint with production data to ensure accuracy and reliability

**Code Snippets:**

```bash
# knowledge-rag pipeline integration
#!/bin/bash
knowledge_rag_query() {
  # query top hub and related docs for contextual insights
  hub=$(knowledge-rag query "SELECT * FROM hub WHERE id = 'MOC'")
  related_docs=$(knowledge-rag query "SELECT * FROM doc WHERE hub_id = '$hub'")
  echo "$hub"
  echo "$related_docs"
}

# top-hub doc insight
#!/bin/bash
top_hub_doc_insight() {
  # review the most-connected hub (e.g., "MOC") before planning tasks
  hub=$(knowledge-rag query "SELECT * FROM hub WHERE id = 'MOC'")
  related_docs=$(knowledge-rag query "SELECT * FROM doc WHERE hub_id = '$hub'")
  echo "$hub"
  echo "$related_docs"
}

# API endpoint creation
#!/bin/bash
api_endpoint_top_hub_signal() {
  # create a new API endpoint to retrieve top hub signal
  echo "GET /api/v1/cost-anomaly/signal/top-hub"
  knowledge_rag_query
  top_hub_doc_insight
}
```

**Commit Message:**

`feat: implement top-hub signal API endpoint`

**API Documentation:**

```markdown
# Top-Hub Signal API Endpoint

## Description

Retrieve the top hub signal and related docs for contextual insights.

## Request

`GET /api/v1/cost-anomaly/signal/top-hub`

## Response

* `hub`: the most-connected hub (e.g., "MOC")
* `related_docs`: the top hub and related docs

## Example Response

```json
{
  "hub": "MOC",
  "related_docs": [
    {
      "id": 1,
      "title": "Doc 1",
      "content": "This is doc 1"
    },
    {
      "id": 2,
      "title": "Doc 2",
      "content": "This is doc 2"
    }
  ]
}
```
```
