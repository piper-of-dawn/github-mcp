#!/usr/bin/env python3
"""Executable-level checks and newline-delimited stdio MCP, no third-party modules."""
import argparse
import base64
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import queue
import re
import shutil
import signal
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit


class Failure(Exception):
    pass


def require(ok, message):
    if not ok:
        raise Failure(message)


def no_network(allow_listener=False):
    """Deny sockets; the local HTTP regression test may instead deny connect."""
    numbers = {'x86_64': (41, 42), 'aarch64': (198, 203)}.get(platform.machine())
    number = numbers[int(allow_listener)] if numbers else None
    if number is None:
        os._exit(125)
    class Filter(ctypes.Structure):
        _fields_ = [('code', ctypes.c_ushort), ('jt', ctypes.c_ubyte),
                    ('jf', ctypes.c_ubyte), ('k', ctypes.c_uint32)]
    class Program(ctypes.Structure):
        _fields_ = [('len', ctypes.c_ushort), ('filter', ctypes.POINTER(Filter))]
    # Load syscall number; deny the selected native/x32 syscall; otherwise allow.
    filters = (Filter * 5)(Filter(0x20, 0, 0, 0),
                          Filter(0x15, 1, 0, number),
                          Filter(0x15, 0, 1, number | 0x40000000),
                          Filter(0x06, 0, 0, 0x50000 | errno.EPERM),
                          Filter(0x06, 0, 0, 0x7fff0000))
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) or libc.prctl(22, 2, ctypes.byref(Program(5, filters)), 0, 0):
        os._exit(125)


def run(command, timeout=30, offline=False, env=None):
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=timeout, env=env,
                            preexec_fn=no_network if offline else None)
    require(result.returncode == 0, 'Executable command failed: ' + command[0])
    return result.stdout.decode('utf-8', errors='replace')


def inspect_binary(path, expected=None, execute=False):
    path = Path(path).resolve(strict=True)
    require(path.is_file() and os.access(path, os.X_OK), 'Binary must be a regular executable file')
    with path.open('rb') as f:
        h = f.read(64)
        require(len(h) == 64 and h[:4] == b'\x7fELF', 'Binary is not ELF')
        require(h[4:7] == b'\x02\x01\x01' and h[7] in (0, 3),
                'Require a little-endian Linux ELF64 binary')
        machine = struct.unpack_from('<H', h, 18)[0]
        arch = {62: 'amd64', 183: 'arm64'}.get(machine)
        require(arch is not None, 'Supported container architectures: amd64 and arm64')
        require(struct.unpack_from('<H', h, 16)[0] in (2, 3), 'ELF must be executable or PIE')
        if expected:
            require(arch == expected, 'Binary architecture %s differs from target %s' % (arch, expected))
        offset = struct.unpack_from('<Q', h, 32)[0]
        size, count = struct.unpack_from('<HH', h, 54)
        require(size >= 56 and count > 0, 'Invalid ELF program headers')
        interpreter, dynamic = None, False
        for i in range(count):
            f.seek(offset + i * size)
            ph = f.read(56)
            require(len(ph) == 56, 'Truncated ELF program headers')
            kind = struct.unpack_from('<I', ph)[0]
            if kind == 2:
                dynamic = True
            if kind == 3:
                pos, length = struct.unpack_from('<Q', ph, 8)[0], struct.unpack_from('<Q', ph, 32)[0]
                require(0 < length < 4096, 'Invalid ELF interpreter')
                f.seek(pos)
                interpreter = f.read(length).rstrip(b'\0').decode('ascii')
    if execute:
        if dynamic:
            require(interpreter and Path(interpreter).is_file(), 'Dynamic ELF loader missing in runtime image')
            output = run([interpreter, '--list', str(path)], offline=True)
            require('not found' not in output.lower(), 'Required shared library missing')
            print('Dynamic loader resolved required shared libraries')
        run([str(path), '--version'], offline=True)
        help_text = run([str(path), 'http', '--help'], offline=True)
        for flag in ('base-url', 'base-path', 'scope-challenge', 'authorization-server'):
            require('--' + flag in help_text, 'Supplied binary lacks HTTP flag --' + flag)
        if os.environ.get('MCP_BIND_HOST', '0.0.0.0') not in ('', '0.0.0.0'):
            require('--listen-host' in help_text, 'Binary does not support a custom internal bind host')
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest() if hasattr(hashlib, 'file_digest') else None
    if digest is None:
        sha = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1048576), b''):
                sha.update(block)
        digest = sha.hexdigest()
    print('Linux ELF %s; %s linked; SHA256 %s' % (arch, 'dynamically' if dynamic else 'statically', digest))


