#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
trap 'printf "ERROR: prestart failed at line %s; HTTP server was not started\n" "$LINENO" >&2' ERR
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
export GITHUB_TOOLSETS=${GITHUB_TOOLSETS:-repos,pull_requests}
export GITHUB_READ_ONLY=${GITHUB_READ_ONLY:-true}
export GITHUB_LOCKDOWN_MODE=${GITHUB_LOCKDOWN_MODE:-false}
export PRESTART_TEST_MODE=${PRESTART_TEST_MODE:-live}
export CONTAINER_TMP_DIR=${CONTAINER_TMP_DIR:-/work/tmp}
export CONTAINER_LOG_DIR=${CONTAINER_LOG_DIR:-/work/logs}
export CONTAINER_REPO_PATH=${CONTAINER_REPO_PATH:-/work/repo}
export TMPDIR=$CONTAINER_TMP_DIR
export GITHUB_ENABLE_COMMAND_LOGGING=false
unset GITHUB_LOG_FILE
export SSL_CERT_FILE="$CONTAINER_TMP_DIR/ca-bundle.pem"
export REQUESTS_CA_BUNDLE=$SSL_CERT_FILE
"$root/tests/test_startup.sh"
# The PAT belongs to prestart stdio tests. HTTP requests authenticate per client.
unset GITHUB_PERSONAL_ACCESS_TOKEN
export GITHUB_BASE_URL=${MCP_BASE_URL:-}
export GITHUB_BASE_PATH=${MCP_BASE_PATH:-/mcp}
export GITHUB_SCOPE_CHALLENGE=${MCP_SCOPE_CHALLENGE:-false}
export GITHUB_PORT=8082
help_text=$("${MCP_BINARY:-/usr/local/bin/github-mcp-server}" http --help)
if [[ "$help_text" == *--listen-host* ]]; then
    export GITHUB_LISTEN_HOST=${MCP_BIND_HOST:-0.0.0.0}
fi
printf 'Prestart complete; starting official Streamable HTTP server on internal port 8082\n'
exec "${MCP_BINARY:-/usr/local/bin/github-mcp-server}" http
