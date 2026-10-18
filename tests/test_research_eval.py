"""A completed trial with failed research checks must not be a green test run."""
import runpy
from pathlib import Path

evaluation_exit_code = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/research_eval.py'))['evaluation_exit_code']


def test_completed_trial_with_bad_research_exits_nonzero():
    assert evaluation_exit_code([{'status': 'completed', 'score': {'passed': 5, 'total': 8}}]) == 1
    assert evaluation_exit_code([]) == 1


def test_blocked_trial_is_distinct_from_passed_and_failed_behavior():
    assert evaluation_exit_code([{'status': 'blocked', 'score': {'passed': None, 'total': 8}}]) == 2
    assert evaluation_exit_code([{'status': 'completed', 'score': {'passed': 8, 'total': 8}}]) == 0
