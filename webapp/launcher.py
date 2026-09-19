"""Start/repair the existing dashboard and collector without duplicate listeners."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
import psutil

APP = Path(__file__).resolve().parent
ROOT = APP.parent
# Batch files and the hidden watcher load this as a script/top-level module.
# Keep package imports independent of cwd and inherited PYTHONPATH.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
RUNTIME = APP / 'runtime'
PIDFILE = RUNTIME / 'launcher-processes.json'
HIDDEN = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def read_json(path):
    try:
        value=json.loads(Path(path).read_text(encoding='utf-8-sig'))
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError):
        return {}


def atomic_json(path,value):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+secrets.token_hex(4)+'.tmp')
    try:
        temporary.write_text(json.dumps(value,indent=2),encoding='utf-8')
        os.replace(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def process_lock(path):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+b') as handle:
        handle.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('Another dashboard operation is already running.') from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name=='nt': msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
            else: fcntl.flock(handle,fcntl.LOCK_UN)


def normalized_path(path):
    return os.path.normcase(os.path.abspath(str(path)))


def dcs_sessions(processes=None):
    """Recognize executables inside a validated DCS installation on any drive."""
    from webapp.backend.discovery import install_from_executable
    sessions=[]
    processes=processes if processes is not None else psutil.process_iter(['pid','name','exe','create_time'])
    for process in processes:
        try:
            info=process.info
            if info.get('name','').lower()!='dcs.exe': continue
            if not info.get('exe') or install_from_executable(info['exe']) is None: continue
            sessions.append({'pid':info['pid'],'created':info['create_time'],'exe':info['exe']})
        except (psutil.Error,TypeError,KeyError): continue
    return sorted(sessions,key=lambda s:(s['created'],s['pid']))


def session_key(session):
    return f"{session['pid']}:{session['created']}" if session else None


def manually_paused(session,runtime=None):
    value=read_json(Path(runtime or RUNTIME)/'manual-stop.json').get('requested_epoch')
    return bool(session and isinstance(value,(int,float)) and value>=session['created'])


def expected_process(proc,role):
    """Validate interpreter, exact module/script token and working directory."""
    if role not in ('dashboard','collector'): return False
    try:
        trusted={normalized_path(sys.executable),normalized_path(getattr(sys,'_base_executable',sys.executable)),
                 normalized_path(APP/'.venv'/'Scripts'/'python.exe'),normalized_path(APP/'.venv'/'Scripts'/'pythonw.exe')}
        base=Path(getattr(sys,'_base_executable',sys.executable))
        trusted.update(normalized_path(base.with_name(name)) for name in ('python.exe','pythonw.exe'))
        if normalized_path(proc.exe()) not in trusted: return False
        args=proc.cmdline()
        if role=='collector':
            return any(normalized_path(a)==normalized_path(ROOT/'collector.py') for a in args[1:] if not a.startswith('-'))
        return any(args[i:i+2]==['-m','webapp.backend'] for i in range(len(args)-1)) and normalized_path(proc.cwd())==normalized_path(ROOT)
    except (psutil.Error,OSError,TypeError): return False


def record(proc,role):
    # Windows virtual-environment executables may redirect to an interpreter child.
    worker=psutil.Process(proc.pid)
    for _ in range(10):
        children=[p for p in worker.children(recursive=True) if expected_process(p,role)]
        if children:
            worker=children[-1]
            break
        time.sleep(.05)
    if not expected_process(worker,role): raise RuntimeError('Started process identity could not be verified.')
    return {'pid':worker.pid,'created':worker.create_time(),'exe':worker.exe(),'role':role,'launcher_pid':proc.pid}


def owned_process(item):
    try:
        if not isinstance(item,dict): return None
        proc=psutil.Process(item['pid'])
        if abs(proc.create_time()-item['created'])>.1: return None
        if item.get('exe') and normalized_path(proc.exe())!=normalized_path(item['exe']): return None
        return proc if expected_process(proc,item.get('role')) else None
    except (psutil.Error,KeyError,TypeError): return None


def port_owner(kind,port,role):
    try:
        ids={c.pid for c in psutil.net_connections(kind=kind) if c.laddr and c.laddr.port==port and
             (kind=='udp' or c.status==psutil.CONN_LISTEN)}
        if not ids: return None
        if None in ids or len(ids)!=1: raise RuntimeError(f'Port {port} has ambiguous owners; no duplicate process will be started.')
        proc=psutil.Process(ids.pop())
        if not expected_process(proc,role): raise RuntimeError(f'Port {port} belongs to another program; it was left untouched.')
        return proc
    except psutil.Error as exc: raise RuntimeError(f'Cannot verify port {port} ownership; startup is paused.') from exc


def dashboard_health(url):
    try:
        with urllib.request.urlopen(url+'/api/bootstrap',timeout=1) as response:
            data=json.loads(response.read(4096))
            return data if response.status==200 and isinstance(data,dict) and 'dry_run' in data else None
    except (OSError,ValueError): return None


def saved_url(data):
    from urllib.parse import urlsplit
    import ipaddress
    value=data.get('url')
    try:
        parsed=urlsplit(value) if isinstance(value,str) else None
        if parsed and parsed.scheme=='http' and parsed.port==18787 and not parsed.username and not parsed.password:
            if parsed.hostname=='localhost' or ipaddress.ip_address(parsed.hostname).is_private:
                return value.rstrip('/')
    except (ValueError,TypeError): pass
    return 'http://127.0.0.1:18787'


def dashboard_url(process):
    """Recover an existing LAN instance's address if its tracking file was lost."""
    addresses=[c.laddr.ip for c in psutil.net_connections(kind='tcp') if c.pid==process.pid and
               c.laddr and c.laddr.port==18787 and c.status==psutil.CONN_LISTEN]
    if not addresses: return None
    address='127.0.0.1' if '127.0.0.1' in addresses else addresses[0]
    if address in ('0.0.0.0','::'): raise RuntimeError('Existing dashboard listens on an unexpected wildcard interface.')
    return f'http://[{address}]:18787' if ':' in address else f'http://{address}:18787'


