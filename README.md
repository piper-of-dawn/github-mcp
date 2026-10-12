# Offline packaging of the official GitHub MCP Server

This project packages an existing executable. It does not compile Go, clone
repositories, install packages, or fetch images. The build context contains only
the binary, Dockerfile, entrypoint and two startup test files. No credentials are
baked into the image. Production runs the official `github-mcp-server http`.

Offline deployment means that image construction needs no internet. Hosted
repository, PR, diff and review operations still require a reachable compatible
GitHub API, such as an internal GitHub Enterprise Server (GHES). A local checkout
cannot supply hosted PRs or reviews. The checkout mount belongs to this validation
harness; the official server does not use it as its repository backend.

Upstream references:

- [Official implementation](https://github.com/github/github-mcp-server)
- [CLI and environment bindings](https://github.com/github/github-mcp-server/blob/v2.0.2/cmd/github-mcp-server/main.go)
- [HTTP hosting](https://github.com/github/github-mcp-server/blob/v2.0.2/pkg/http/server.go)
- [Configuration guide](https://github.com/github/github-mcp-server/blob/v2.0.2/docs/server-configuration.md)

## 1. Prepare approved artifacts on a connected machine

Obtain an approved precompiled Linux release matching the runtime architecture
(amd64 or arm64). Alternatively have your normal build pipeline supply it. No Go
toolchain is needed on the restricted machine. Verify the release archive against
its published SHA256 checksum and record the executable's checksum after extraction.
This project never builds upstream sources or claims to run upstream Go unit tests.

During this session, at the user's explicit request, the official Linux amd64
v2.0.2 release was downloaded to `bin/github-mcp-server`. Its archive SHA256 was
checked against release metadata. `bin/RELEASE.txt` records provenance. This is a
one-time preparation step; there is no download code in build or startup scripts.

Binary SHA256:

```text
721ffbb573397d8e95b847f7a1908359fd2973784ca8a8d5d3182db6f9fcb5b6
```

Transfer the executable, these project files, and an approved runtime image archive
via your approved internal transfer process. For example, between accessible hosts:

```bash
scp /approved/github-mcp-server restricted-host:/approved/github-mcp-server
scp /approved/python-runtime.tar restricted-host:/approved/python-runtime.tar
```

The runtime image must already contain Linux, Bash, Python 3.9 or newer with its
standard library including SSL and ctypes, a valid PEM CA trust store, and the
`mkdir` and `chmod` utilities. Optional Git enables local object integrity checks.
No other command-line test utilities are needed inside the container. Dynamic
binaries also require their ELF loader and all shared libraries in this image.
Use a trusted, internally curated image; this project adds no packages to it.

On a machine where the approved image is already present, export it:

```bash
docker save hsbc-approved/python-runtime:local -o /approved/python-runtime.tar
```

## 2. Load the base image, then package the external executable

On the restricted machine:

```bash
docker load --input /approved/python-runtime.tar
docker image inspect hsbc-approved/python-runtime:local
export RUNTIME_BASE_IMAGE=hsbc-approved/python-runtime:local
./build.sh /approved/github-mcp-server
# Optional custom output tag:
./build.sh ../approved/github-mcp-server github-mcp:local
# The locally prepared release can also be used:
./build.sh ./bin/github-mcp-server github-mcp:local
```

Host prerequisites: Docker Engine with a running daemon, Bash, Python 3.9+, and
standard `cp`, `chmod`, `mkdir`, `mktemp`, `rm` utilities. Use the local Docker Engine
builder that sees the loaded image, not an isolated or remote buildx builder. Do not
change the base image tag while a build is running.

`build.sh` resolves arbitrary relative or absolute paths, including spaces, checks
executable permission, validates Linux-compatible ELF headers and amd64/arm64
architecture against the local image, reports linkage and SHA256, and stages a
minimal temporary context. The image preflight uses the actual target container as
UID/GID 65532, with networking disabled. For a dynamic executable, the ELF loader's
`--list` resolves the required libraries; missing libraries or an incompatible ABI
fail the build. The binary's version and HTTP capabilities must execute successfully.
Static binaries need no shared libraries. The actual execution check also rejects
non-Linux executables that happen to have compatible-looking ELF headers.

The build uses exactly these offline restrictions:

```text
docker build --network=none --pull=false --platform linux/<arch> \
  --build-arg RUNTIME_BASE_IMAGE=<local-image> --tag <output-image> <temporary-context>
```

A missing base image fails before Docker build. Nothing is silently pulled.
Temporary staging is deleted on success, failure, SIGINT or SIGTERM. Like any shell
cleanup trap, it cannot run after SIGKILL or a machine crash. The Dockerfile has no
remote syntax frontend directive and no package-manager commands. Use `build.sh`;
a direct build bypasses its local-image and executable validation guards.

## 3. Configure the internal deployment

```bash
cp .env.example .env
chmod 600 .env
mkdir -p tmp logs
# For the default runtime UID/GID, grant access to writable mount directories:
# An administrator may be needed for these host ownership changes.
chown 65532:65532 tmp logs
```

Edit `.env` as data, not as a shell script. All variables are described there.
`build.sh` reads exported `RUNTIME_BASE_IMAGE` from the shell, not from `.env`.
Set an existing absolute `HOST_REPO_PATH` if validating a real checkout; keep it
readable/searchable by UID 65532. The checkout is mounted read-only. Existing tmp,
log and checkout bind sources are required; Compose does not create missing paths.
All host and container paths are configurable. Mount targets must be distinct and
must not obscure `/opt/github-mcp`, `/usr/local/bin` or the CA trust store.

Example live settings:

```dotenv
GITHUB_HOST=https://github.internal.example
GITHUB_PERSONAL_ACCESS_TOKEN=YOUR_PRESTART_TEST_PAT
GITHUB_TOOLSETS=repos,pull_requests
GITHUB_READ_ONLY=true
GITHUB_LOCKDOWN_MODE=false
PRESTART_TEST_MODE=live
TEST_REPO_OWNER=my-team
TEST_REPO_NAME=my-repository
TEST_REPO_BRANCH=main
TEST_REPO_FILE_PATH=README.md
TEST_PR_NUMBER=123
MCP_HOST_PORT=8082
MCP_BASE_URL=https://mcp.internal.example
```

`GITHUB_HOST` is the GitHub HTTPS web host, without `/api/v3`; upstream derives the
enterprise API endpoint. Test PAT access must cover the selected repository and
PR. Use a real small file (under 1 MB) and a PR with actual diff content. The tests
force read-only mode even if production is configured to permit writes. In lockdown
mode select a PR whose author passes upstream's collaborator checks.

For an enterprise CA, set `CUSTOM_CA_CERT_PATH` to an existing PEM bundle on the
host. Compose mounts it read-only at `/run/enterprise-ca.pem`. Startup validates
and combines it with `CA_BUNDLE_PATH` into the configured writable temp directory
and sets `SSL_CERT_FILE` for the Go server. No trust-store updater is needed.
Leaving it empty mounts `/dev/null` and uses only the image's existing CA bundle.
The extra CA is never baked into the image. A direct `docker run` can instead mount
a PEM anywhere and set `CUSTOM_CA_CERT_PATH` to that container path.

Set `HTTPS_PROXY`, `HTTP_PROXY`, and `NO_PROXY` for approved internal networking.
Include the GHES host in `NO_PROXY` when connecting directly. Neither prestart nor
production needs public internet access. `LOG_DIR` is a configurable host log mount,
with `CONTAINER_LOG_DIR` as its target. Prestart verifies it is writable; routine
logs go to Docker stdout/stderr. Request/response logging is disabled. Do not publish
`docker compose config` output with real tokens, since interpolated settings contain
the test PAT.

## 4. Mandatory live integration tests

The startup sequence is configuration validation, executable/compatibility checks,
temp/CA/permission preparation, selected protocol tests, result reporting, then
HTTP startup. Default `PRESTART_TEST_MODE=live` never falls back to offline.

To execute the complete prestart suite without starting production HTTP:

```bash
docker compose run --rm --no-deps --pull never \
  --entrypoint /opt/github-mcp/tests/test_startup.sh github-mcp
```

The Python standard-library harness launches the actual executable in stdio mode,
performs MCP initialize and initialized notification, retrieves tools/list including
pagination, and requires `list_branches`, `get_file_contents`, `pull_request_read`.
It then calls branches, the configured file at the configured branch, PR details,
and `pull_request_read` with `method=get_diff`. It validates the returned data and
checks temporary server exit/shutdown. All remote operations are read-only.
Authentication is verified through these authenticated operations, rather than a
mock or an unrelated connectivity probe. Failure, timeout, missing tools, inaccessible
repository, invalid token, empty diff or unreachable API exits nonzero. Production
HTTP cannot start after failure. `TEST_RETRIES` means total bounded attempts;
`TEST_TIMEOUT_SECONDS` applies per RPC, so total duration can be longer.

## 5. Explicit offline smoke tests

When no GitHub API can be reached, select `PRESTART_TEST_MODE=offline` in `.env`, or
run this one-off command:

```bash
docker compose run --rm --no-deps --pull never -e PRESTART_TEST_MODE=offline \
  --entrypoint /opt/github-mcp/tests/test_startup.sh github-mcp
```

This executes the real version check, MCP initialize and tools/list, filesystem
permissions, config/temp and CA checks. If Git and checkout metadata exist, it also
runs local `git fsck --no-reflogs`, without writing or fetching. Otherwise that
optional check is explicitly skipped. No token is required for this mode. A dummy
PAT prevents upstream OAuth login, and Linux seccomp denies socket creation in the
temporary server and its descendants. Failure to install the filter fails closed.
This filter is scoped to test subprocesses; it does not block the production HTTP
listener or future client API operations. Supported test architectures are Linux
amd64 and arm64.

The required result message is:

```text
Local MCP startup tests passed. Live GitHub repository and PR operations were NOT verified.
```

Offline mode may start HTTP, but does not establish that GitHub API calls work.

## 6. Start Compose and connect a LangGraph MCP client

```bash
docker compose up -d --pull never
docker compose logs -f github-mcp
docker compose ps -a
```

The container runs as UID/GID 65532 with a read-only root filesystem, read-only
checkout, writable configured temp/log mounts, and normal signal handling. The
entrypoint uses `exec`; Compose's init forwards signals and reaps children. No
additional service is required. Startup failure stops the container; there is no
automatic restart loop.

Docker maps `MCP_HOST_PORT` to upstream's internal 8082. `MCP_PUBLISH_HOST` controls
the host publishing interface and defaults to loopback. Set an approved host interface
for external clients. `MCP_BIND_HOST` controls the internal listener; normally leave
it at `0.0.0.0`. Non-default internal binds require a binary with `--listen-host`.
`MCP_BASE_URL`, `MCP_BASE_PATH`, `MCP_SCOPE_CHALLENGE` map to upstream's corresponding
HTTP environment settings. `GITHUB_AUTHORIZATION_SERVER` is passed through.
The base path describes OAuth resource metadata; it does not change the upstream
`/mcp` route. A reverse proxy exposing a prefix must route/strip that prefix to `/mcp`.
Use your approved TLS termination for remote access; this deployment starts HTTP.

The prestart PAT is removed from the production process environment. Each HTTP
client supplies its own GitHub bearer token; there is no shared production PAT
fallback. The Compose container configuration still contains the prestart PAT and
is visible to Docker administrators. Keep `.env` and Docker access restricted.

In an external LangGraph application whose approved environment already includes
`langchain-mcp-adapters`, use Streamable HTTP (no packages are installed here):

```python
import asyncio
import os
from langchain_mcp_adapters.client import MultiServerMCPClient

async def main():
    client = MultiServerMCPClient({
        "github": {
            "transport": "streamable_http",
            "url": os.environ["GITHUB_MCP_URL"],  # http://server:8082/mcp
            "headers": {"Authorization": "Bearer " + os.environ["CLIENT_GITHUB_PAT"]},
        }
    })
    tools = await client.get_tools()
    # Supply these tools to your existing LangGraph agent.
    print([tool.name for tool in tools])

asyncio.run(main())
```

This is client integration configuration, not a dependency of this container.
Client environments must trust the service's internal TLS certificate when using
HTTPS. OAuth metadata configuration does not convert the test PAT into client
authentication or add OAuth capabilities to GHES itself.

## 7. Diagnosis and local verification

- Missing image: load the approved image archive and check the active Docker
  context/daemon. Neither Compose nor build.sh pulls anything.
- ELF/architecture failure: supply a Linux amd64/arm64 executable matching the
  loaded image. A Windows or macOS executable cannot run here.
- Dynamic-library failure: use an approved runtime containing the correct loader
  and shared-library ABI. Do not copy random host libraries into the image.
- Startup failure: inspect `docker compose logs github-mcp` and exit status with
  `docker compose ps -a`. The harness hides API response bodies and upstream stdio
  stderr to keep credentials and repository contents out of logs.
- Live RPC failure: verify DNS, internal API reachability, proxy bypass, trust
  chain, token validity/repository access, test file/branch and PR number. Never
  change to offline and then claim live integration passed.
- Missing tools: enable `repos,pull_requests`; use a compatible binary with HTTP
  support. Tool availability is tested against the actual supplied executable.
- HTTP 401: the client must send its own `Authorization: Bearer <GitHub token>`.
  Successful prestart tests do not authenticate production clients.
- HTTP 403/404: check client token repository permissions, policy/SSO authorization,
  configured GitHub host, and lockdown restrictions.
- Permission errors: grant UID 65532 read/search access to checkout and CA mounts,
  and write/search access to temp/log mounts. Apply your host's SELinux bind-mount
  policy where required. Do not mount a directory over a PEM file path.

Run available local regression tests without Docker or any package installation:

```bash
bash -n build.sh entrypoint.sh tests/test_startup.sh
python3 -m unittest discover -s tests -p test_validation.py -v
docker compose --env-file .env.example config --quiet
```

Regression tests cover configuration/ELF failures, denied offline sockets, packaging
allowlist and cleanup through a Docker CLI stub, and real executable startup/HTTP
behavior when `bin/github-mcp-server` is present. The stub is only a build-script
control-flow test, never a substitute for a real Docker build or GitHub integration.
The HTTP check uses localhost, expects unauthenticated initialize to return 401,
and completes a real Streamable HTTP initialize with a syntactically valid dummy
client PAT while outbound connect is denied. This tests transport, not PAT validity.
The unreachable-live negative test uses a closed localhost API port and must fail
before production starts. No fake GitHub responses are used.

Current verification in this workspace: official v2.0.2 binary execution, real
offline MCP initialization/tools, local HTTP startup/authentication rejection and Streamable HTTP handshake,
prestart PAT removal, graceful SIGTERM, live startup rejection at an unreachable
endpoint, shell syntax, Compose validation and 11 local regression tests were run. The local
Git integrity and dynamic-loader checks were also executed.
Docker image build/container execution were not run because the Docker daemon and
approved runtime image were unavailable. Successful live repository/file/PR/diff
operations were not run because no reachable internal endpoint and credentials
were supplied. No upstream Go unit tests were run.
