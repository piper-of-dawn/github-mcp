#!/usr/bin/env bash
set -Eeuo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
export TMPDIR=${CONTAINER_TMP_DIR:-/work/tmp}
python3 "$root/test_mcp.py" config
python3 "$root/test_mcp.py" binary
python3 "$root/test_mcp.py" prepare
export SSL_CERT_FILE="${CONTAINER_TMP_DIR:-/work/tmp}/ca-bundle.pem"
export REQUESTS_CA_BUNDLE=$SSL_CERT_FILE
exec python3 "$root/test_mcp.py" test
