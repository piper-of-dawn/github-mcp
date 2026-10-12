#!/usr/bin/env python3
"""Local regression tests. No fake GitHub API responses and no downloads."""
import http.client
import json
import time
import importlib.util
import os
from pathlib import Path
import socket
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('harness', Path(__file__).with_name('test_mcp.py'))
harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harness)
ROOT = Path(__file__).resolve().parents[1]


class ValidationTests(unittest.TestCase):
    def test_configuration_modes_and_credentials(self):
        with patch.dict(os.environ, {'GITHUB_HOST': 'https://github.internal.example',
                                     'PRESTART_TEST_MODE': 'live'}, clear=True):
            with self.assertRaises(harness.Failure):
                harness.configuration()
            os.environ['PRESTART_TEST_MODE'] = 'offline'
            self.assertEqual(harness.configuration(), 'offline')
            os.environ['TEST_RETRIES'] = '0'
            with self.assertRaises(harness.Failure):
                harness.configuration()

    def test_invalid_host_and_bool(self):
        for env in ({'GITHUB_HOST': 'https://token@host'},
                    {'GITHUB_HOST': 'https://host/api/v3'},
                    {'GITHUB_HOST': 'http://host'},
                    {'GITHUB_READ_ONLY': 'yes'}, {'PRESTART_TEST_MODE': 'unknown'}):
            with patch.dict(os.environ, dict({'GITHUB_HOST': 'https://internal.example',
                                             'PRESTART_TEST_MODE': 'offline'}, **env), clear=True):
                with self.assertRaises(harness.Failure):
                    harness.configuration()

    def test_elf_rejections(self):
        binary = ROOT / 'bin/github-mcp-server'
        if not binary.exists():
            self.skipTest('Official local binary unavailable')
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / 'candidate'
            p.write_text('#!/bin/sh\nexit 0\n')
            p.chmod(0o755)
            with self.assertRaises(harness.Failure):
                harness.inspect_binary(p)
            data = binary.read_bytes()
            p.write_bytes(data)
            with self.assertRaises(harness.Failure):
                harness.inspect_binary(p, expected='arm64')
            header = bytearray(data[:64])
            struct.pack_into('<H', header, 54, 1)
            p.write_bytes(header)
            with self.assertRaises(harness.Failure):
                harness.inspect_binary(p)

    def test_offline_socket_denial(self):
        result = subprocess.run([sys.executable, '-c',
            'import socket,errno\ntry: socket.socket()\nexcept OSError as e: raise SystemExit(0 if e.errno==errno.EPERM else 2)\nraise SystemExit(1)'],
            preexec_fn=harness.no_network)
        self.assertEqual(result.returncode, 0)

    def test_tool_error_and_empty_content_rejected(self):
        for value in ({'isError': True, 'content': [{'type': 'text', 'text': 'secret'}]},
                      {'content': []}, {}):
            with self.assertRaises(harness.Failure):
                harness.payload(value)

    def test_build_argument_failures(self):
        for args in ([], ['/missing/executable'], ['/bin/sh', 'tag', 'extra']):
            result = subprocess.run([str(ROOT / 'build.sh')] + args,
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('ERROR:', result.stderr)

    def test_build_orchestration_and_cleanup(self):
        binary = ROOT / 'bin/github-mcp-server'
        if not binary.exists():
            self.skipTest('Official local binary unavailable')
        # This stub tests packaging control flow only, not Docker or GitHub behavior.
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory)
            stub = d / 'docker'
            stub.write_text('''#!/usr/bin/env python3
import sys,os,json,pathlib
args=sys.argv[1:]
with open(os.environ['CALL_LOG'],'a') as f: f.write(json.dumps(args)+'\\n')
if args[:2]==['image','inspect']:
 if '--format' in args:
  print('linux/amd64' if '.Os' in args[args.index('--format')+1] else 'sha256:local-test-id')
elif args[0]=='run':
 mount=args[args.index('--mount')+1]
 context=pathlib.Path(mount.split('src=')[1].split(',dst=')[0])
 pathlib.Path(os.environ['CONTEXT_LOG']).write_text(str(context))
 assert set(p.relative_to(context).as_posix() for p in context.rglob('*') if p.is_file())=={'Dockerfile','.dockerignore','entrypoint.sh','github-mcp-server','tests/test_mcp.py','tests/test_startup.sh'}
 assert not context.stat().st_mode & 0o077 == 0
elif args[0]=='build':
 assert '--network=none' in args and '--pull=false' in args
 if os.environ.get('FAIL_BUILD'): sys.exit(3)
else: sys.exit(4)
''')
            stub.chmod(0o755)
            outside = d / 'external executable'
            outside.write_bytes(binary.read_bytes())
            outside.chmod(0o755)
            env = dict(os.environ, PATH=str(d) + ':' + os.environ['PATH'],
                       RUNTIME_BASE_IMAGE='approved:local', CALL_LOG=str(d / 'calls'),
                       CONTEXT_LOG=str(d / 'context'))
            for failure in (False, True):
                env['FAIL_BUILD'] = '1' if failure else ''
                result = subprocess.run([str(ROOT / 'build.sh'), './external executable', 'custom:local'],
                                        cwd=d, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, not failure, result.stderr)
                self.assertFalse(Path((d / 'context').read_text()).exists())
                if not failure:
                    self.assertIn('Image: custom:local', result.stdout)

    def test_real_startup_modes_and_http(self):
        binary = ROOT / 'bin/github-mcp-server'
        if not binary.exists():
            self.skipTest('Official local binary unavailable')
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, MCP_BINARY=str(binary), GITHUB_HOST='https://127.0.0.1:9',
                       CONTAINER_TMP_DIR=directory, CONTAINER_LOG_DIR=directory,
                       CONTAINER_REPO_PATH=str(ROOT), GITHUB_TOOLSETS='repos,pull_requests',
                       GITHUB_PERSONAL_ACCESS_TOKEN='test-secret-sentinel', GITHUB_READ_ONLY='true',
                       GITHUB_LOCKDOWN_MODE='false', TEST_TIMEOUT_SECONDS='2', TEST_RETRIES='1',
                       TEST_REPO_OWNER='owner', TEST_REPO_NAME='repo', TEST_REPO_BRANCH='main',
                       TEST_REPO_FILE_PATH='README.md', TEST_PR_NUMBER='1',
                       MCP_SCOPE_CHALLENGE='false', MCP_BIND_HOST='127.0.0.1',
                       NO_PROXY='127.0.0.1', no_proxy='127.0.0.1', PRESTART_TEST_MODE='live')
            failed = subprocess.run([str(ROOT / 'entrypoint.sh')], env=env,
                                    capture_output=True, text=True, timeout=20)
            self.assertNotEqual(failed.returncode, 0)
            self.assertNotIn('Prestart complete', failed.stdout)
            self.assertNotIn('test-secret-sentinel', failed.stdout + failed.stderr)
            self.assertIn('Real MCP initialize', failed.stdout)
            # Real HTTP listener test on fixed upstream port; skip if already in use.
            probe = socket.socket()
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(('127.0.0.1', 8082))
            except OSError:
                self.skipTest('Internal port 8082 is already in use')
            finally:
                probe.close()
            env['PRESTART_TEST_MODE'] = 'offline'
            with tempfile.TemporaryFile() as output:
                process = subprocess.Popen([str(ROOT / 'entrypoint.sh')], env=env,
                                           stdout=output, stderr=output,
                                           preexec_fn=lambda: harness.no_network(allow_listener=True))
                try:
                    for _ in range(150):
                        self.assertIsNone(process.poll(), 'HTTP startup exited early')
                        try:
                            connection = http.client.HTTPConnection('127.0.0.1', 8082, timeout=1)
                            body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                                               'params': {'protocolVersion': '2025-03-26', 'capabilities': {},
                                                          'clientInfo': {'name': 'local-test', 'version': '1'}}})
                            connection.request('POST', '/mcp', body, headers={
                                'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'})
                            response = connection.getresponse()
                            self.assertEqual(response.status, 401)
                            response.read()
                            connection.close()
                            break
                        except OSError:
                            time.sleep(0.05)
                    else:
                        self.fail('HTTP listener never became ready')
                    # Real Streamable HTTP MCP handshake with a client-supplied dummy PAT.
                    # Outbound connect is denied for the server: no GitHub calls can leave.
                    connection = http.client.HTTPConnection('127.0.0.1', 8082, timeout=3)
                    connection.request('POST', '/mcp', body, headers={
                        'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
                        'Authorization': 'Bearer ghp_' + 'A' * 36})
                    response = connection.getresponse()
                    reply = response.read().decode()
                    connection.close()
                    self.assertEqual(response.status, 200, reply)
                    if response.getheader('Content-Type', '').startswith('text/event-stream'):
                        reply = '\n'.join(line[6:] for line in reply.splitlines() if line.startswith('data: '))
                    initialized = json.loads(reply)
                    self.assertEqual(initialized['id'], 1)
                    self.assertIn('serverInfo', initialized['result'])
                    connection.close()
                    self.assertNotIn(b'GITHUB_PERSONAL_ACCESS_TOKEN=',
                                     Path('/proc/%d/environ' % process.pid).read_bytes())
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                self.assertEqual(process.returncode, 0, 'SIGTERM was not handled gracefully')
                output.seek(0)
                logs = output.read().decode()
                self.assertIn('operations were NOT verified.', logs)
                self.assertNotIn('test-secret-sentinel', logs)

    def test_dynamic_loader_check(self):
        python = Path(sys.executable).resolve()
        if python.read_bytes()[:4] != b'\x7fELF':
            self.skipTest('Local Python is not ELF')
        # Python is a real dynamically linked executable, but not an MCP server.
        # The library resolution should pass before HTTP-capability rejection.
        import io
        from contextlib import redirect_stdout
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(harness.Failure):
            harness.inspect_binary(python, execute=True)
        self.assertIn('Dynamic loader resolved required shared libraries', output.getvalue())

    def test_local_git_integrity_and_ca(self):
        binary = ROOT / 'bin/github-mcp-server'
        if not shutil.which('git') or not binary.exists():
            self.skipTest('Git or official executable unavailable')
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / 'repo'
            repo.mkdir()
            subprocess.run(['git', '-C', str(repo), 'init', '-q'], check=True)
            (repo / 'README.md').write_text('Local integrity test\n')
            subprocess.run(['git', '-C', str(repo), 'add', 'README.md'], check=True)
            subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test',
                            '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'test'], check=True)
            env = dict(os.environ, MCP_BINARY=str(binary), PRESTART_TEST_MODE='offline',
                       GITHUB_HOST='https://internal.example', GITHUB_TOOLSETS='repos,pull_requests',
                       CONTAINER_REPO_PATH=str(repo), CONTAINER_TMP_DIR=directory,
                       CONTAINER_LOG_DIR=directory)
            tested = subprocess.run([str(ROOT / 'tests/test_startup.sh')], env=env,
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(tested.returncode, 0, tested.stderr)
            self.assertIn('Local Git object integrity passed', tested.stdout)
            self.assertTrue((Path(directory) / 'ca-bundle.pem').exists())

    def test_ascii_sources(self):
        for path in ROOT.rglob('*'):
            if path.is_file() and not any(x in path.parts for x in ('bin', 'tmp', 'logs', '__pycache__', '.git')):
                path.read_bytes().decode('ascii')


if __name__ == '__main__':
    unittest.main()
