#!/bin/bash
# wrapper — hermes-cli refuses symlinks as path-traversal, so exec the real script
"/opt/surrogate-1-harvest/bin/claude-auto-merge.sh" "$@"
