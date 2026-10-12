#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
trap 'printf "ERROR: build failed at line %s\n" "$LINENO" >&2' ERR
[[ $# -ge 1 && $# -le 2 ]] || fail 'Usage: ./build.sh /path/to/github-mcp-server [github-mcp:local]'
for tool in python3 docker mktemp cp chmod mkdir rm; do
    command -v "$tool" >/dev/null || fail "Required host utility missing: $tool"
done
project=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
binary=$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve(strict=True))' "$1" 2>/dev/null) || fail 'Supplied binary path cannot be resolved'
[[ -f "$binary" && -x "$binary" ]] || fail 'Supplied binary must be a regular executable file'
image=${2:-github-mcp:local}
base=${RUNTIME_BASE_IMAGE:-}
[[ -n "$base" ]] || fail 'Set RUNTIME_BASE_IMAGE to an approved, locally loaded image tag'
docker image inspect "$base" >/dev/null 2>&1 || fail 'Runtime base image is not locally available (or Docker daemon unavailable). Use docker load first.'
base_id=$(docker image inspect --format '{{.Id}}' "$base")
platform=$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$base")
case "$platform" in linux/amd64|linux/arm64) ;; *) fail "Unsupported runtime image platform: $platform" ;; esac
context=$(mktemp -d)
cleanup() { rm -rf -- "$context"; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir -p "$context/tests"
# Explicit allowlist: never stage .env, a checkout, credentials, or the project tree.
cp -- "$binary" "$context/github-mcp-server"
chmod 0755 "$context/github-mcp-server"
for source in Dockerfile .dockerignore entrypoint.sh; do cp -- "$project/$source" "$context/$source"; done
for source in test_mcp.py test_startup.sh; do cp -- "$project/tests/$source" "$context/tests/$source"; done
chmod 0755 "$context" "$context/tests" "$context/entrypoint.sh" "$context/tests/test_startup.sh"
chmod 0644 "$context/tests/test_mcp.py" "$context/Dockerfile" "$context/.dockerignore"
python3 "$context/tests/test_mcp.py" inspect --binary "$context/github-mcp-server" --arch "${platform#linux/}"
# Preflight the inspected local image ID. Keep its tag unchanged during the build.
# Loader --list validates dynamic dependencies inside the actual target runtime.
docker run --rm --network=none --pull=never --platform "$platform" --user 65532:65532 \
    --mount "type=bind,src=$context,dst=/stage,readonly" \
    --entrypoint /bin/bash "$base_id" -ec \
    'python3 -c "import ssl; ssl.create_default_context(); assert ssl.create_default_context().get_ca_certs()"; python3 /stage/tests/test_mcp.py binary --binary /stage/github-mcp-server --arch "$1"' _ "${platform#linux/}"
docker build --network=none --pull=false --platform "$platform" \
    --build-arg "RUNTIME_BASE_IMAGE=$base" --tag "$image" "$context"
printf 'Image: %s\nStartup: MCP_IMAGE=%q docker compose up -d --pull never\n' "$image" "$image"
