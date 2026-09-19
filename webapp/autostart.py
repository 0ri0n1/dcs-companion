"""Per-user DCS watcher. Does not launch DCS, send inputs, or change DCS files."""
import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys
import time

if __package__:
    from . import launcher
else:
    import launcher


def launch_dashboard():
    python=launcher.APP/'.venv'/'Scripts'/'python.exe'
    args=[str(python),str(launcher.APP/'launcher.py'),'start','--no-open','--automatic']
    result=subprocess.run(args,cwd=launcher.ROOT,capture_output=True,text=True,timeout=45,
                          creationflags=launcher.HIDDEN)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or 'Dashboard startup failed').strip()[-600:])
    return launcher.saved_url(launcher.read_json(launcher.PIDFILE))


def components_healthy(url):
    if launcher.dashboard_health(url) is None: return False
    try:
        # The existing collector writes continually even when no aircraft exports.
        return time.time()-(launcher.ROOT/'state'/'state.json').stat().st_mtime<4
    except OSError:
        return False


class DCSWatcher:
    """Injected probes/clock/launcher allow complete simulation without DCS."""
    def __init__(self,runtime=None,*,clock=time.time,sessions=launcher.dcs_sessions,
                 health=components_healthy,start=launch_dashboard,open_browser=launcher.open_dashboard):
        self.runtime=Path(runtime or launcher.RUNTIME)
        self.runtime.mkdir(parents=True,exist_ok=True)
        self.clock,self.sessions,self.health,self.start,self.open_browser=clock,sessions,health,start,open_browser
        self.started=clock()
        self.current=None
        self.retry_at=0
        self.failures=0
        self.next_health_at=0
        self.needs_start_check=True
        self.url=launcher.saved_url(launcher.read_json(self.runtime/'launcher-processes.json'))
        self.last_status=None

    def publish(self,state,error=None):
        status={'schema':'dcs-companion/autostart/1','enabled':True,'watcher_pid':os.getpid(),
            'updated_epoch':self.clock(),'state':state,'dcs_session':self.current,
            'dashboard_url':self.url,'last_error':error,'next_retry_epoch':self.retry_at or None,
            'opened_for_session':launcher.read_json(self.runtime/'autostart-session.json').get('opened',False),
            'policy':'Automatic starts are dry-run and loopback. Dashboard stays available after DCS exits.'}
        # Heartbeat at most every 10 seconds while idle; transitions write immediately.
        if self.last_status is None or any(status[k]!=self.last_status[k] for k in ('state','dcs_session','last_error','next_retry_epoch')) or status['updated_epoch']-self.last_status['updated_epoch']>=10:
            launcher.atomic_json(self.runtime/'autostart-status.json',status)
            self.last_status=status
        return status

    def tick(self):
        stopped=launcher.read_json(self.runtime/'autostart-stop.json').get('requested_epoch',0)
        if isinstance(stopped,(int,float)) and stopped>=self.started:
            status=self.publish('Disabled')
            status['enabled']=False
            launcher.atomic_json(self.runtime/'autostart-status.json',status)
            return False
        sessions=self.sessions()
        if len(sessions)!=1:
            was_active=self.current is not None
            self.current=None
            self.failures=0
            self.retry_at=0
            self.next_health_at=0
            if len(sessions)>1:
                self.publish('Waiting for DCS','Multiple matching DCS processes; automatic start is paused.')
            else:
                self.publish('Debrief' if was_active or launcher.read_json(self.runtime/'autostart-session.json') else 'Waiting for DCS')
            return True
        session=sessions[0]
        key=launcher.session_key(session)
        if launcher.session_key(self.current)!=key:
            self.current=session
            self.failures=0
            self.retry_at=0
            self.next_health_at=0
            self.needs_start_check=True
            ledger=launcher.read_json(self.runtime/'autostart-session.json')
            if ledger.get('session_key')!=key:
                launcher.atomic_json(self.runtime/'autostart-session.json',{'session_key':key,'opened':False})
        if launcher.manually_paused(session,self.runtime):
            self.retry_at=0
            self.publish('Paused by manual stop')
            return True
        now=self.clock()
        if now<self.retry_at:
            self.publish('Retrying',self.last_status.get('last_error') if self.last_status else None)
            return True
        if now<self.next_health_at:
            self.publish('Watching DCS')
            return True
        try:
            if self.needs_start_check or not self.health(self.url):
                self.publish('Starting dashboard')
                self.url=self.start()
                if not self.health(self.url): raise RuntimeError('Dashboard or collector is still unavailable after repair.')
                self.needs_start_check=False
            self.failures=0
            self.retry_at=0
            self.next_health_at=self.clock()+5
            ledger=launcher.read_json(self.runtime/'autostart-session.json')
            if not ledger.get('opened'):
                # Claim browser launch before invoking it: a crash/restart never repeats it.
                launcher.atomic_json(self.runtime/'autostart-session.json',{'session_key':key,'opened':True})
                self.open_browser(self.url,session)
            self.publish('Watching DCS')
        except (OSError,RuntimeError,ValueError,subprocess.SubprocessError) as exc:
            self.failures+=1
            self.retry_at=self.clock()+min(120,5*(2**min(self.failures-1,5)))
            logging.warning('Dashboard recovery deferred: %s',exc)
            self.publish('Retrying',str(exc)[-600:])
        return True


def watch():
    runtime=launcher.RUNTIME
    runtime.mkdir(parents=True,exist_ok=True)
    with launcher.process_lock(runtime/'autostart.lock'):
        handler=RotatingFileHandler(runtime/'autostart.log',maxBytes=256_000,backupCount=2,encoding='utf-8')
        logging.basicConfig(level=logging.INFO,handlers=[handler],format='%(asctime)s %(levelname)s %(message)s')
        watcher=DCSWatcher()
        logging.info('DCS companion watcher started; dry-run loopback policy.')
        while watcher.tick(): time.sleep(2)
        logging.info('DCS companion watcher disabled.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=['watch','stop','status'])
    args=parser.parse_args()
    if args.operation=='stop':
        launcher.atomic_json(launcher.RUNTIME/'autostart-stop.json',{'requested_epoch':time.time()})
    elif args.operation=='status':
        print(json.dumps(launcher.read_json(launcher.RUNTIME/'autostart-status.json'),indent=2))
    else:
        try: watch()
        except RuntimeError:
            # A second startup shortcut invocation is harmless and exits quietly.
            return 0
    return 0


if __name__=='__main__': raise SystemExit(main())