def positive(name, default, maximum):
    value = os.environ.get(name, str(default))
    require(value.isdigit() and 1 <= int(value) <= maximum, name + ' must be a bounded positive integer')
    return int(value)


def configuration():
    mode = os.environ.get('PRESTART_TEST_MODE', 'live')
    require(mode in ('live', 'offline'), 'PRESTART_TEST_MODE must be live or offline')
    host = os.environ.get('GITHUB_HOST', '')
    parsed = urlsplit(host if '://' in host else 'https://' + host)
    require(parsed.scheme == 'https' and parsed.hostname and not parsed.username
            and not parsed.password and parsed.path in ('', '/') and not parsed.query
            and not parsed.fragment, 'GITHUB_HOST must be an HTTPS GitHub host, without an API path or credentials')
    for key in ('GITHUB_READ_ONLY', 'GITHUB_LOCKDOWN_MODE', 'MCP_SCOPE_CHALLENGE'):
        require(os.environ.get(key, 'false') in ('true', 'false'), key + ' must be true or false')
    positive('TEST_TIMEOUT_SECONDS', 30, 600)
    positive('TEST_RETRIES', 1, 10)
    positive('MCP_HOST_PORT', 8082, 65535)
    for key in ('MCP_BASE_URL', 'GITHUB_AUTHORIZATION_SERVER'):
        if os.environ.get(key):
            u = urlsplit(os.environ[key])
            require(u.scheme in ('http', 'https') and u.hostname and not u.username
                    and not u.password and not u.query and not u.fragment, key + ' must be a URL without credentials')
    path = os.environ.get('MCP_BASE_PATH', '/mcp')
    require(path.startswith('/') and not any(x in path for x in ('?', '#', '..')), 'Invalid MCP_BASE_PATH')
    if mode == 'live':
        for key in ('GITHUB_PERSONAL_ACCESS_TOKEN', 'TEST_REPO_OWNER', 'TEST_REPO_NAME',
                    'TEST_REPO_BRANCH', 'TEST_REPO_FILE_PATH'):
            require(bool(os.environ.get(key, '').strip()), 'Live tests require ' + key)
        require(not any(c.isspace() for c in os.environ['GITHUB_PERSONAL_ACCESS_TOKEN']), 'Test PAT must not contain whitespace')
        positive('TEST_PR_NUMBER', 0, 2147483647)
        for key in ('TEST_REPO_OWNER', 'TEST_REPO_NAME'):
            require(re.fullmatch(r'[A-Za-z0-9_.-]+', os.environ[key]), 'Invalid ' + key)
        require(not os.environ['TEST_REPO_FILE_PATH'].startswith('/')
                and '..' not in Path(os.environ['TEST_REPO_FILE_PATH']).parts, 'Invalid test file path')
    return mode


def prepare():
    configuration()
    tmp = Path(os.environ.get('CONTAINER_TMP_DIR', '/work/tmp'))
    logs = Path(os.environ.get('CONTAINER_LOG_DIR', '/work/logs'))
    repo = Path(os.environ.get('CONTAINER_REPO_PATH', '/work/repo'))
    for path in (tmp, logs, repo):
        require(path.is_absolute(), 'Container directories must be absolute')
        require(path.is_dir(), 'Mounted directory is missing: ' + str(path))
    require(os.access(repo, os.R_OK | os.X_OK), 'Repository directory must be readable and searchable')
    for path in (tmp, logs):
        with tempfile.TemporaryFile(dir=path) as f:
            f.write(b'permission-check')
    ca = Path(os.environ.get('CA_BUNDLE_PATH', '/etc/ssl/certs/ca-certificates.crt'))
    data = ca.read_bytes()
    custom = os.environ.get('CUSTOM_CA_CERT_PATH', '')
    if custom:
        extra = Path(custom).read_bytes()
        require(not extra or b'-----BEGIN CERTIFICATE-----' in extra, 'Enterprise CA bundle must contain PEM certificates')
        data += b'\n' + extra
    # Trust stores may have non-ASCII human comments; keep only PEM certificates.
    data = b'\n'.join(re.findall(b'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----', data, re.S))
    require(data, 'Missing PEM CA certificates')
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cadata=data.decode('ascii'))
    with tempfile.NamedTemporaryFile(dir=tmp, delete=False) as f:
        f.write(data)
        staged = Path(f.name)
    staged.replace(tmp / 'ca-bundle.pem')
    print('Configuration, directory permissions, temporary files and CA bundle passed')


