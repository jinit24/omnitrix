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


def test_context_proposal_is_reviewable_and_not_applied(tmp_path):
    command = [sys.executable, '-m', 'omnitrix.cli', '--db', str(tmp_path/'state.db')]
    goal=subprocess.run(command+['create','Improve reliability','--criterion','Preserve contracts'],capture_output=True,text=True,check=True).stdout.strip()
    context={'brief':'Focus on retries','rubric':['Check idempotency']}
    file=tmp_path/'context.json'
    file.write_text(json.dumps(context))
    proposed=json.loads(subprocess.run(command+['context',goal,'--file',str(file)],capture_output=True,text=True,check=True).stdout)
    assert proposed['proposed_change']==context
    assert proposed['proposal']
    active=json.loads(subprocess.run(command+['context',goal],capture_output=True,text=True,check=True).stdout)
    assert active['version']==0