def process_records(data):
    records=data.get('processes',[])
    return [item for item in records if isinstance(item,dict)] if isinstance(records,list) else []


def open_dashboard(url,session=None,force=False):
    """Record intent first so recovery cannot open a second window for this session."""
    ledger=RUNTIME/'browser-session.json'
    key=session_key(session)
    if not force and key and read_json(ledger).get('session_key')==key: return False
    if key: atomic_json(ledger,{'session_key':key,'opened':True,'url':url,'requested_epoch':time.time()})
    webbrowser.open(url,new=0)
    return True


def stop():
    RUNTIME.mkdir(parents=True,exist_ok=True)
    atomic_json(RUNTIME/'manual-stop.json',{'requested_epoch':time.time(),'reason':'Manual Stop Dashboard'})
    data=read_json(PIDFILE)
    if not data:
        print('No owned dashboard is recorded. Automatic recovery is paused until the next DCS session.')
        return
    pending=[]
    for item in sorted(process_records(data),key=lambda x:0 if x.get('role')=='dashboard' else 1):
        proc=owned_process(item)
        if not proc: continue
        if item['role']=='dashboard': atomic_json(RUNTIME/'stop-request.json',{'pid':proc.pid,'created':item['created']})
        elif pending:
            pending.append(item)
            continue
        else: proc.terminate()
        try: proc.wait(12)
        except psutil.TimeoutExpired:
            pending.append(item)
            print(f"{item['role']} is still stopping; it was not forcibly interrupted.")
    if pending:
        data['processes']=pending
        atomic_json(PIDFILE,data)
    else:
        PIDFILE.unlink(missing_ok=True)
        print('Dashboard stopped. DCS and pre-existing Copilot processes were left running.')


def clean_environment(args):
    env=os.environ.copy()
    for key in list(env):
        if key.startswith('DCS_DASH_'): env.pop(key)
    host='127.0.0.1'
    if getattr(args,'tablet',False) and not getattr(args,'automatic',False):
        address=getattr(args,'address',None)
        if not address:
            addresses=[a.address for rows in psutil.net_if_addrs().values() for a in rows if a.family==socket.AF_INET and not a.address.startswith(('127.','169.254.'))]
            print('Available computer addresses: '+', '.join(addresses))
            address=input('Enter this computer’s private local IPv4 address: ').strip()
        import ipaddress
        parsed=ipaddress.ip_address(address)
        own=[a.address for rows in psutil.net_if_addrs().values() for a in rows if a.family==socket.AF_INET]
        if parsed.version!=4 or not parsed.is_private or parsed.is_loopback or parsed.is_unspecified or address not in own:
            raise RuntimeError('Choose this computer’s private local IPv4 address.')
        host=address
        env['DCS_DASH_PAIRING_TOKEN']=secrets.token_urlsafe(24)
        print('TABLET ACCESS ENABLED. Pairing code: '+env['DCS_DASH_PAIRING_TOKEN'])
        print('Use a trusted local network; local HTTP is not encrypted.')
    arm=bool(getattr(args,'arm',False) and not getattr(args,'automatic',False))
    origins = ['http://127.0.0.1:18787', 'http://localhost:18787']
    if host != '127.0.0.1': origins.append(f'http://{host}:18787')
    env.update(DCS_DASH_HOST=host,DCS_DASH_PORT='18787',DCS_DASH_ARM='YES' if arm else 'NO',
               DCS_DASH_ORIGINS=','.join(origins))
    if getattr(args,'mission',None) and not getattr(args,'automatic',False): env['DCS_DASH_MISSION']=str(Path(args.mission).resolve())
    for argument, variable in (('install_root', 'DCS_DASH_INSTALL_ROOT'), ('saved_games', 'DCS_DASH_SAVED_GAMES')):
        if getattr(args, argument, None):
            env[variable] = str(Path(getattr(args, argument)).resolve())
    return env,f'http://{host}:18787'


