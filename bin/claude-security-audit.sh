#!/bin/bash
# wrapper — hermes-cli refuses symlinks as path-traversal, so exec the real script
exec "$(dirname "$0")/claude/claude-security-audit.sh" "$@"
