#!/usr/bin/env bash
# Validate templates and CLI contracts. No services, credentials or state are changed.
set -euo pipefail
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${OMNITRIX_PYTHON:-$repo_root/.venv/bin/python}"
cd "$repo_root"
bash -n deploy/check.sh
"$python_bin" - <<'PY'
from configparser import ConfigParser
from pathlib import Path
import os
import subprocess
import tempfile
import shutil
import sys

root = Path.cwd()
for name in ('omnitrix-dashboard.service', 'omnitrix-manager@.service'):
    parser = ConfigParser(interpolation=None)
    parser.read(root / 'deploy' / name)
    service = parser['Service']
    assert service['User'] == 'omnitrix'
    assert service['EnvironmentFile'] == '/etc/omnitrix.env'
    assert service['Restart'] == 'on-failure'
    assert service['StateDirectory'] == 'omnitrix'
    assert service['ReadWritePaths'] == '/var/lib/omnitrix'
    assert '--db ${OMNITRIX_DB}' in service['ExecStart']
    if 'dashboard' in name:
        assert '--host 127.0.0.1' in service['ExecStart']
        for flag in ('--auth-users', '--auth-sessions', '--secure-cookies', '--workspace-root'):
            assert flag in service['ExecStart']
    else:
        assert 'run %i --steps 0' in service['ExecStart']
        assert service['KillSignal'] == 'SIGINT'
    if shutil.which('systemd-analyze'):
        with tempfile.TemporaryDirectory(prefix='omnitrix-units-') as folder:
            unit = Path(folder) / name.replace('@.', '@check.')
            text = (root / 'deploy' / name).read_text().replace('/opt/omnitrix/.venv/bin/python', sys.executable)
            unit.write_text(text)
            subprocess.run(['systemd-analyze', 'verify', '--man=no', str(unit)], check=True)
for command in ('dashboard', 'run'):
    help_text = subprocess.check_output([sys.executable, '-m', 'omnitrix.cli', command, '--help'], text=True)
    flags = ('--auth-users', '--auth-sessions', '--secure-cookies', '--workspace-root') if command == 'dashboard' else ('--steps', '--hours')
    assert all(flag in help_text for flag in flags)
assert '{$OMNITRIX_DOMAIN}' in (root / 'deploy/Caddyfile').read_text()
assert 'reverse_proxy 127.0.0.1:8765' in (root / 'deploy/Caddyfile').read_text()
print('Service templates and CLI contracts verified. No deployment performed.')
caddy = os.getenv('OMNITRIX_CADDY') or shutil.which('caddy')
if caddy:
    with tempfile.TemporaryDirectory(prefix='omnitrix-caddy-check-') as folder:
        env = {'PATH': os.getenv('PATH', ''), 'OMNITRIX_DOMAIN': 'localhost',
               'XDG_DATA_HOME': folder, 'XDG_CONFIG_HOME': folder}
        subprocess.run([caddy, 'adapt', '--validate', '--config', 'deploy/Caddyfile', '--adapter', 'caddyfile'],
                       env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    print('Caddy HTTPS proxy configuration verified without starting a service.')
else:
    print('Caddy is unavailable here; validate its configuration on the target host before enabling it.')
PY
