"""Reporting must preserve assessment history and distinguish observed coverage."""
import hashlib
import json
from pathlib import Path
import sqlite3

from scripts import public_research_report as reporting


def fixture(tmp_path):
    directory = tmp_path / 'run'
    directory.mkdir()
    workspace = directory / 'workspace'
    workspace.mkdir()
    (workspace / 'chunk-1').write_text('first\nsecond\n')
    (workspace / 'chunk-2').write_text('eighty-first\neighty-second\n')
    (workspace / 'catalog').write_text('Navigation metadata\n')
    digest = lambda name: hashlib.sha256((workspace/name).read_bytes()).hexdigest()
    snapshot = {'repository': 'httpd', 'origin': 'https://github.com/apache/httpd.git', 'commit': 'a'*40,
        'scope': 'whole_repository', 'areas': [{'path': 'modules'}],
        'inventory': [{'path': 'modules/a.c', 'included': True}, {'path': 'logo.png', 'included': False, 'reason': 'binary NUL content'}],
        'chunks': [{'locator': 'chunk-1', 'original_path': 'modules/a.c', 'original_start_line': 1, 'sha256': digest('chunk-1')},
                   {'locator': 'chunk-2', 'original_path': 'modules/a.c', 'original_start_line': 81, 'sha256': digest('chunk-2')}]}
    (directory/'source-snapshot.json').write_text(json.dumps(snapshot))
    (directory/'experiment.json').write_text(json.dumps({'repository': 'httpd', 'goal_id': 'g', 'model': 'test-model', 'state': 'stopped'}))
    (directory/'control-probe.json').write_text(json.dumps({'reply_persisted': True, 'delivery_seconds': .02}))
    with sqlite3.connect(directory/'state.db') as db:
        db.executescript('''
        CREATE TABLE goals(id,status);
        CREATE TABLE research_jobs(id,spec,stage,status);
        CREATE TABLE research_runs(id,job_id,status,report,evaluation,created,finished,feedback);
        CREATE TABLE research_reviews(id,job_id,run_id,status,evaluation,created,finished,feedback,context);
        CREATE TABLE research_sources(id,locator,revision,excerpt,observed);
        CREATE TABLE dollar_budgets(limit_nano,prices);
        CREATE TABLE dollar_requests(id,role,state,charged_nano,input_tokens,output_tokens,error,created,finished);
        CREATE TABLE audit_areas(path,files,bytes,kind,source_prefixes);
        CREATE TABLE audit_area_updates(area,revision,status,priority,rationale,next_question,job_id);
        CREATE TABLE activity(id,kind,detail,created);
        CREATE TABLE messages(kind,state);
        ''')
        db.execute('INSERT INTO goals VALUES(?,?)', ('g','paused'))
        spec = {'hypothesis': 'A proposed contract is violated', 'scope': ['chunk-1','chunk-2'], 'question': 'Investigate contract', 'decision': 'Decide whether to validate', 'expected_value': 'Resolve uncertainty', 'stop_condition': 'Contract resolved'}
        db.execute('INSERT INTO research_jobs VALUES(?,?,?,?)',('job',json.dumps(spec),'research','falsified'))
        report = {'findings':[{'statement':'<script>not executable</script> Source contradicts the claim', 'classification':'claim', 'evidence_ids':['s1']}], 'uncertainties':['No runtime validation'], 'recommendation':'Retire this hypothesis'}
        original_eval = {'verdict':'insufficient', 'evidential_verdict':'falsified', 'reason':'Process attestation absent', 'workflow_issues':['Checkpoint missing from evaluator packet'], 'feedback':'Retain evidential result'}
        new_eval = {'verdict':'falsified', 'evidential_verdict':'falsified', 'reason':'Recorded checkpoint now included', 'feedback':'Investigate a different question'}
        db.execute('INSERT INTO research_runs VALUES(?,?,?,?,?,?,?,?)',('run','job','escalated',json.dumps(report),json.dumps(original_eval),1000,1010,'Original workflow did not pass'))
        db.execute('INSERT INTO research_reviews VALUES(?,?,?,?,?,?,?,?,?)',('review','job','run','falsified',json.dumps(new_eval),1020,1030,'New review of retained report',json.dumps({'report':report})))
        for item in [('s1','chunk-1','1: first\n2: second'),('s2','chunk-1','2: second'),('s3','chunk-2','1: eighty-first\n2: eighty-second'),('meta','catalog','1: Navigation metadata')]:
            db.execute('INSERT INTO research_sources VALUES(?,?,?,?,?)',(item[0],item[1],digest(item[1]),item[2],1005))
        db.execute('INSERT INTO dollar_budgets VALUES(?,?)',(25_000_000_000,'{}'))
        db.executemany('INSERT INTO dollar_requests VALUES(?,?,?,?,?,?,?,?,?)',[
            ('r1','manager','settled',100_000_000,100,20,None,999,1000),
            ('r2','manager','uncertain',200_000_000,None,None,'Provider HTTP 400',998,999)])
        db.execute('INSERT INTO audit_areas VALUES(?,?,?,?,?)',('modules',1,20,'repository area','["sources/full/modules"]'))
        db.execute('INSERT INTO audit_area_updates VALUES(?,?,?,?,?,?,?)',('modules',1,'triaged',1,'High impact, no coverage claim','Inspect another boundary',None))
        db.execute('INSERT INTO activity VALUES(?,?,?,?)',(1,'structured_contract_diagnosed',json.dumps({'message':'11 sources exceeded the 8-source focused limit'}),1015))
        db.execute('INSERT INTO messages VALUES(?,?)',('control','acknowledged'))
    return directory


