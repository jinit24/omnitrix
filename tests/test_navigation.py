import pytest
from omnitrix.navigation import list_resources


def test_manager_can_discover_files_beyond_first_page(tmp_path):
    for i in reversed(range(95)):
        (tmp_path/f'file-{i:03d}.md').write_text('source')
    (tmp_path/'.git').mkdir()
    first=list_resources(tmp_path)
    second=list_resources(tmp_path,offset=first['next_offset'])
    third=list_resources(tmp_path,offset=second['next_offset'])
    names=[e['name'] for p in (first,second,third) for e in p['entries']]
    assert len(names)==95 and names==sorted(names)
    assert third['next_offset'] is None
    assert list_resources(tmp_path,query='094')['entries'][0]['name']=='file-094.md'


def test_navigation_does_not_expose_runtime_or_external_symlinks(tmp_path):
    (tmp_path/'.omnitrix').mkdir()
    (tmp_path/'link').symlink_to('/etc')
    assert not list_resources(tmp_path)['entries']
    with pytest.raises(ValueError): list_resources(tmp_path,directory='.omnitrix')
    with pytest.raises(ValueError): list_resources(tmp_path,directory='../')


def test_research_can_locate_late_source_lines_without_claiming_evidence(tmp_path):
    from omnitrix.store import Store
    from omnitrix.research import Research
    from omnitrix.research_models import ResearchPlan
    from omnitrix.knowledge import ResearchTools
    store=Store(tmp_path/'state.db')
    (tmp_path/'large.c').write_text('\n'.join('target' if i>150 else 'other' for i in range(200)))
    goal=store.create_goal('Investigate source',['Cite evidence'],tmp_path)
    job=Research(store).plan(goal,'discovery',ResearchPlan(scope=['large.c'],question='Where is the relevant behavior?'))['job_id']
    store.execute("INSERT INTO research_runs(id,job_id,status,created) VALUES('run',?,'running',0)",(job,))
    tools=ResearchTools(store,'run',str(tmp_path),['large.c'],4)
    found=tools.locate('large.c','target')
    assert found['matches'][0]['line']==152 and found['total']==49
    assert found['next_offset']==20
    assert tools.evidence()==[]
    assert tools.locate('large.c','target',offset=20)['matches'][0]['line']==172
    (tmp_path/'outside.c').write_text('target')
    with pytest.raises(ValueError,match='scope'): tools.locate('outside.c','target')


def test_wide_source_navigation_is_bounded_and_scoped(tmp_path):
    from omnitrix.navigation import find_resources, search_workspace
    (tmp_path/'sources').mkdir()
    for i in range(50): (tmp_path/'sources'/f'code-{i:02d}.go').write_text('Needle contract\nother line')
    assert find_resources(tmp_path,'code-')['total']==50
    assert len(find_resources(tmp_path,'code-',offset=40)['paths'])==10
    result=search_workspace(tmp_path,'needle')
    assert len(result['matches'])==20 and result['next_offset']==20
    assert all(m['line']==1 for m in result['matches'])
    with pytest.raises(ValueError): search_workspace(tmp_path,'needle','../')
