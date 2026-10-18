"""Adapter boundary; the database owns status, adapters own execution sessions."""
import asyncio
import json
from pathlib import Path
from typing import Callable, Protocol
from .models import Handoff
from .codex_config import api_provider_config, codex_environment

ROOT = Path(__file__).resolve().parents[1]


class Worker(Protocol):
    name: str

    async def run(self, instructions: str, workspace: str, session_id: str | None,
                  on_session: Callable[[str], None]) -> Handoff:
        """Start when session_id is None, otherwise resume with additional instructions."""
        ...


class CodexWorker:
    name = 'codex'

    def __init__(self, timeout=300):
        self.timeout = timeout

    async def run(self, instructions, workspace, session_id, on_session):
        payload = json.dumps(dict(instructions=instructions, workspace=workspace,
                                  session_id=session_id, schema=Handoff.model_json_schema(), config=api_provider_config()))
        proc = await asyncio.create_subprocess_exec(
            'node', str(ROOT / 'bridge/codex.mjs'), env=codex_environment(), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        async def collect():
            proc.stdin.write(payload.encode())
            await proc.stdin.drain()
            proc.stdin.close()
            result = None
            async for line in proc.stdout:
                event = json.loads(line)
                if event['type'] == 'session':
                    on_session(event['id'])
                elif event['type'] == 'result':
                    result = Handoff.model_validate(event['handoff'])
                elif event['type'] == 'error':
                    raise RuntimeError(event['message'])
            await proc.wait()
            if proc.returncode or result is None:
                raise RuntimeError(f'Codex bridge failed (exit {proc.returncode}); no valid handoff')
            return result
        # Drain stderr concurrently so a verbose worker cannot deadlock the pipe.
        async def drain():
            async for _ in proc.stderr:
                pass
        stderr_task = asyncio.create_task(drain())
        try:
            return await asyncio.wait_for(collect(), self.timeout)
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            await stderr_task
