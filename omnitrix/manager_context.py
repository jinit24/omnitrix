"""Deterministic manager views; complete reports remain in durable storage."""
import copy
import json
from .budget import BudgetError

CONTEXT_BYTES = 16 * 1024
TOOL_BYTES = 4 * 1024
HISTORY_BYTES = 12 * 1024
MODEL_INPUT_BYTES = 40 * 1024  # final serialized request has a separate 48 KiB guard


class ContextLimitError(BudgetError):
    pass


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def clip(value, limit=240):
    text = str(value)
    data = text.encode('utf-8')
    return text if len(data) <= limit else data[:limit].decode('utf-8','ignore') + '… [detail retained]'


def bounded_json(value, max_bytes=TOOL_BYTES):
    """Explicit preview when a tool page exceeds its allowance; never raw slicing."""
    rendered = json.dumps(value,ensure_ascii=False)
    if len(rendered.encode()) <= max_bytes:
        return rendered
    preview = {'truncated':True,'preview':clip(rendered,max_bytes//2),
               'next':'Use a narrower query or retrieve_record with an exact record ID and byte offset; full records remain durable.'}
    result = [preview] if isinstance(value,list) else preview
    while len(encoded(result)) > max_bytes:
        preview['preview'] = clip(preview['preview'],max(0,len(preview['preview'].encode())//2))
    return json.dumps(result,ensure_ascii=False,separators=(',',':'))


def card(record, keys, limit=240):
    result = {}
    for key in keys:
        if key not in record:
            continue
        value = record[key]
        if isinstance(value,str):
            result[key] = clip(value,limit)
        elif value is None or isinstance(value,(bool,int,float)):
            result[key] = value
        elif isinstance(value,list):
            result[key] = [clip(item,120) for item in value[:4]]
    return result


def message_card(message):
    result = card(message,('id','kind','task_id','state','direction','created'))
    result['detail_ref'] = {'section':'messages','id':message.get('id'),'task_id':message.get('task_id')}
    body = message.get('body',{})
    if isinstance(body,str):
        try:
            body = json.loads(body)
        except ValueError:
            return {**result,'body':clip(body),'body_is_preview':len(body.encode())>240}
    if not isinstance(body,dict):
        return result
    result.update(card(body,('job_id','run_id','status','summary','reason','error','feedback','recommendation')))
    report = body.get('report') or {}
    evaluation = body.get('evaluation') or {}
    if isinstance(report,dict):
        result['finding_cards'] = [card(finding,('statement','classification','evidence_ids'),160)
                                   for finding in report.get('findings',[])[:2] if isinstance(finding,dict)]
        result['uncertainties'] = [clip(value,120) for value in report.get('uncertainties',[])[:2]]
        result['contradictory_evidence'] = [clip(value,120) for value in report.get('contradictory_evidence',[])[:4]]
        result['contradictory_evidence_count'] = len(report.get('contradictory_evidence',[]))
        result['blocked'] = bool(report.get('blocked',False))
        if report.get('recommendation'):
            result['recommendation'] = clip(report['recommendation'])
    if isinstance(evaluation,dict):
        result['review'] = card(evaluation,('verdict','evidential_verdict','reason','feedback','workflow_issues'),160)
        result['review']['unmet_rubric_count'] = sum(item.get('verdict')=='unmet' for item in evaluation.get('rubric_assessments',[]) if isinstance(item,dict))
    return result


def manager_context(snapshot, *, max_bytes=CONTEXT_BYTES):
    # Mandatory contracts are copied whole. Unknown snapshot fields do not
    # enter the model context merely because a caller added them to status().
    context = {key:copy.deepcopy(snapshot[key]) for key in ('goal','research_context') if key in snapshot}
    if 'manager_repair' in snapshot:
        context['manager_repair'] = copy.deepcopy(snapshot['manager_repair'])
    if len(encoded(context)) > max_bytes:
        raise ContextLimitError('Current goal, rubric or repair contract exceeds the manager context byte ceiling; revise the contract explicitly')
    optional = {}
    optional['dollar_budget'] = card(snapshot.get('dollar_budget') or {},
        ('limit_usd','committed_usd','remaining_usd','reserved_usd','manager_limit_usd','manager_remaining_usd',
         'protected_review_usd','available_research_usd','requests','unresolved_requests'))
    optional['dollar_budget']['role_committed_usd'] = {
        role:value for role,value in (snapshot.get('dollar_budget') or {}).get('role_committed_usd',{}).items()
        if role in ('manager','manager_chat','discovery','research','evaluation') and isinstance(value,(int,float))}
    audit = snapshot.get('audit_coverage') or {}
    optional['audit_coverage'] = card(audit,('areas_total','top_level_areas','subareas_total','inventory_files','inventory_bytes','coverage_note'))
    optional['audit_coverage']['by_status'] = {status:value for status,value in audit.get('by_status',{}).items()
        if status in ('unexamined','triaged','investigating','sampled','deferred') and isinstance(value,int)}
    optional['tasks'] = [card(item,('id','status','priority','adapter','session_id','error')) for item in snapshot.get('tasks',[])[-12:]]
    optional['decisions'] = [card(item,('action','rationale','research_id','implementation_id','user_message'),180) for item in snapshot.get('decisions',[])[-6:]]
    optional['worker_messages'] = [message_card(item) for item in snapshot.get('worker_messages',[])[:3]]
    optional['pending_messages'] = [card(item,('id','kind','task_id','direction','state')) for item in snapshot.get('pending_messages',[])[:12]]
    optional['active'] = [card(item,('task_id','state','goal_revision','policy_revision','started','deadline'))
                          for item in snapshot.get('active',[]) if item.get('state') in ('running','waiting','uncertain','paused')][:8]
    optional['research_checkpoints'] = [card(item,('detail','created'),240) for item in snapshot.get('research_checkpoints',[])[:3]]
    optional['research'] = []
    for job in snapshot.get('research',[])[:10]:
        spec = job.get('spec') or {}
        if isinstance(spec,str):
            try:
                spec = json.loads(spec)
            except ValueError:
                spec = {}
        optional['research'].append(card(job,('id','stage','status','attempts')) |
            card(spec,('question','hypothesis','decision','stop_condition'),160) |
            {'detail_ref':{'section':'research','id':job.get('id')}})
    optional['opportunities'] = [card(item,('id','title','hypothesis','impact','confidence','risk','effort','actionable','scope_gaps'),160)
                                 for item in snapshot.get('opportunities',[])[:6]]
    optional['research_reviews'] = [card(item,('id','job_id','run_id','status','error')) for item in snapshot.get('research_reviews',[])[:4]]
    optional['manager_repairs'] = [card(item,('id','category','state','diagnostic','required_change','created'),240)
                                    for item in snapshot.get('manager_repairs',[])[:3]]
    campaign = snapshot.get('campaign') or {}
    optional['campaign'] = card(campaign,('queued_jobs','run_status','budgeted_deliverable_status','program_status','whole_audit_complete'))
    optional['campaign']['queue'] = {key:value for key,value in campaign.get('queue',{}).items() if isinstance(value,int)}
    optional['implementations'] = [card(item,('id','status','candidate','authorization_revoked','attempts','feedback'))
                                   for item in snapshot.get('implementations',[])[:4]]
    optional['research_budget'] = card(snapshot.get('research_budget') or {},('max_runs','spent'))
    # Lists are admitted card by card, leaving an explicit count of omitted
    # records and retrieval instructions. No mandatory text is truncated.
    omitted = {}
    context['context_meta'] = {'format':'manager-cards-v1','omitted':omitted,
        'detail':'Full reports and earlier records are available through recall_history/retrieve_record.'}
    if len(encoded(context)) > max_bytes:
        raise ContextLimitError('Mandatory manager contract leaves no room for context metadata')
    first = ('dollar_budget','manager_repairs','campaign','worker_messages','active','audit_coverage')
    for key in (*first,*(key for key in optional if key not in first)):
        value = optional[key]
        if isinstance(value,list):
            context[key] = []
            for index,item in enumerate(value):
                context[key].append(item)
                if len(encoded(context)) > max_bytes-256:
                    context[key].pop()
                    omitted[key] = len(value)-index
                    break
        else:
            context[key] = value
            if len(encoded(context)) > max_bytes-256:
                del context[key]
                omitted[key] = 1
    if len(encoded(context)) > max_bytes:
        raise ContextLimitError('Manager contract and omission metadata exceed context ceiling')
    return context


def manager_chat_context(snapshot, user_message, policy):
    mandatory = {'goal':snapshot.get('goal',{}),'research_context':snapshot.get('research_context',{}),
                 'policy':policy,'user_message':user_message}
    if snapshot.get('manager_repair'):
        mandatory['manager_repair'] = snapshot['manager_repair']
    if len(encoded(mandatory)) > CONTEXT_BYTES:
        raise ContextLimitError('Current chat goal, policy, rubric or user message exceeds the context ceiling')
    compact = manager_context(snapshot,max_bytes=max(1024,CONTEXT_BYTES-len(encoded({'policy':policy,'user_message':user_message}))))
    return {**compact,'policy':copy.deepcopy(policy),'user_message':user_message}


def _content_text(item):
    content = item.get('content')
    if isinstance(content,str):
        return content
    if isinstance(content,list) and len(content)==1 and isinstance(content[0],dict):
        return content[0].get('text')
    return None


def _snapshot_item(item):
    text = _content_text(item)
    if item.get('role') != 'user' or not isinstance(text,str):
        return False
    try:
        value = json.loads(text)
        return (isinstance(value,dict) and isinstance(value.get('goal'),dict) and
                (value.get('context_meta',{}).get('format')=='manager-cards-v1' or 'tasks' in value and 'decisions' in value))
    except ValueError:
        return False


def pair_items(items):
    calls = {item.get('call_id') for item in items if item.get('type')=='function_call'}
    outputs = {item.get('call_id') for item in items if item.get('type')=='function_call_output'}
    paired = calls & outputs
    return [item for item in items if item.get('type') not in ('function_call','function_call_output') or item.get('call_id') in paired]


def merge_manager_history(history,new_input):
    retained = [copy.deepcopy(item) for item in history if not _snapshot_item(item)]
    while retained and len(encoded(retained)) > HISTORY_BYTES:
        retained.pop(0)
        retained = pair_items(retained)
    return pair_items(retained + copy.deepcopy(new_input))


def manager_history_filter(data):
    """Limit every SDK turn, preserving the newest contract and complete pairs."""
    from agents.run import ModelInputData
    items = copy.deepcopy(data.model_data.input)
    snapshot_indexes = [index for index,item in enumerate(items) if _snapshot_item(item)]
    latest = snapshot_indexes[-1] if snapshot_indexes else None
    if latest is not None:
        items = [item for index,item in enumerate(items) if not _snapshot_item(item) or index==latest]
    for item in items:
        if item.get('type')=='function_call_output' and isinstance(item.get('output'),str):
            if len(item['output'].encode()) > TOOL_BYTES:
                try:
                    result = json.loads(item['output'])
                except ValueError:
                    result = item['output']
                item['output'] = bounded_json(result)
    def history_size():
        return len(encoded([item for item in items if not _snapshot_item(item)]))
    while history_size() > HISTORY_BYTES:
        removable = next((index for index,item in enumerate(items) if not _snapshot_item(item) and item.get('role') not in ('system','developer')),None)
        if removable is None:
            raise ContextLimitError('Mandatory manager history exceeds its byte ceiling')
        removed = items.pop(removable)
        if removed.get('type') in ('function_call','function_call_output'):
            items = [item for item in items if item.get('call_id') != removed.get('call_id')]
        items = pair_items(items)
    instructions = data.model_data.instructions
    def size():
        return len(encoded(items)) + len((instructions or '').encode())
    while size() > MODEL_INPUT_BYTES:
        removable = next((index for index,item in enumerate(items) if not _snapshot_item(item) and item.get('role') not in ('system','developer')),None)
        if removable is None:
            raise ContextLimitError('Current instructions and mandatory contract exceed the assembled model input ceiling')
        removed = items.pop(removable)
        if removed.get('type') in ('function_call','function_call_output'):
            items = [item for item in items if item.get('call_id') != removed.get('call_id')]
        items = pair_items(items)
    return ModelInputData(input=pair_items(items),instructions=instructions)
