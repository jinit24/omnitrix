import json
import subprocess
import sys


def test_create_and_inspect_in_separate_processes(tmp_path):
    command = [sys.executable, '-m', 'omnitrix.cli', '--db', str(tmp_path / 'state.db')]
    created = subprocess.run(command + ['create', 'Investigate startup', '--criterion',
        'Cite entry point', '--workspace', str(tmp_path)], capture_output=True, text=True, check=True)
    inspected = subprocess.run(command + ['status', created.stdout.strip()],
        capture_output=True, text=True, check=True)
    state = json.loads(inspected.stdout)
    assert state['goal']['objective'] == 'Investigate startup'
    assert state['goal']['requirements'] == ['Cite entry point']
    assert state['goal']['status'] == 'active'
