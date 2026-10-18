"""Codex App Server JSON-RPC client. Steering uses turn/steer, not chat spoofing."""
import asyncio
import json
import os
import shutil
from pathlib import Path
import uuid
from .codex_config import cli_config_args, codex_environment


class CodexRuntime:
    def __init__(self, on_event=None, executable=None):
        self.executable = executable or os.getenv('OMNITRIX_CODEX_PATH') or shutil.which('codex') or str(Path(__file__).resolve().parents[1]/'node_modules/.bin/codex')
        self.on_event = on_event
        self.proc = None
        self.connection_id = uuid.uuid4().hex
        self.sequence = 0
        self.pending = {}
        self.turns = {}
        self.finished = {}
        self.usage = {}
        self.session_ids = set()
        self.questions = set()
        self.items = {}
        self.reader = self.stderr = None

    async def connect(self):
        if self.proc is not None:
            return
        self.proc = await asyncio.create_subprocess_exec(self.executable, 'app-server', '--stdio', *cli_config_args(),
            env=codex_environment(),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=4*1024*1024)
        self.reader = asyncio.create_task(self._read())
        async def drain():
            async for _ in self.proc.stderr:
                pass
        self.stderr = asyncio.create_task(drain())
        await self.call('initialize', {'clientInfo': {'name': 'omnitrix', 'version': '0.2.0'},
                                       'capabilities': {'experimentalApi': True}})
        await self.write({'method': 'initialized'})

    async def write(self, data):
        self.proc.stdin.write((json.dumps(data)+'\n').encode())
        await self.proc.stdin.drain()

    async def call(self, method, params, timeout=30):
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.write({'id': request_id, 'method': method, 'params': params})
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(request_id, None)

    async def _read(self):
        try:
            async for line in self.proc.stdout:
                data = json.loads(line)
                if 'method' not in data and 'id' in data:
                    future = self.pending.get(data['id'])
                    if future and not future.done():
                        if 'error' in data:
                            future.set_exception(RuntimeError(str(data['error'])))
                        else:
                            future.set_result(data.get('result'))
                    continue
                params = data.get('params', {})
                if data.get('method') == 'thread/tokenUsage/updated':
                    self.usage[params['threadId']] = params.get('tokenUsage',{}).get('total',{})
                if data.get('method') == 'item/completed' and params.get('turnId') and params.get('item',{}).get('type')=='agentMessage':
                    self.items.setdefault((params['threadId'],params['turnId']),[]).append(params['item'])
                if data.get('method') == 'turn/completed':
                    key = (params['threadId'], params['turn']['id'])
                    final_items=[i for i in params['turn'].get('items',[]) if i.get('type')=='agentMessage']
                    params['turn']['items']=final_items or self.items.pop(key,[])
                    self.items.pop(key,None)
                    self.finished[key] = params['turn']
                    if len(self.finished)>64:
                        self.finished.pop(next(iter(self.finished)))
                    future = self.turns.get(key)
                    if future and not future.done():
                        future.set_result(params['turn'])
                if 'id' in data and 'method' in data:
                    # Approval requests are never auto-granted. Questions remain pending for a real answer.
                    if data['method'] == 'item/tool/requestUserInput':
                        self.questions.add(data['id'])
                    else:
                        await self.write({'id': data['id'], 'error': {'code': -32000,
                            'message': 'Unattended approval denied; ask manager for an explicit decision'}})
                if self.on_event:
                    await self.on_event(data)
        except Exception as exc:
            self.failure = exc
        finally:
            for future in list(self.pending.values()) + list(self.turns.values()):
                if not future.done():
                    future.set_exception(RuntimeError('Codex connection lost; outcome uncertain, reconcile thread before retry'))

    async def start(self, workspace, instructions, session_id=None, write=False, schema=None, on_thread=None):
        await self.connect()
        params = {**({'model':os.environ['OMNITRIX_CODEX_MODEL']} if os.getenv('OMNITRIX_CODEX_MODEL') else {}), 'cwd': workspace, 'sandbox': 'workspace-write' if write else 'read-only',
                  'approvalPolicy': 'never', 'config': {'sandbox_workspace_write.network_access': False},
                  'developerInstructions': 'Bounded engineering task. No deployments or destructive commands. Ask for decisions when blocked.'}
        if session_id:
            thread = await self.call('thread/resume', dict(params, threadId=session_id))
        else:
            thread = await self.call('thread/start', params)
        thread_id = thread['thread']['id']
        self.session_ids.add(thread_id)
        if on_thread:
            on_thread(thread_id)
        turn = await self.call('turn/start', {'threadId': thread_id,
            'input': [{'type': 'text', 'text': instructions}], **({'outputSchema': schema} if schema else {})})
        return thread_id, turn['turn']['id']

    async def wait(self, thread_id, turn_id, timeout=300):
        key = (thread_id, turn_id)
        if key in self.finished:
            return self.finished[key]
        future = self.turns.get(key)
        if future is None:
            future=asyncio.get_running_loop().create_future()
            future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
            self.turns[key]=future
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        finally:
            if future.done():
                self.turns.pop(key,None)

    async def steer(self, thread_id, turn_id, text, message_id):
        return await self.call('turn/steer', {'threadId': thread_id, 'expectedTurnId': turn_id,
            'clientUserMessageId': message_id, 'input': [{'type': 'text', 'text': text}]})

    async def interrupt(self, thread_id, turn_id):
        await self.call('turn/interrupt', {'threadId': thread_id, 'turnId': turn_id})
        # An accepted interrupt request is not yet proof the turn stopped.
        return await self.wait(thread_id,turn_id,timeout=5)

    async def answer(self, request_id, answers):
        if self.proc is None or request_id not in self.questions:
            raise RuntimeError('Question belongs to a previous runtime connection; reconcile and resume the thread instead of replaying an old RPC response')
        await self.write({'id': request_id, 'result': {'answers': {
            key: {'answers': values} for key, values in answers.items()}}})
        self.questions.discard(request_id)
        # JSON-RPC response has no acknowledgment; callers must preserve uncertainty.

    async def reconcile(self, thread_id):
        await self.connect()
        return await self.call('thread/read', {'threadId': thread_id, 'includeTurns': True})

    async def close(self):
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        if self.reader:
            await self.reader
        if self.stderr:
            await self.stderr
