"""Terminal interface; input and worker execution do not share a blocking loop."""
import asyncio
import json
from .supervisor import Supervisor


async def interactive(store, goal_id):
    supervisor = await Supervisor(store, goal_id).open()
    driver = None
    print('Manager session ready. /status /run [steps] /policy TEXT /context /accept ID /send TASK TEXT /pause TASK /cancel TASK /reconcile TASK /events /quit')
    try:
        while True:
            try:
                line = (await asyncio.to_thread(input, 'manager> ')).strip()
            except EOFError:
                break
            if not line:
                continue
            try:
                command, _, rest = line.partition(' ')
                if command == '/quit':
                    break
                if command == '/status':
                    result = supervisor.status()
                elif command == '/run':
                    if driver and not driver.done():
                        result = {'running':True}
                    else:
                        driver = asyncio.create_task(supervisor.drive(int(rest or 5)))
                        result = {'started':True}
                elif command == '/policy':
                    proposal = supervisor.control.propose(goal_id,'policy', {'instructions':rest})
                    result = {'proposal':proposal,'next':f'/accept {proposal}'}
                elif command == '/context':
                    result = supervisor.control.research_context(goal_id)
                elif command == '/requirements':
                    proposal = supervisor.control.propose(goal_id,'requirements',json.loads(rest))
                    result = {'proposal':proposal,'next':f'/accept {proposal}'}
                elif command == '/accept':
                    result = await supervisor.accept(rest)
                elif command in ('/pause','/cancel'):
                    result = await supervisor.stop(rest, command=='/pause')
                elif command == '/send':
                    task_id, _, text = rest.partition(' ')
                    mid = supervisor.control.message(goal_id,'steering',{'instruction':text},task_id,'manager_to_worker')
                    result = await supervisor.deliver(mid)
                elif command == '/answer':
                    question, _, payload = rest.partition(' ')
                    result = await supervisor.answer(question,json.loads(payload))
                elif command == '/resolve':
                    mid, delivered, evidence = rest.split(' ',2)
                    result = supervisor.resolve_delivery(mid,delivered=='delivered',evidence)
                elif command == '/reconcile':
                    result = await supervisor.reconcile(rest)
                elif command == '/events':
                    result = [dict(r) for r in store.db.execute('SELECT * FROM activity WHERE goal_id=? ORDER BY id DESC LIMIT 20',(goal_id,))]
                else:
                    result = await supervisor.chat(line)
                print(json.dumps(result, indent=2))
            except (ValueError,RuntimeError,KeyError) as exc:
                print(f'Action failed: {exc}')
    finally:
        if driver:
            driver.cancel()
            await asyncio.gather(driver,return_exceptions=True)
        await supervisor.close()
