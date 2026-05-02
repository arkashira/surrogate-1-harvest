# Costinel / discovery

**Highest-Value Incremental Improvement: Discovery Cycle Automation**
===========================================================

**Description:** Automate the discovery cycle by leveraging the existing `axentx-dev-bot` commits and integrating them with the project's CI/CD pipeline.

**Implementation Plan:**

1. **Identify reusable code:** Extract the reusable code from the recent commits (e.g., `f08b6fa`) and create a new script `discovery_cycle.sh` that can be invoked via the CI/CD pipeline.
2. **Integrate with CI/CD pipeline:** Update the `docker-compose.yml` file to include a new service `discovery` that runs the `discovery_cycle.sh` script.
3. **Configure CI/CD pipeline:** Update the `.gitlab-ci.yml` file to trigger the `discovery` service after each commit.
4. **Test and validate:** Run the automated discovery cycle and verify that it produces the expected output.

**Code Snippets:**

```bash
# discovery_cycle.sh
#!/bin/bash

# Extract reusable code from recent commits
recent_commits=$(git log --format=%H -n 10)

# Iterate over recent commits and extract relevant information
for commit in $recent_commits; do
  # Extract commit message and hash
  message=$(git show -s --format=%s $commit)
  hash=$(git rev-parse $commit)

  # Process commit message and hash
  echo "Commit: $hash - $message"
done
```

```yml
# docker-compose.yml
version: '3'

services:
  discovery:
    image: axentx-dev-bot
    command: discovery_cycle.sh
    depends_on:
      - db
```

```yml
# .gitlab-ci.yml
stages:
  - discovery

discovery:
  stage: discovery
  script:
    - docker-compose up -d discovery
  only:
    - main
```

**Estimated Time:** 1 hour

**Tags:** #discovery #automation #ci/cd #axentx-dev-bot
