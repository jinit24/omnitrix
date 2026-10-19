import pytest
from pydantic import TypeAdapter, ValidationError

from omnitrix.models import Decision, ScheduledResearch


def task(stage='research'):
    plan = {'scope': ['source.md'], 'question': 'Which contract needs validation?'}
    if stage == 'research':
        plan.update(hypothesis='The source preserves the recorded error',
                    falsification='A later assignment clears that error')
    return {'stage': stage, 'plan': plan, 'priority': 2, 'instructions': 'Trace the final exit'}


def test_scheduled_stage_exposes_the_correct_plan_schema():
    adapter = TypeAdapter(ScheduledResearch)
    assert adapter.validate_python(task()).plan.hypothesis
    invalid = task('discovery')
    invalid['stage'] = 'research'
    with pytest.raises(ValidationError):
        adapter.validate_python(invalid)
    invalid = task('research')
    invalid['stage'] = 'discovery'
    with pytest.raises(ValidationError):
        adapter.validate_python(invalid)


def test_strategy_batch_is_bounded_in_model_schema():
    fields = dict(action='schedule_research', rationale='Advance the persistent frontier',
                  instructions='Trace independent contracts', task_id=None, priority=2)
    assert len(Decision(**fields, research_batch=[task()] * 6).research_batch) == 6
    with pytest.raises(ValidationError):
        Decision(**fields, research_batch=[task()] * 7)
