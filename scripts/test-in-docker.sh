#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

docker compose -f docker-compose.test.yml build test
docker compose -f docker-compose.test.yml run --rm --no-deps test \
  python -m pytest --collect-only -q
docker compose -f docker-compose.test.yml run --rm --no-deps test \
  python -m pytest -q -m "not browser and not live" "$@"
