# surrogate-1 / frontend

**Diagnosis**
* The project lacks a robust implementation for handling Hugging Face API rate limits, which can block dataset training.
* The existing implementation may not be reusing existing Lightning Studio instances efficiently, leading to wasted quota and potential downtime.
* The frontend may not be properly handling errors and exceptions, leading to a poor user experience.
* The project may not be taking full advantage of the Hugging Face CDN, which can bypass API rate limits and improve training performance.
* The frontend may not be properly displaying information about the training process, such as progress and errors.

**Proposed change**
* Implement a robust Hugging Face API rate limit handling mechanism in the frontend.

**Implementation**
1. Create a new file `src/frontend/utils/api_rate_limit.js` with the following code:
```javascript
import axios from 'axios';

const apiRateLimit = {
  getRateLimit: async () => {
    const response = await axios.get('https://huggingface.co/api/v1/rate-limits');
    return response.data;
  },
  waitBeforeRetry: (retryAfter) => {
    return new Promise((resolve) => {
      setTimeout(() => {
        resolve();
      }, retryAfter * 1000);
    });
  },
};

export default apiRateLimit;
```
2. Modify the `src/frontend/train.js` file to use the `apiRateLimit` utility:
```javascript
import apiRateLimit from '../utils/api_rate_limit';

// ...

const listRepoTreeResponse = await axios.get(`https://huggingface.co/api/v1/repos/${repo}/tree/${path}`, {
  headers: {
    Authorization: `Bearer ${token}`,
  },
});

const files = listRepoTreeResponse.data.files;

// ...

if (files.length > 100) {
  // Paginate the API call
  const pagination = await apiRateLimit.getRateLimit();
  const retryAfter = pagination.retry_after;
  await apiRateLimit.waitBeforeRetry(retryAfter);
  // ...
}
```
3. Modify the `src/frontend/train.js` file to handle API rate limit errors:
```javascript
import apiRateLimit from '../utils/api_rate_limit';

// ...

try {
  // ...
} catch (error) {
  if (error.response.status === 429) {
    console.error('API rate limit exceeded:', error.response.data);
    await apiRateLimit.waitBeforeRetry(error.response.data.retry_after);
    // ...
  } else {
    throw error;
  }
}
```
**Verification**
1. Run the frontend application and simulate a scenario where the API rate limit is exceeded.
2. Check the console output for error messages related to API rate limits.
3. Verify that the application waits for the specified retry period before retrying the API call.
4. Check that the application handles API rate limit errors correctly and does not crash.
