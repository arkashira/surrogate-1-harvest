# airship / frontend

**Highest-Value Incremental Improvement:**

**Frontend: Improve UX and UI Responsiveness by Displaying Loading Indicators and Caching Frequently Accessed Data**

**Implementation Plan:**

1. **Identify long-running API calls and frequently accessed data**: Review the frontend code to identify API calls that take more than 2 seconds to complete and data that is frequently accessed by users.
2. **Add loading indicators**: Use a library like `react-loading-skeleton` to add loading indicators for the identified API calls.
3. **Implement caching using Redis**: Set up Redis as a caching layer in the Arkship frontend. This will store frequently accessed data in memory, reducing the load on the backend and improving UI responsiveness.
4. **Cache data using a library like `react-query`**: Use a library like `react-query` to cache data in the frontend. This will allow us to easily manage caching and invalidate cached data when it changes.
5. **Configure caching settings**: Configure caching settings to ensure that data is cached for a reasonable amount of time, and that data is invalidated when it changes.

**Code Snippets:**

**Step 1: Identify long-running API calls and frequently accessed data**

```javascript
// src/utils/api-calls.js
import { fetchSurrogateData, fetchArkshipData } from './api';

const apiCallsWithLoadingIndicators = [
  'fetchSurrogateData',
  'fetchArkshipData',
];

const frequentlyAccessedData = [
  'workflow-templates',
  'incident-management-dashboards',
  'ai-model-information',
];

export { apiCallsWithLoadingIndicators, frequentlyAccessedData };
```

**Step 2: Add loading indicators**

```javascript
// src/components/LoadingIndicator.js
import { Skeleton } from 'react-loading-skeleton';

const LoadingIndicator = () => {
  return (
    <Skeleton count={1} width="100%" height="20px" />
  );
};

export default LoadingIndicator;
```

**Step 3: Implement caching using Redis and `react-query`**

```javascript
// src/utils/redis.js
import Redis from 'ioredis';

const redis = new Redis({
  host: 'localhost',
  port: 6379,
});

export default redis;
```

```javascript
// src/components/WorkflowTemplate.js
import { useQuery, useQueryClient } from 'react-query';
import redis from '../utils/redis';

const fetchWorkflowTemplate = async () => {
  const response = await fetch('/api/workflow-templates');
  return response.json();
};

const WorkflowTemplate = () => {
  const { data, isLoading, error } = useQuery(
    'workflow-template',
    fetchWorkflowTemplate,
    {
      // Cache data for 1 hour
      staleTime: 3600000,
      // Use Redis caching
      cache: 'redis',
    }
  );

  if (isLoading) return <div>Loading...</div>;
  if (error) return <div>Error: {error.message}</div>;

  return <div>{data.name}</div>;
};

export default WorkflowTemplate;
```

**Step 4: Configure caching settings**

```javascript
// src/utils/cache.js
import redis from './redis';

const cacheSettings = {
  // Cache data for 1 hour
  staleTime: 3600000,
  // Use Redis caching
  cache: 'redis',
};

export default cacheSettings;
```

**Commit Message:**

`feat(frontend): improve UX and UI responsiveness by displaying loading indicators and caching frequently accessed data`

**API Documentation:**

`GET /api/workflow-templates`

* Returns a list of workflow templates
* Caches data for 1 hour using Redis

This implementation improves UX and UI responsiveness by displaying loading indicators for long-running API calls and caching frequently accessed data using Redis and `react-query`. This reduces the load on the backend and improves the user experience.

**Estimated Time:** 2 hours

**Tags:** #frontend #ux #ui #caching #redis #react-query #loading-indicators
