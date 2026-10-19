import argparse
import asyncio
import json
from pathlib import Path
from .manager import AgentBrain
from .store import Store
from .chat import interactive
from .knowledge import Knowledge
from .implementation import ImplementationSpec, Implementations
from .supervisor import Supervisor


def main():
    parser = argparse.ArgumentParser(description='Persistent engineering manager')
    parser.add_argument('--db', default='.omnitrix/state.db')
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create')
    create.add_argument('objective')
    create.add_argument('--criterion', action='append', required=True)
    create.add_argument('--require-implementation',action='store_true',help='Require independently verified code and a current goal audit before completion')
    create.add_argument('--workspace', type=Path, default=Path.cwd())
    status = commands.add_parser('status')
    status.add_argument('goal_id')
    resume = commands.add_parser('resume')
    resume.add_argument('goal_id')
    resume.add_argument('--steps', type=int, default=5)
    resume.add_argument('--message', help='Additional conversational guidance for the manager')
    resume.add_argument('--model')
    chat = commands.add_parser('chat')
    chat.add_argument('goal_id')
    run = commands.add_parser('run')
    run.add_argument('goal_id')
    run.add_argument('--steps',type=int,default=20)
    run.add_argument('--hours',type=float,default=1)
    campaign = commands.add_parser('campaign', help='Start/resume a durable, dollar-capped read-only investigation')
    campaign.add_argument('goal_id')
    campaign.add_argument('--days', type=float)
    campaign.add_argument('--decisions', type=int)
    campaign.add_argument('--research-runs', type=int)
    campaign.add_argument('--dollars', type=float)
    campaign.add_argument('--report', type=Path)
    campaign.add_argument('--prepare-only', action='store_true')
    campaign.add_argument('--continue-paused', action='store_true')
    campaign.add_argument('--pause', action='store_true')
    campaign.add_argument('--strategy',choices=('manager','investigator'))
    campaign.add_argument('--evidence', action='append', default=[], metavar='PATH', help='Index an exported UTF-8 log/measurement inside the goal workspace; repeat for multiple files')
    search = commands.add_parser('knowledge')
    search.add_argument('goal_id')
    search.add_argument('query',nargs='?',default='')
    export = commands.add_parser('export')
    export.add_argument('goal_id')
    report = commands.add_parser('report', help='Write a standalone HTML evidence and decision log')
    report.add_argument('goal_id')
    report.add_argument('--output', type=Path, required=True)
    authorize = commands.add_parser('authorize')
    authorize.add_argument('goal_id')
    authorize.add_argument('spec',type=Path)
    authorize.add_argument('--allow-code-modifications',action='store_true',required=True)
    send = commands.add_parser('send',help='Queue a command to an already-running manager without taking ownership')
    send.add_argument('goal_id')
    send.add_argument('text',nargs='?',default='')
    send.add_argument('--command',dest='command_type',default='chat',choices=['chat','status','policy','requirements','accept','pause','cancel','steer','continue','reconcile'])
    send.add_argument('--id',help='Stable idempotency key for this message')
    messages=commands.add_parser('messages')
    messages.add_argument('goal_id')
    dashboard = commands.add_parser('dashboard', help='Read-only manager dashboard')
    dashboard.add_argument('--host', default='127.0.0.1')
    dashboard.add_argument('--port', type=int, default=8765)
    dashboard.add_argument('--allow-control', action='store_true')
    dashboard.add_argument('--allowed-origin', action='append', default=[])
    args = parser.parse_args()
    if args.command == 'dashboard':
        from .dashboard import serve
        import os
        token = os.getenv('OMNITRIX_DASHBOARD_TOKEN') if args.allow_control else None
        if args.allow_control and not token:
            parser.exit(1, 'Set OMNITRIX_DASHBOARD_TOKEN securely before enabling control\n')
        try:
            serve(args.db, args.host, args.port, control_token=token, allowed_origins=args.allowed_origin)
        except ValueError as exc:
            parser.exit(1, f'{exc}\n')
        return
    store = Store(args.db)
    try:
        if args.command == 'create':
            if not args.workspace.is_dir():
                raise ValueError('Workspace must be an existing directory')
            goal_id=store.create_goal(args.objective,args.criterion,args.workspace)
            if args.require_implementation:
                from .control import Control
                Control(store).require_implementation(goal_id)
            print(goal_id)
        elif args.command == 'status':
            print(json.dumps(store.snapshot(args.goal_id), indent=2))
        elif args.command == 'send':
            from .control import Control
            control=Control(store);control.initialize(args.goal_id)
            print(control.message(args.goal_id,'control',{'command':args.command_type,'text':args.text},message_id=args.id))
        elif args.command == 'messages':
            print(json.dumps([dict(r) for r in store.db.execute('SELECT * FROM messages WHERE goal_id=? ORDER BY created DESC LIMIT 40',(args.goal_id,))],indent=2))
        elif args.command == 'knowledge':
            print(json.dumps(Knowledge(store).search(store.snapshot(args.goal_id)['goal']['workspace'],args.query),indent=2))
        elif args.command == 'export':
            print(json.dumps(Knowledge(store).export(args.goal_id),indent=2))
        elif args.command == 'report':
            from .reporting import goal_report, write_report
            write_report(goal_report(store, args.goal_id), args.output)
            print(str(args.output.resolve()))
        elif args.command == 'authorize':
            service = Implementations(store)
            spec = ImplementationSpec.model_validate_json(args.spec.read_text())
            authorization = service.authorize(args.goal_id,spec,explicit=args.allow_code_modifications)
            print(json.dumps({'authorization':authorization,'implementation_id':service.create(authorization)}))
        elif args.command == 'chat':
            asyncio.run(interactive(store,args.goal_id))
        elif args.command == 'run':
            async def run_manager():
                supervisor = await Supervisor(store,args.goal_id).open()
                try:
                    return await supervisor.drive(args.steps,args.hours*3600,linger=True)
                finally:
                    await supervisor.close()
            print(json.dumps(asyncio.run(run_manager()),indent=2))
        elif args.command == 'campaign':
            from .campaign import Campaign
            service = Campaign(store, args.goal_id)
            if args.pause:
                if not service.status():
                    raise ValueError('No campaign exists for this goal')
                service.stop('paused', 'Operator paused the campaign')
                print(json.dumps(service.status(),indent=2))
            else:
                service.configure(args.days,args.decisions,args.research_runs,args.dollars,args.report,strategy=args.strategy)
                if args.evidence:
                    from .frontier import Frontier
                    frontier=Frontier(store,args.goal_id)
                    frontier.build()
                    frontier.register(args.evidence)
                if args.prepare_only:
                    from .frontier import Frontier
                    print(json.dumps({'campaign':service.status(),'frontier':Frontier(store,args.goal_id).build()},indent=2))
                else:
                    print(json.dumps(asyncio.run(service.drive(continue_paused=args.continue_paused)),indent=2))
        else:
            if not __import__('os').getenv('OPENAI_API_KEY'):
                raise ValueError('Set OPENAI_API_KEY securely for the manager; authenticate Codex separately')
            async def resume_manager():
                supervisor = await Supervisor(store,args.goal_id,brain=AgentBrain(args.model,store=store)).open()
                try:
                    if args.message:
                        store.execute('INSERT INTO decisions(goal_id,body) VALUES(?,?)',(args.goal_id,json.dumps({'user_message':args.message})))
                    return await supervisor.drive(args.steps)
                finally:
                    await supervisor.close()
            print(json.dumps(asyncio.run(resume_manager()),indent=2))
    except (ValueError, RuntimeError) as exc:
        parser.exit(1, f'{exc}\n')
    finally:
        store.db.close()


if __name__ == '__main__':
    main()