def _start_child(role,env=None):
    args=[sys.executable,str(ROOT/'collector.py')] if role=='collector' else [sys.executable,'-m','webapp.backend']
    checkpoint=RUNTIME/'collector-restart-checkpoint.json'
    if role=='collector' and checkpoint.is_file():
        args.extend(['--resume-checkpoint',str(checkpoint)])
    logfile=RUNTIME/(role+'-console.log')
    if logfile.exists() and logfile.stat().st_size>2_000_000:
        try: os.replace(logfile,logfile.with_suffix('.previous.log'))
        except OSError: pass
    with logfile.open('ab') as output:
        return subprocess.Popen(args,cwd=ROOT,env=env,stdout=output,stderr=output,creationflags=HIDDEN)


def start(args):
    RUNTIME.mkdir(parents=True,exist_ok=True)
    sessions=dcs_sessions()
    session=sessions[0] if len(sessions)==1 else None
    automatic=getattr(args,'automatic',False)
    if automatic and (not session or manually_paused(session)):
        raise RuntimeError('Automatic start is paused: no unique DCS session, or the pilot selected Stop.')
    if not automatic: (RUNTIME/'manual-stop.json').unlink(missing_ok=True)
    old=read_json(PIDFILE)
    processes=[item for item in process_records(old) if owned_process(item)]
    collector=port_owner('udp',17781,'collector')
    dashboard=port_owner('tcp',18787,'dashboard')
    # Verify both ports BEFORE starting either child.
    if not collector:
        if any(item.get('role')=='collector' for item in processes):
            raise RuntimeError('Existing collector is starting or unresponsive; no duplicate was launched.')
        child=_start_child('collector')
        started_collector=record(child,'collector')
        processes.append(started_collector)
        atomic_json(PIDFILE,{'url':saved_url(old),'processes':processes})
        try:
            for _ in range(20):
                if child.poll() is not None: raise RuntimeError('Collector stopped during startup; inspect runtime/collector-console.log.')
                collector=port_owner('udp',17781,'collector')
                if collector: break
                time.sleep(.1)
            if not collector: raise RuntimeError('Collector has not opened its expected port; startup is paused.')
            if collector.pid!=started_collector['pid']:
                raise RuntimeError('Another collector won startup; our duplicate will be stopped.')
        except (RuntimeError,psutil.Error):
            owned=owned_process(started_collector)
            if owned:
                owned.terminate()
                try: owned.wait(3)
                except psutil.TimeoutExpired: pass
            processes=[p for p in processes if p is not started_collector]
            atomic_json(PIDFILE,{'url':saved_url(old),'processes':processes})
            raise
    processes=[item for item in processes if owned_process(item)]
    url=dashboard_url(dashboard) if dashboard else 'http://127.0.0.1:18787'
    url=url or 'http://127.0.0.1:18787'
    if not dashboard:
        if any(item['role']=='dashboard' for item in processes): raise RuntimeError('Existing dashboard is starting or unresponsive; no second copy was launched.')
        env,url=clean_environment(args)
        child=_start_child('dashboard',env)
        processes.append(record(child,'dashboard'))
        atomic_json(PIDFILE,{'url':url,'processes':processes,'automatic':automatic,'dry_run':env['DCS_DASH_ARM']!='YES'})
    else:
        atomic_json(PIDFILE,{'url':url,'processes':processes,'automatic':old.get('automatic',False),'dry_run':old.get('dry_run',True)})
    for _ in range(30):
        health=dashboard_health(url)
        if health is not None:
            if not getattr(args,'no_open',False):
                # The host uses loopback for automatic pairing and QR creation;
                # the separately printed LAN URL is for phones/tablets.
                browser_url = 'http://127.0.0.1:18787' if health.get('lan_enabled') else url
                open_dashboard(browser_url,session,force=not automatic)
            print('Dashboard ready: '+url+(' (dry run)' if health['dry_run'] else ' (existing live controls)'))
            return {'url':url,'health':health,'processes':processes}
        time.sleep(.3)
    raise RuntimeError('Dashboard is unavailable; inspect runtime/dashboard-console.log. No duplicate was launched.')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('operation',choices=['start','stop'])
    parser.add_argument('--tablet',action='store_true')
    parser.add_argument('--address')
    parser.add_argument('--arm',action='store_true')
    parser.add_argument('--mission')
    parser.add_argument('--install-root', help='Explicit DCS installation for a nonstandard location.')
    parser.add_argument('--saved-games', help='Explicit DCS Saved Games profile directory.')
    parser.add_argument('--no-open',action='store_true')
    parser.add_argument('--automatic',action='store_true',help='Watcher only: forced dry-run loopback and manual-Stop guard.')
    args=parser.parse_args()
    try:
        with process_lock(RUNTIME/'launcher.lock'): stop() if args.operation=='stop' else start(args)
    except (RuntimeError,OSError,ValueError,psutil.Error) as exc:
        print(str(exc),file=sys.stderr)
        return 1
    return 0


if __name__=='__main__': raise SystemExit(main())
