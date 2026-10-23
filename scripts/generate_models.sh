#!/usr/bin/env bash
# Regenerate typed models from spec/openapi.json (run after every spec sync).
set -euo pipefail
cd "$(dirname "$0")/.."
uv run datamodel-codegen --input spec/openapi.json --input-file-type openapi \
  --output sdk/python/promptli/_generated/models.py --output-model-type pydantic_v2.BaseModel \
  --use-annotated --field-constraints --target-python-version 3.11 --use-standard-collections \
  --use-union-operator --enum-field-as-literal all --snake-case-field --allow-population-by-field-name \
  --disable-timestamp --formatters ruff-format
touch sdk/python/promptli/_generated/__init__.py
