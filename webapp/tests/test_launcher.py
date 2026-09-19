from types import SimpleNamespace
from unittest.mock import Mock
import sys
import subprocess
import pytest
from webapp import launcher as l


def test_standalone_launcher_imports_discovery_without_parent_pythonpath(tmp_path):
    code = "import sys; sys.path.insert(0, " + repr(str(l.APP)) + "); import launcher; assert launcher.dcs_sessions(processes=[]) == []"
    result = subprocess.run([sys.executable, "-I", "-c", code], cwd=tmp_path,
                            capture_output=True, text=True, timeout=15,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert result.returncode == 0, result.stderr


def args(**kw):
    return SimpleNamespace(automatic=False,no_open=True,tablet=False,address=None,arm=False,mission=None,**kw)


@pytest.fixture
def rig(tmp_path,monkeypatch):
    monkeypatch.setattr(l,'RUNTIME',tmp_path)
    monkeypatch.setattr(l,'PIDFILE',tmp_path/'launcher-processes.json')
    monkeypatch.setattr(l,'dcs_sessions',lambda:[])
    monkeypatch.setattr(l.time,'sleep',lambda _:None)
    monkeypatch.setattr(l,'dashboard_health',lambda url:{'dry_run':True})
    monkeypatch.setattr(l,'dashboard_url',lambda proc:'http://127.0.0.1:18787')
    monkeypatch.setattr(l,'owned_process',lambda item: SimpleNamespace(pid=item['pid']) if item.get('alive',True) else None)
    return tmp_path,monkeypatch


def test_existing_dashboard_repairs_collector_and_preserves_ownership(rig):
    path,m=rig
    l.atomic_json(l.PIDFILE,{'url':'http://127.0.0.1:18787','processes':[{'pid':10,'created':1,'role':'dashboard'}]})
    calls=[]
    collector=[None]
    def owner(kind,port,role): return SimpleNamespace(pid=10) if role=='dashboard' else collector[0]
    def start(role,env=None):
        calls.append(role); collector[0]=SimpleNamespace(pid=11)
        return SimpleNamespace(pid=11,poll=lambda:None)
    m.setattr(l,'port_owner',owner); m.setattr(l,'_start_child',start)
    m.setattr(l,'record',lambda proc,role:{'pid':proc.pid,'created':2,'role':role})
    result=l.start(args())
    assert calls==['collector']
    assert {p['pid'] for p in result['processes']}=={10,11}


def test_backend_recovery_retains_old_owned_collector(rig):
    path,m=rig
    l.atomic_json(l.PIDFILE,{'processes':[{'pid':11,'created':2,'role':'collector'}]})
    m.setattr(l,'port_owner',lambda kind,port,role:SimpleNamespace(pid=11) if role=='collector' else None)
    calls=[]
    m.setattr(l,'_start_child',lambda role,env=None:calls.append((role,env)) or SimpleNamespace(pid=12))
    m.setattr(l,'record',lambda proc,role:{'pid':proc.pid,'created':3,'role':role})
    result=l.start(args())
    assert {p['pid'] for p in result['processes']}=={11,12}
    assert calls[0][0]=='dashboard' and calls[0][1]['DCS_DASH_ARM']=='NO'


def test_tablet_mode_opens_host_loopback_for_qr_pairing(rig):
    _, m = rig
    m.setattr(l, 'port_owner', lambda *args: SimpleNamespace(pid=10))
    m.setattr(l, 'dashboard_url', lambda proc: 'http://192.168.1.20:18787')
    m.setattr(l, 'dashboard_health', lambda url: {'dry_run': True, 'lan_enabled': True})
    opened = Mock()
    m.setattr(l, 'open_dashboard', opened)
    options = args()
    options.no_open = False
    l.start(options)
    assert opened.call_args.args[0] == 'http://127.0.0.1:18787'


def test_new_collector_is_rolled_back_if_udp_ownership_races(rig):
    _,m=rig
    own=Mock(pid=11)
    calls=[None,SimpleNamespace(pid=10)]
    def owner(kind,port,role):
        if role=='dashboard': return SimpleNamespace(pid=12)
        return calls.pop(0)
    m.setattr(l,'port_owner',owner)
    m.setattr(l,'_start_child',lambda role,env=None:SimpleNamespace(pid=11,poll=lambda:None))
    m.setattr(l,'record',lambda proc,role:{'pid':11,'created':3,'role':role})
    m.setattr(l,'owned_process',lambda item:own)
    with pytest.raises(RuntimeError,match='won startup'): l.start(args())
    own.terminate.assert_called_once()
    assert l.read_json(l.PIDFILE)['processes']==[]


def test_stop_orders_dashboard_before_repaired_collector(rig):
    path,m=rig
    events=[]
    class Proc:
        def __init__(self,pid): self.pid=pid
        def wait(self,seconds): events.append(('wait',self.pid))
        def terminate(self): events.append(('terminate',self.pid))
    m.setattr(l,'owned_process',lambda item:Proc(item['pid']))
    l.atomic_json(l.PIDFILE,{'processes':[{'pid':10,'created':1,'role':'dashboard'},
        {'pid':11,'created':2,'role':'collector'}]})
    l.stop()
    assert events[0]==('wait',10)
    assert events[1]==('terminate',11)
    assert l.read_json(path/'stop-request.json')['pid']==10


def test_both_ports_checked_before_spawning_on_conflict(rig):
    _,m=rig
    def owner(kind,port,role):
        if role=='dashboard': raise RuntimeError('occupied')
        return None
    start=Mock(); m.setattr(l,'port_owner',owner); m.setattr(l,'_start_child',start)
    with pytest.raises(RuntimeError,match='occupied'): l.start(args())
    start.assert_not_called()


@pytest.mark.parametrize('raw',['{broken','{"processes":null}','{"processes":"bad"}'])
def test_corrupt_pidfile_reuses_ports_without_claiming_unowned_processes(rig,raw):
    _,m=rig
    l.PIDFILE.write_text(raw)
    m.setattr(l,'port_owner',lambda kind,port,role:SimpleNamespace(pid=10))
    start=Mock(); m.setattr(l,'_start_child',start)
    result=l.start(args())
    start.assert_not_called()
    assert result['processes']==[]


def test_auto_environment_never_inherits_live_lan_or_mission(monkeypatch):
    monkeypatch.setenv('DCS_DASH_ARM','YES'); monkeypatch.setenv('DCS_DASH_HOST','10.0.0.1')
    monkeypatch.setenv('DCS_DASH_PAIRING_TOKEN','secret'); monkeypatch.setenv('DCS_DASH_MISSION','old.miz')
    env,url=l.clean_environment(SimpleNamespace(automatic=True,arm=True,tablet=True,mission='x'))
    assert env['DCS_DASH_ARM']=='NO' and env['DCS_DASH_HOST']=='127.0.0.1'
    assert 'DCS_DASH_PAIRING_TOKEN' not in env and 'DCS_DASH_MISSION' not in env
    assert url=='http://127.0.0.1:18787'


def test_automatic_manual_stop_guard_and_manual_start_reset(rig):
    path,m=rig
    m.setattr(l,'dcs_sessions',lambda:[{'pid':1,'created':10}])
    l.atomic_json(path/'manual-stop.json',{'requested_epoch':11})
    a=args(); a.automatic=True
    with pytest.raises(RuntimeError,match='paused'): l.start(a)
    m.setattr(l,'port_owner',lambda kind,port,role:SimpleNamespace(pid=10))
    l.start(args())
    assert not (path/'manual-stop.json').exists()


def test_browser_once_per_dcs_session_and_manual_reopen_without_session(rig):
    _,m=rig
    browser=Mock(); m.setattr(l.webbrowser,'open',browser)
    session={'pid':1,'created':10}
    assert l.open_dashboard('http://127.0.0.1:18787',session)
    assert not l.open_dashboard('http://127.0.0.1:18787',session)
    assert browser.call_count==1
    l.open_dashboard('http://127.0.0.1:18787',{'pid':1,'created':11})
    assert browser.call_count==2


def test_manual_stop_marker_written_even_without_pidfile(rig):
    path,_=rig
    l.stop()
    assert l.read_json(path/'manual-stop.json')['requested_epoch']>0


def test_process_path_and_pid_creation_guard(monkeypatch):
    good=SimpleNamespace(exe=lambda:sys.executable,cmdline=lambda:[sys.executable,'-m','webapp.backend'],cwd=lambda:str(l.ROOT),create_time=lambda:12)
    assert l.expected_process(good,'dashboard')
    fake=SimpleNamespace(exe=lambda:r'C:\fake\python.exe',cmdline=good.cmdline,cwd=good.cwd)
    assert not l.expected_process(fake,'dashboard')
    monkeypatch.setattr(l.psutil,'Process',lambda pid:good)
    assert l.owned_process({'pid':1,'created':11,'role':'dashboard'}) is None


def test_singleton_lock_prevents_second_watcher(tmp_path):
    with l.process_lock(tmp_path/'watcher.lock'):
        with pytest.raises(RuntimeError):
            with l.process_lock(tmp_path/'watcher.lock'): pass


def test_invalid_saved_url_is_local_and_new_lan_url_stays_local():
    assert l.saved_url({'url':None})=='http://127.0.0.1:18787'
    assert l.saved_url({'url':'https://example.com/'})=='http://127.0.0.1:18787'
    assert l.saved_url({'url':'http://192.168.1.5:18787'})=='http://192.168.1.5:18787'
