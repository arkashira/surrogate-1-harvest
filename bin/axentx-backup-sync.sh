#!/usr/bin/env bash
# wrapper — hermes-cli refuses symlinks as path-traversal, so exec the real script
exec /bin/bash "/opt/surrogate-1-harvest/bin/axentx-backup-sync.sh" "$@"
