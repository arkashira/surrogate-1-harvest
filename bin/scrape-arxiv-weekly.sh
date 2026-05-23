#!/bin/bash
# wrapper — hermes-cli refuses symlinks as path-traversal, so exec the real script
exec "/opt/surrogate-1-harvest/bin/scrape-arxiv-weekly.sh" "$@"
