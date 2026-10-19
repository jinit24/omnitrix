#!/usr/bin/env python3
"""Local HTTPS/login smoke check. Trust a temporary test CA only in this client."""
import argparse
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.request import build_opener, HTTPCookieProcessor, HTTPSHandler, Request

from omnitrix.dashboard import make_server
from omnitrix.web_auth import create_user


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--caddy', default=shutil.which('caddy'))
    args = parser.parse_args()
    if not args.caddy:
        parser.error('Provide an installed, verified Caddy binary with --caddy')
    with tempfile.TemporaryDirectory(prefix='omnitrix-https-') as directory:
        root = Path(directory)
        password = secrets.token_urlsafe(24)
        create_user(root / 'users.json', 'operator', password)
        server = make_server(root / 'state.db', port=0, workspace_root=root,
            auth_users=root / 'users.json', auth_sessions=root / 'sessions.db',
            secure_cookies=True, allow_control=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        config = root / 'Caddyfile'
        config.write_text('{\n admin off\n skip_install_trust\n auto_https disable_redirects\n}\n'
            f'localhost:{port} {{\n bind 127.0.0.1\n tls internal\n reverse_proxy 127.0.0.1:{server.server_port}\n}}\n')
        env = {key: value for key, value in os.environ.items() if key in ('PATH', 'TMPDIR', 'SSL_CERT_FILE', 'SSL_CERT_DIR')}
        env.update(XDG_DATA_HOME=str(root / 'data'), XDG_CONFIG_HOME=str(root / 'config'))
        with (root / 'caddy.log').open('w+') as log:
            process = subprocess.Popen([args.caddy, 'run', '--config', str(config), '--adapter', 'caddyfile'],
                env=env, stdout=log, stderr=log)
            try:
                cert = root / 'data/caddy/pki/authorities/local/root.crt'
                deadline = time.monotonic() + 10
                while not cert.is_file():
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError('Caddy did not create the temporary test CA')
                    time.sleep(.1)
                opener = build_opener(HTTPCookieProcessor(CookieJar()), HTTPSHandler(context=ssl.create_default_context(cafile=str(cert))))
                origin = f'https://localhost:{port}'
                def call(path, body=None, csrf=None):
                    headers = {'Origin': origin, 'Content-Type': 'application/json'}
                    if csrf:
                        headers['X-CSRF-Token'] = csrf
                    with opener.open(Request(origin + path, data=json.dumps(body).encode() if body is not None else None,
                                              headers=headers), timeout=5) as response:
                        return response.status, json.load(response), response.headers
                try:
                    call('/api/state')
                    raise AssertionError('Unauthenticated state was exposed')
                except HTTPError as error:
                    assert error.code == 401
                _, login, headers = call('/api/login', {'username': 'operator', 'password': password})
                cookie = headers['Set-Cookie']
                assert all(flag in cookie for flag in ('Secure', 'HttpOnly', 'SameSite=Strict'))
                goal = {'request_id': 'https-smoke', 'objective': 'Validate authenticated HTTPS',
                        'requirements': ['Preserve original criterion'], 'workspace': '.'}
                status, created, _ = call('/api/goals', goal, login['csrf_token'])
                assert status == 201 and created['started'] is False
                _, state, _ = call('/api/state')
                assert state['goals'][0]['original_requirements'] == goal['requirements']
                call('/api/logout', {}, login['csrf_token'])
                try:
                    call('/api/state')
                    raise AssertionError('Logout did not revoke the session')
                except HTTPError as error:
                    assert error.code == 401
                print('HTTPS, explicitly trusted test CA, secure login, CSRF goal creation and logout verified.')
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    main()
