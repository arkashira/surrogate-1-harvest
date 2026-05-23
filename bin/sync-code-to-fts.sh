#!/usr/bin/env bash
# Wrapper: delegate to /opt/surrogate-1-harvest/bin/sync-code-to-fts.sh (real implementation).
# Hermes cron requires scripts inside /opt/surrogate-1-harvest/bin/.
exec /opt/surrogate-1-harvest/bin/sync-code-to-fts.sh "$@"
