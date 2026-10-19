"""Supervise a saved campaign owner without resetting its limits or decisions.

Use a process supervisor (for example systemd) around this watcher to also recover
from a machine reboot or a watcher crash. A deliberate operator stop is terminal.
"""
import argparse
import fcntl
import json
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from omnitrix.campaign import Campaign
from omnitrix.control import Control
from omnitrix.store import Store


def watch(database, goal, command=None, restart_delay=2):
    database = Path(database).resolve()
    with open(str(database)+'.watch.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another campaign watcher owns this database') from None
        store = Store(database)
        campaign = Campaign(store,goal)
        stopping = False
        child = None
        def stop(signum, frame):
            nonlocal stopping
            stopping = True
            if child is not None and child.poll() is None:
                child.terminate()
        original = {sig:signal.signal(sig,stop) for sig in (signal.SIGINT,signal.SIGTERM)}
        crashes = 0
        try:
            while not stopping:
                state = campaign.status()
                if not state or state['state'] not in ('ready','running','interrupted'):
                    return state
                if state['deadline'] <= time.time():
                    campaign.stop('deadline','Saved campaign deadline reached before owner restart')
                    return campaign.status()
                child = subprocess.Popen(command or [sys.executable,'-m','omnitrix','--db',str(database),'campaign',goal],cwd=ROOT)
                Control(store).event(goal,'campaign_owner_started',{'pid':child.pid,'restart':crashes})
                while child.poll() is None and not stopping:
                    try:
                        child.wait(timeout=.2)
                    except subprocess.TimeoutExpired:
                        pass
                code = child.poll()
                if stopping:
                    break
                state = campaign.status()
                if state['state'] not in ('ready','running','interrupted'):
                    return state
                crashes += 1
                delay = min(60,restart_delay * 2**min(crashes-1,5))
                Control(store).event(goal,'campaign_owner_restarted',{'exit_code':code,'delay_seconds':delay,
                    'note':'Resume saved state; deadlines, cost reservations and attempts are not reset.'})
                until = time.monotonic()+delay
                while not stopping and time.monotonic()<until:
                    time.sleep(min(.2,until-time.monotonic()))
            campaign.stop('paused','Operator stopped the campaign watcher')
            return campaign.status()
        finally:
            for sig,handler in original.items():signal.signal(sig,handler)
            if child is not None and child.poll() is None:
                child.terminate()
                try:child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill();child.wait()
            store.db.close()


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--goal',required=True)
    args=parser.parse_args()
    print(json.dumps(watch(args.db,args.goal)),flush=True)
