"""Durable execution interface. Calls are serial under the manager lock."""
import asyncio
import uuid
from .store import Store
from .workers import Worker


class Execution:
    def __init__(self, store: Store, worker: Worker):
        self.store, self.worker = store, worker

    def status(self, task_id):
        return self.store.task(task_id)

    def assign(self, goal_id, instructions, priority):
        task_id = uuid.uuid4().hex
        self.store.execute('''INSERT INTO tasks
            (id,goal_id,instructions,priority,adapter,status) VALUES(?,?,?,?,?,?)''',
            (task_id, goal_id, instructions, priority, self.worker.name, 'pending'))
        return task_id

    async def start(self, task_id, workspace):
        task = self.status(task_id)
        if task['status'] != 'pending':
            raise ValueError('Use send for an existing assignment')
        return await self.send(task_id, task['instructions'], workspace)

    async def send(self, task_id, instructions, workspace):
        """Send another turn, resuming the saved session if available."""
        task = self.status(task_id)
        if task['adapter'] != self.worker.name:
            raise ValueError('Assignment belongs to a different adapter')
        with self.store.db:
            attempt = self.store.db.execute('''INSERT INTO attempts
                (task_id,instructions,status) VALUES(?,?,'running')''',
                (task_id, instructions)).lastrowid
            self.store.db.execute("UPDATE tasks SET status='running',error=NULL WHERE id=?", (task_id,))
        def save_session(session_id):
            self.store.execute('UPDATE tasks SET session_id=? WHERE id=?', (session_id, task_id))
        try:
            handoff = await self.worker.run(instructions, workspace, task['session_id'], save_session)
            with self.store.db:
                self.store.db.execute('UPDATE tasks SET status=?,result=? WHERE id=?',
                                      (handoff.status, handoff.model_dump_json(), task_id))
                self.store.db.execute('UPDATE attempts SET status=?,result=? WHERE id=?',
                                      (handoff.status, handoff.model_dump_json(), attempt))
            return handoff
        except (Exception, asyncio.CancelledError) as exc:
            with self.store.db:
                error = str(exc) or type(exc).__name__
                self.store.db.execute("UPDATE tasks SET status='failed',error=? WHERE id=?", (error, task_id))
                self.store.db.execute("UPDATE attempts SET status='failed',error=? WHERE id=?", (error, attempt))
            raise
