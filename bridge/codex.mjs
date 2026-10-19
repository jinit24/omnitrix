import { Codex } from '@openai/codex-sdk';
let input = '';
for await (const chunk of process.stdin) input += chunk;
const request = JSON.parse(input);
if (!request.role_instructions?.trim()) throw new Error('Worker role instructions are required');
const codex = new Codex({
  ...(process.env.OMNITRIX_CODEX_PATH ? { codexPathOverride: process.env.OMNITRIX_CODEX_PATH } : {}),
  config: { ...(request.config ?? {}), developer_instructions: request.role_instructions },
});
const options = {
  workingDirectory: request.workspace,
  sandboxMode: 'read-only', approvalPolicy: 'never',
  networkAccessEnabled: false,
};
const thread = request.session_id
  ? codex.resumeThread(request.session_id, options) : codex.startThread(options);
const emit = value => process.stdout.write(JSON.stringify(value) + '\n');
const controller = new AbortController();
process.on('SIGTERM', () => controller.abort());
try {
  const { events } = await thread.runStreamed(
    request.instructions,
    { outputSchema: request.schema, signal: controller.signal },
  );
  let handoff;
  for await (const event of events) {
    if (event.type === 'thread.started') emit({ type: 'session', id: event.thread_id });
    if (event.type === 'item.completed' && event.item.type === 'agent_message') {
      handoff = event.item.text;
    }
    if (event.type === 'turn.failed') throw new Error(event.error.message);
    if (event.type === 'error') throw new Error(event.message);
  }
  if (!handoff) throw new Error('Codex returned no handoff');
  emit({ type: 'result', handoff: JSON.parse(handoff) });
} catch (error) {
  emit({ type: 'error', message: error.message });
  process.exitCode = 1;
}
