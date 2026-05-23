#!/usr/bin/env bash
# Wrapper: delegate to /opt/surrogate-1-harvest/bin/github-bulk-train-scrape.sh (real implementation).
# Hermes cron requires scripts inside /opt/surrogate-1-harvest/bin/.
exec /opt/surrogate-1-harvest/bin/github-bulk-train-scrape.sh "$@"
