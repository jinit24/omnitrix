#!/usr/bin/env python3
"""Run the reproducible fault-injection suite and emit machine-readable outcomes."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
CASES={
 'kill_worker':'tests/test_interactive.py::test_stall_and_worker_death_are_uncertain_not_success',
 'restart_manager':'tests/test_recovery.py::test_sigkill_manager_preserves_ownership_messages_and_conversation',
 'update_requirements_and_stale_result':'tests/test_interactive.py::test_requirements_revision_invalidates_only_affected_work',
 'repeated_verification_failure':'tests/test_implementation.py::test_repeated_local_failure_exhausts_budget',
 'misleading_research':'tests/test_research.py::test_unsupported_claims_fail_even_if_evaluator_trusts_them',
 'lost_external_ack':'tests/test_interactive.py::test_lost_ack_is_reconciled_not_replayed',
 'lost_git_ack':'tests/test_implementation.py::test_lost_worktree_ack_reconciles_existing_checkout',
 'waiting_dependency':'tests/test_interactive.py::test_waiting_worker_does_not_spend_manager_tokens',
 'false_activity':'tests/test_recovery.py::test_tokens_and_duplicate_activity_are_not_meaningful_progress',
}
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--output',type=Path,default=ROOT/'.omnitrix/faults')
args=p.parse_args();args.output.mkdir(parents=True,exist_ok=True)
started=time.monotonic()
completed=subprocess.run([sys.executable,'-m','pytest','-q',*CASES.values(),f'--junitxml={args.output / "results.xml"}'],cwd=ROOT)
root=ET.parse(args.output/'results.xml').getroot()
cases=[]
for case in root.iter('testcase'):
 cases.append({'name':case.attrib['name'],'seconds':float(case.attrib.get('time',0)),
               'outcome':'failed' if case.find('failure') is not None or case.find('error') is not None else 'skipped' if case.find('skipped') is not None else 'passed'})
report={'mode':'deterministic fault injection, not live Codex','wall_seconds':time.monotonic()-started,
        'exit_code':completed.returncode,'cases':cases,'covered_faults':list(CASES)}
(args.output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
raise SystemExit(completed.returncode)