class MCP:
    def __init__(self, binary, mode, timeout):
        env = os.environ.copy()
        env['GITHUB_READ_ONLY'] = 'true'
        env['GITHUB_ENABLE_COMMAND_LOGGING'] = 'false'
        env.pop('GITHUB_LOG_FILE', None)
        if mode == 'offline':
            # A dummy PAT prevents OAuth login. The child cannot create sockets.
            env['GITHUB_PERSONAL_ACCESS_TOKEN'] = 'offline-unused-placeholder'
            for key in list(env):
                if key.startswith('GITHUB_OAUTH_') or key.startswith('GITHUB_APP_'):
                    env.pop(key)
        self.timeout, self.counter = timeout, 0
        self.messages = queue.Queue()
        self.proc = subprocess.Popen([binary, 'stdio', '--read-only'], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     env=env, preexec_fn=no_network if mode == 'offline' else None)
        def read():
            try:
                for line in self.proc.stdout:
                    self.messages.put(json.loads(line))
            except (ValueError, OSError):
                self.messages.put({'invalid': True})
            finally:
                self.messages.put(None)
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()

    def send(self, message):
        self.proc.stdin.write((json.dumps(message) + '\n').encode())
        self.proc.stdin.flush()

    def request(self, method, params):
        self.counter += 1
        self.send({'jsonrpc': '2.0', 'id': self.counter, 'method': method, 'params': params})
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                message = self.messages.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                raise Failure('MCP timeout during ' + method) from None
            require(isinstance(message, dict) and message.get('jsonrpc') == '2.0', 'Invalid MCP response or premature server exit')
            # Never echo RPC errors, repository contents or upstream stderr: they may contain secrets.
            if message.get('id') == self.counter:
                require('error' not in message and isinstance(message.get('result'), dict), 'MCP request failed: ' + method)
                return message['result']
            require('id' not in message, 'Unexpected MCP response ID or server request')

    def close(self, success):
        self.proc.stdin.close()
        try:
            code = self.proc.wait(timeout=5)
            if success:
                require(code == 0, 'Temporary MCP server exited with a failure status')
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                code = self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
                if success:
                    raise Failure('Temporary server required SIGKILL')
            if success:
                require(code in (0, -signal.SIGTERM), 'Temporary server shutdown failed')
        finally:
            self.proc.stdout.close()
            self.reader.join(timeout=1)


def payload(result):
    require(not result.get('isError'), 'GitHub MCP tool returned an error (check access, endpoint and test targets)')
    require(isinstance(result.get('content'), list) and result['content'], 'Tool returned no content')
    return '\n'.join(x.get('text', '') for x in result['content'] if x.get('type') == 'text')


def decoded(result):
    text = payload(result)
    try:
        return json.loads(text)
    except ValueError:
        structured = result.get('structuredContent')
        require(isinstance(structured, (dict, list)), 'Expected JSON tool content')
        return structured


