---
name: ensure-test-files-exist
description: Verify that a JavaScript/TypeScript project has test files and a valid test script before CI runs.
author: Ashira
version: 1.0.0
---

# Purpose
Ensures that a project using npm scripts (e.g., vitest, jest) actually contains test files to avoid CI failures.

# Prerequisites
- Node.js and npm installed.
- Project root with a `package.json`.

# Steps
1. Locate `package.json` in the project root.
2. Parse the `scripts.test` entry. If missing, report and optionally add a placeholder script.
3. Search for test files matching common patterns:
   - `**/*.{test,spec}.?(c|m)[jt]s?(x)`
   - `**/tests/**/*.js` etc.
   Use `search_files` with the appropriate glob.
4. If no test files are found, fail the verification and suggest adding at least one test file or disabling the test script.
5. (Optional) If a test script exists but no test files, add a minimal example test file to `src/__tests__/example.test.ts`:
   ```
   import { describe, it, expect } from 'vitest';
   describe('example', () => {
     it('passes', () => {
       expect(true).toBe(true);
     });
   });
   ```
6. Run `npm test --silent` and capture exit code.
7. Return JSON result:
   ```json
   {"passed": <bool>, "details": "..."}
   ```

# Pitfalls
- Projects using alternative test runners may have different file patterns.
- Ensure the repository's `.gitignore` does not exclude the test directory.
- Running `npm test` in CI without installing dependencies will also fail.

# Verification
- The command should exit with code 0 when tests run successfully.
- The JSON output must contain `passed` key.
