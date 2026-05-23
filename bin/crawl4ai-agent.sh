#!/usr/bin/env bash
# Wrapper: delegate to /opt/surrogate-1-harvest/bin/crawl4ai-agent.sh (real implementation).
# Hermes cron requires scripts inside /opt/surrogate-1-harvest/bin/.
exec /opt/surrogate-1-harvest/bin/crawl4ai-agent.sh "$@"
