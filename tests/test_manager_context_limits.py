import copy
import json
from types import SimpleNamespace

import pytest
from omnitrix.manager_context import (manager_context, manager_chat_context, manager_history_filter,
    merge_manager_history, bounded_json, encoded, ContextLimitError, CONTEXT_BYTES, HISTORY_BYTES, TOOL_BYTES)


def snapshot():
    return {'goal':{'id':'goal','objective':'Keep correctness','requirements':['Never lose mandatory criteria'],'constraints':['No side effects']},
            'research_context':{'version':2,'brief':'Use authoritative evidence','rubric':['Address contradictions']},
            'tasks':[],'decisions':[]}


def test_whitelist_cards_bounded_unknown_fields_reports_and_budget_refs():
    state = snapshot()
    state.update(unexpected_snapshot_payload='secret'*100000,
        worker_messages=[{'id':'message','task_id':'task','kind':'findings','body':json.dumps({
            'job_id':'job','status':'supported','report':{'findings':[{'statement':'evidence'*10000,'classification':'claim','evidence_ids':['ev1']}],
                'recommendation':'x'*10000,'uncertainties':['Remaining unknown']},'evaluation':{'verdict':'supported','reason':'Read evidence'}})}]*10,
        opportunities=[{'id':str(i),'title':'x'*10000,'hypothesis':'x'*10000,'impact':5} for i in range(30)],
        campaign={'queue':{'ready':3},'queued_jobs':3,'run_status':'working','unexpected':'x'*10000},
        manager_repairs=[{'id':'repair','category':'schema','diagnostic':'x'*10000,'state':'pending'}])
    original = copy.deepcopy(state)
    result = manager_context(state)
    assert len(encoded(result)) <= CONTEXT_BYTES
    assert result['goal'] == state['goal']
    assert result['research_context'] == state['research_context']
    assert 'unexpected_snapshot_payload' not in result
    assert 'body' not in result['worker_messages'][0]
    assert result['worker_messages'][0]['job_id'] == 'job'
    assert result['worker_messages'][0]['detail_ref']['id'] == 'message'
    assert result['campaign']['queue']['ready'] == 3
    assert 'unexpected' not in result['campaign']
    assert state == original


def test_oversized_mandatory_contract_fails_explicitly_without_truncating():
    state = snapshot()
    state['research_context']['brief'] = 'important' * 10000
    original = copy.deepcopy(state)
    with pytest.raises(ContextLimitError,match='rubric'):
        manager_context(state)
    assert state == original


def test_chat_preserves_policy_goal_rubric_and_user_message():
    state = snapshot()
    policy = {'version':7,'instructions':'Preserve these rules exactly'}
    prompt = 'Prioritize the important question'
    result = manager_chat_context(state,prompt,policy)
    assert result['policy'] == policy
    assert result['goal'] == state['goal']
    assert result['research_context'] == state['research_context']
    assert result['user_message'] == prompt
    with pytest.raises(ContextLimitError):
        manager_chat_context(state,'x'*20000,policy)


def test_bounded_tool_outputs_retain_explicit_truncation_and_list_shape():
    for data in ([{'detail':'\u263a'*10000}], {'detail':'x'*10000}):
        output = bounded_json(data)
        assert len(output.encode()) <= TOOL_BYTES
        decoded = json.loads(output)
        preview = decoded[0] if isinstance(decoded,list) else decoded
        assert preview['truncated']
        assert 'durable' in preview['next']


def test_each_turn_caps_history_and_pairs_without_mutating_contract_or_session():
    state = snapshot()
    mandatory = {'role':'user','content':json.dumps(manager_context(state))}
    items = [mandatory]
    for index in range(30):
        items += [{'type':'function_call','name':'read','arguments':'{}','call_id':str(index)},
                  {'type':'function_call_output','call_id':str(index),'output':json.dumps({'source':'x'*10000})}]
    before = copy.deepcopy(items)
    result = manager_history_filter(SimpleNamespace(model_data=SimpleNamespace(input=items,instructions='Mandatory instructions')))
    assert mandatory in result.input
    history = [item for item in result.input if item != mandatory]
    assert len(encoded(history)) <= HISTORY_BYTES
    calls = {item['call_id'] for item in history if item.get('type')=='function_call'}
    outputs = {item['call_id'] for item in history if item.get('type')=='function_call_output'}
    assert calls == outputs
    assert result.instructions == 'Mandatory instructions'
    assert items == before
    assert all(len(item['output'].encode()) <= TOOL_BYTES for item in history if item.get('type')=='function_call_output')


def test_fresh_repair_contract_survives_and_huge_instructions_fail():
    state = snapshot()
    state['manager_repair'] = {'ticket_id':'ticket','category':'schema','diagnostic':'Fix shape','required_change':'Use allowed enum','fresh_context':True}
    compact = manager_context(state)
    assert compact['manager_repair'] == state['manager_repair']
    item = {'role':'user','content':json.dumps(compact)}
    with pytest.raises(ContextLimitError):
        manager_history_filter(SimpleNamespace(model_data=SimpleNamespace(input=[item],instructions='x'*50000)))
