"""Export the OpenAPI schema to a JSON file.

Usage: PYTHONPATH=. python scripts/export_openapi.py [output_path]
Default output: docs/openapi.json
"""
import json
import sys
from apps.api.main import create_app

app = create_app()
schema = app.openapi()
output = sys.argv[1] if len(sys.argv) > 1 else "docs/openapi.json"
with open(output, "w") as f:
    json.dump(schema, f, indent=2)
print(f"Exported OpenAPI schema to {output}")
print(f"  Paths: {len(schema.get('paths', {}))}")
print(f"  Schemas: {len(schema.get('components', {}).get('schemas', {}))}")
