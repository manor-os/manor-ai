#!/bin/bash
# Generate TypeScript API client from OpenAPI schema
# Requires: npx openapi-typescript (installed as dev dep)
set -e

echo "Exporting OpenAPI schema..."
PYTHONPATH=. python3 scripts/export_openapi.py docs/openapi.json

echo "Generating TypeScript types..."
cd apps/web
npx openapi-typescript ../../docs/openapi.json -o src/lib/api-types.generated.ts

echo "Done! Generated types at apps/web/src/lib/api-types.generated.ts"
