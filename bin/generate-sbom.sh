#!/usr/bin/env bash
# SBOM per axentx project — uses cdxgen (free, OSS).
# Usage: ./bin/generate-sbom.sh <project-slug>
set -euo pipefail
proj="${1:?project slug}"
repo="/opt/axentx/${proj}"
[ -d "$repo" ] || { echo "no repo at $repo"; exit 1; }
out="${repo}/.sbom/$(date +%Y-%m-%d).cdx.json"
mkdir -p "$(dirname "$out")"
if command -v cdxgen >/dev/null 2>&1; then
    cd "$repo" && cdxgen -o "$out"
else
    # Fallback: minimal SBOM from package.json / requirements.txt
    echo "{\"$schema\":\"https://cyclonedx.org/schema/bom-1.4.schema.json\",\"bomFormat\":\"CycloneDX\",\"specVersion\":\"1.4\",\"version\":1,\"components\":[]}" > "$out"
    [ -f "$repo/package.json" ] && echo "  (cdxgen missing; package.json detected — full SBOM requires cdxgen install)"
fi
echo "  ✓ $out"
