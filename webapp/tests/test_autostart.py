from types import SimpleNamespace
import pytest
from webapp import launcher
from webapp.autostart import DCSWatcher


@pytest.fixture
def watcher_rig(tmp_path):
    now=[100.0]
    sessions=[]
    healthy=[False]
    starts=[]
    windows=[]
    def start():
        starts.append(now[0]); healthy[0]=True
        return 'http://127.0.0.1:18787'
    kwargs=dict(runtime=tmp_path,clock=lambda:now[0],sessions=lambda:list(sessions),
        health=lambda url:healthy[0],start=start,open_browser=lambda url,session:windows.append(launcher.session_key(session)))
    return DCSWatcher(**kwargs),now,sessions,healthy,starts,windows,tmp_path,kwargs


def session(pid=1,created=90):
    return {'pid':pid,'created':created,'exe':r'C:\FixtureDCS\bin\DCS.exe'}


def test_idle_never_starts_or_opens(watcher_rig):
    w,now,_,_,starts,windows,*_=watcher_rig
    for _ in range(4): w.tick(); now[0]+=2
    assert not starts and not windows
    assert w.last_status['state']=='Waiting for DCS'


def test_one_open_per_session_no_repeat_on_recovery_or_watcher_restart(watcher_rig):
    w,now,sessions,healthy,starts,windows,path,kwargs=watcher_rig
    sessions.append(session())
    w.tick()
    assert len(starts)==len(windows)==1
    now[0]+=6; healthy[0]=False; w.tick()
    assert len(starts)==2 and len(windows)==1
    restarted=DCSWatcher(**kwargs); restarted.tick()
    assert len(windows)==1
    sessions[0]=session(2,101); now[0]=110; restarted.tick()
    assert len(windows)==2


def test_manual_stop_stays_paused_until_new_session(watcher_rig):
    w,now,sessions,healthy,starts,windows,path,_=watcher_rig
    sessions.append(session()); w.tick()
    launcher.atomic_json(path/'manual-stop.json',{'requested_epoch':101})
    now[0]=102; healthy[0]=False; w.tick()
    assert w.last_status['state']=='Paused by manual stop'
    assert len(starts)==1
    now[0]=200; w.tick(); assert len(starts)==1
    sessions[0]=session(2,201); now[0]=202; w.tick()
    assert len(starts)==2


def test_close_leaves_dashboard_for_debrief_and_does_not_recover(watcher_rig):
    w,now,sessions,healthy,starts,windows,*_=watcher_rig
    sessions.append(session()); w.tick()
    sessions.clear(); healthy[0]=False; now[0]+=10; w.tick()
    assert len(starts)==1 and w.last_status['state']=='Debrief'


def test_crash_backoff_is_bounded_and_no_browser_on_failure(watcher_rig):
    w,now,sessions,healthy,starts,windows,*_=watcher_rig
    sessions.append(session())
    def fail(): raise RuntimeError('blocked port')
    w.start=fail
    w.tick(); assert w.retry_at==105
    now[0]=104; w.tick(); assert w.failures==1
    now[0]=105; w.tick(); assert w.retry_at==115
    for _ in range(8): now[0]=w.retry_at; w.tick()
    assert w.retry_at-now[0]==120 and not windows


def test_validated_install_detection_rejects_same_name_and_handles_pid_reuse(tmp_path):
    install = tmp_path / 'custom-install'
    (install / 'bin-mt').mkdir(parents=True)
    (install / 'Config' / 'Input').mkdir(parents=True)
    executable = install / 'bin-mt' / 'DCS.exe'
    executable.touch()
    processes=[SimpleNamespace(info={'pid':1,'name':'DCS.exe','exe':r'C:\fake\DCS.exe','create_time':90}),
        SimpleNamespace(info={'pid':2,'name':'DCS.exe','exe':str(executable),'create_time':91})]
    assert launcher.dcs_sessions(processes)==[{'pid':2,'created':91,'exe':str(executable)}]
    assert launcher.session_key(session(2,91))!=launcher.session_key(session(2,92))


def test_ambiguous_dcs_and_stop_marker_are_safe(watcher_rig):
    w,now,sessions,_,starts,_,path,_=watcher_rig
    sessions.extend([session(),session(2,91)]); w.tick()
    assert not starts
    launcher.atomic_json(path/'autostart-stop.json',{'requested_epoch':101})
    now[0]=102
    assert w.tick() is False
    assert launcher.read_json(path/'autostart-status.json')['enabled'] is False


def test_corrupt_ledger_recovers_without_dcs_execution(watcher_rig):
    w,_,sessions,_,starts,windows,path,_=watcher_rig
    (path/'autostart-session.json').write_text('{bad')
    sessions.append(session()); w.tick()
    assert len(starts)==len(windows)==1
