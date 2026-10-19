from omnitrix.control import Control
from omnitrix.store import Store


def test_control_initialization_does_not_build_a_history_snapshot(tmp_path, monkeypatch):
    store = Store(tmp_path / 'state.db')
    goal = store.create_goal('Preserve task contracts', ['Keep source evidence'], tmp_path)
    monkeypatch.setattr(store, 'snapshot', lambda *_: (_ for _ in ()).throw(
        AssertionError('Initialization must not materialize the entire history')))
    current = Control(store).current(goal)
    assert current['goal']['requirements'] == ['Keep source evidence']
    assert current['goal']['revision'] == 1


def test_control_initialization_does_not_commit_callers_admission_transaction(tmp_path):
    store = Store(tmp_path / 'state.db')
    goal = store.create_goal('Atomic admission', ['Never partially admit plans'], tmp_path)
    store.db.execute('BEGIN IMMEDIATE')
    Control(store).current(goal)
    assert store.db.in_transaction
    assert store.db.execute('SELECT count(*) FROM goal_versions').fetchone()[0] == 1
    store.db.rollback()
    assert store.db.execute('SELECT count(*) FROM goal_versions').fetchone()[0] == 0
    assert store.db.execute('SELECT count(*) FROM policy_versions').fetchone()[0] == 0