def test_fresh_read_only_report_keeps_original_and_review_and_line_mapping(tmp_path):
    directory = fixture(tmp_path)
    before = (directory/'state.db').read_bytes()
    report = reporting.read_repository(directory)
    assert (directory/'state.db').read_bytes() == before
    assert report['counts']['inventory_paths'] == 2
    assert report['counts']['excluded_paths'] == 1
    assert report['counts']['original_files_captured'] == 1
    assert report['counts']['unique_original_lines_captured'] == 4
    assert report['counts']['metadata_evidence_records'] == 1
    assert report['captured_files'][0]['line_ids'] == [1,2,81,82]
    assert report['evidence']['s3']['source_url'].endswith('/modules/a.c#L81-L82')
    assert report['counts']['distinct_hypotheses_evaluated'] == 1
    assert report['counts']['independent_evaluations'] == 2
    assert report['counts']['latest_overall_verdicts'] == {'falsified':1}
    assert report['assessments'][0]['status'] == 'escalated'
    assert report['assessments'][0]['evaluation']['verdict'] == 'insufficient'
    assert report['assessments'][1]['kind'] == 'additional_review'
    assert report['assessments'][1]['evaluation']['verdict'] == 'falsified'
    assert report['budget']['committed_usd'] == .3
    assert report['budget']['measured_usage_estimate_usd'] == .1
    assert report['budget']['uncertain_reservations_usd'] == .2
    assert report['control_probe']['delivery_seconds'] == .02
    assert len(report['incidents']) == 2


def test_standalone_output_escapes_source_and_reports_actual_limits(tmp_path):
    directory = fixture(tmp_path)
    output = tmp_path/'output'
    result = reporting.generate([directory],output)
    assert Path(result['report']).is_file()
    rendered = (output/'audit-report.html').read_text()
    summary = json.loads((output/'summary.json').read_text())
    assert '<script>not executable</script>' not in rendered
    assert '&lt;script&gt;not executable&lt;/script&gt;' in rendered
    assert 'Whole-repository inventory' in rendered
    assert 'Actual captured research evidence' in rendered
    assert 'Overall workflow verdict' in rendered and 'Evidential verdict' in rendered
    assert 'Original workflow did not pass' in rendered
    assert 'New review of retained report' in rendered
    assert 'Not a provider invoice' in rendered
    assert summary['claims']['runtime_validated_bugs'] is False
    assert summary['claims']['exhaustive_semantic_audit'] is False
    assert summary['repositories'][0]['counts']['verification_runs'] == 0


def test_http400_causes_require_specific_retained_diagnostics():
    incidents = reporting.http_error_incidents([
        {'id':'initial', 'error':'BudgetError: Provider HTTP 400; outcome uncertain and reservation retained'},
        {'id':'history', 'error':'Provider HTTP 400: No tool call found for function call output with call_id call_example'},
        {'id':'reasoning', 'error':"Provider HTTP 400: Unsupported value 'none' for reasoning.effort; supported values are 'low' and 'high'"},
        {'id':'unknown', 'error':'Provider HTTP 400: Bad request'},
        {'id':'other', 'error':'Provider HTTP 500: Internal error'}])
    by_cause = {incident['kind']:incident for incident in incidents}
    assert by_cause['http400_unclassified']['request_ids'] == ['initial','unknown']
    assert by_cause['orphan_function_call_output']['request_ids'] == ['history']
    assert by_cause['unsupported_reasoning_effort']['request_ids'] == ['reasoning']
    assert 'reasoning' not in by_cause['http400_unclassified']['description']
    assert sum(incident['http400_requests'] for incident in incidents) == 4


def test_completion_check_separates_current_eligibility_from_sample_history(tmp_path):
    directory = fixture(tmp_path)
    completion = {'checked_at_utc':'2026-10-18T19:00:00+00:00', 'top_level_areas':1,
                  'recorded_sampled_top_level':1, 'currently_eligible_top_level':0,
                  'gaps':[{'path':'modules','status':'sampled','reason':'Source revision changed; recorded sample is no longer eligible'}],
                  'complete_coverage_floor_met':False}
    (directory/'completion-check.json').write_text(json.dumps(completion))
    output = tmp_path/'output'
    reporting.generate([directory],output)
    summary = json.loads((output/'summary.json').read_text())
    assert summary['repositories'][0]['completion_check'] == completion
    rendered = (output/'audit-report.html').read_text()
    assert 'Recorded sampled history' in rendered
    assert 'Currently eligible samples' in rendered
    assert 'not met' in rendered
    assert 'Source revision changed; recorded sample is no longer eligible' in rendered
    assert 'Current completion gap reasons' in rendered
    assert 'Recorded priorities, gaps and next questions' in rendered