def suite(binary):
    mode = configuration()
    timeout = positive('TEST_TIMEOUT_SECONDS', 30, 600)
    repo = os.environ.get('CONTAINER_REPO_PATH', '/work/repo')
    if shutil.which('git') and (Path(repo) / '.git').exists():
        env = os.environ.copy()
        env['GIT_OPTIONAL_LOCKS'] = '0'
        run(['git', '-c', 'safe.directory=' + repo, '-C', repo, 'fsck', '--no-reflogs'],
            timeout=timeout, offline=True, env=env)
        print('Local Git object integrity passed')
    else:
        print('Local Git integrity skipped: Git or checkout metadata unavailable')
    for attempt in range(positive('TEST_RETRIES', 1, 10)):
        client, success = None, False
        try:
            client = MCP(binary, mode, timeout)
            init = client.request('initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
                                  'clientInfo': {'name': 'offline-deployment-prestart', 'version': '1.0'}})
            require(init.get('protocolVersion') in ('2024-11-05', '2025-03-26', '2025-06-18'), 'Unsupported MCP protocol version')
            require(isinstance(init.get('serverInfo'), dict) and isinstance(init.get('capabilities'), dict)
                    and 'tools' in init['capabilities'], 'Initialize did not advertise tools')
            client.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
            names, cursor, seen = set(), None, set()
            for _ in range(100):
                listing = client.request('tools/list', {'cursor': cursor} if cursor else {})
                require(isinstance(listing.get('tools'), list), 'Invalid tools/list')
                for tool in listing['tools']:
                    require(isinstance(tool, dict) and isinstance(tool.get('name'), str)
                            and isinstance(tool.get('inputSchema'), dict), 'Invalid tool definition')
                    names.add(tool['name'])
                cursor = listing.get('nextCursor')
                if not cursor:
                    break
                require(isinstance(cursor, str) and cursor not in seen, 'Invalid tools pagination')
                seen.add(cursor)
            else:
                raise Failure('Too many tools/list pages')
            require({'list_branches', 'get_file_contents', 'pull_request_read'} <= names,
                    'Required repository/PR tools missing; enable repos,pull_requests')
            print('Real MCP initialize and repository/PR tools/list passed')
            if mode == 'live':
                common = {'owner': os.environ['TEST_REPO_OWNER'], 'repo': os.environ['TEST_REPO_NAME']}
                def call(name, arguments):
                    return client.request('tools/call', {'name': name, 'arguments': dict(common, **arguments)})
                branches = decoded(call('list_branches', {'perPage': 100}))
                require(isinstance(branches, list) and branches
                        and all(isinstance(x, dict) and x.get('name') and x.get('commit', {}).get('sha') for x in branches),
                        'Branch listing is empty or malformed')
                result = call('get_file_contents', {'path': os.environ['TEST_REPO_FILE_PATH'],
                              'ref': 'refs/heads/' + os.environ['TEST_REPO_BRANCH']})
                # Upstream returns a resource block for a file, or JSON metadata in older releases.
                payload(result)
                resources = [x.get('resource', {}) for x in result['content'] if x.get('type') == 'resource']
                if resources:
                    require(any(x.get('uri') and ('text' in x or 'blob' in x) for x in resources), 'Invalid file resource')
                    for resource in resources:
                        if 'blob' in resource:
                            base64.b64decode(resource['blob'], validate=True)
                else:
                    file_data = decoded(result)
                    require(isinstance(file_data, dict) and file_data.get('type') == 'file'
                            and file_data.get('path') == os.environ['TEST_REPO_FILE_PATH']
                            and file_data.get('sha') and isinstance(file_data.get('content'), str),
                            'Expected actual contents of the configured file, not metadata or a directory')
                    if file_data.get('encoding') == 'base64':
                        base64.b64decode(''.join(file_data['content'].split()), validate=True)
                number = int(os.environ['TEST_PR_NUMBER'])
                pr = decoded(call('pull_request_read', {'method': 'get', 'pullNumber': number}))
                require(isinstance(pr, dict) and pr.get('number') == number and pr.get('head')
                        and pr.get('base'), 'Invalid or mismatched pull request')
                diff = payload(call('pull_request_read', {'method': 'get_diff', 'pullNumber': number}))
                require('diff --git ' in diff and re.search(r'^(@@ |new file mode |deleted file mode |Binary files |GIT binary patch|rename from )', diff, re.M),
                        'No actual PR diff returned; choose a PR with changes')
                print('Live branches, configured file, pull request and actual diff passed')
            success = True
        except (Failure, BrokenPipeError) as error:
            if attempt + 1 == int(os.environ.get('TEST_RETRIES', '1')):
                raise Failure(str(error)) from None
            print('Prestart attempt failed; retrying without logging upstream content')
        finally:
            if client:
                client.close(success)
        if success:
            if mode == 'offline':
                print('Local MCP startup tests passed. Live GitHub repository and PR operations were NOT verified.')
            else:
                print('Live GitHub MCP integration tests passed.')
            return


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('inspect', 'binary', 'config', 'prepare', 'test'))
    parser.add_argument('--binary', default=os.environ.get('MCP_BINARY', '/usr/local/bin/github-mcp-server'))
    parser.add_argument('--arch')
    args = parser.parse_args()
    try:
        if args.action in ('inspect', 'binary'):
            inspect_binary(args.binary, args.arch, args.action == 'binary')
        elif args.action == 'config':
            configuration()
            print('Configuration validation passed')
        elif args.action == 'prepare':
            prepare()
        else:
            suite(args.binary)
    except (Failure, OSError, ValueError, subprocess.SubprocessError, ssl.SSLError) as error:
        # Avoid exception contents from environment-derived URLs and remote tool content.
        message = str(error) if isinstance(error, Failure) else type(error).__name__ + ': check binary, mounts and runtime dependencies'
        print('FAIL: ' + message, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
