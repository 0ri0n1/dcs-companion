"""Rebuild an independent companion binding cache; no DCS/profile/input mutation."""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from webapp.backend.bindings import BindingService, SUPPORTED_AIRCRAFT, atomic_json, utc_now
from webapp.backend.adapters import list_adapters

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install-root')
    parser.add_argument('--saved-games')
    parser.add_argument('--data-dir')
    parser.add_argument('--aircraft', choices=SUPPORTED_AIRCRAFT, default='F-22A')
    args=parser.parse_args()
    from webapp.backend.config import Settings
    settings = Settings.from_env()
    setup_error = None
    install, saved = args.install_root, args.saved_games
    if install is None or saved is None:
        from webapp.backend.setup import SetupManager
        if install is not None:
            settings.install_root = Path(install)
        if saved is not None:
            settings.saved_games = Path(saved)
        manager = SetupManager(settings)
        install, saved = manager.active['install_path'], manager.active['profile_path']
        if not install or not saved:
            setup_error = '; '.join(manager.snapshot().get('issues', [])) or 'Select an available DCS installation and Saved Games profile in Your setup.'
    service=BindingService(args.data_dir or settings.data_dir,install,saved,
                           aircraft=args.aircraft,setup_error=setup_error)
    check = service.check_current()
    if not check.get('rebuild_eligible', True):
        print(json.dumps({'aircraft':args.aircraft, 'status':'Unavailable',
                          'reason':check.get('reason'), 'safe_actions':[]}, indent=2))
        return 2
    from webapp.backend.smart import generation_lock
    with generation_lock(settings.runtime_dir):
        result=service.refresh()
        atomic_json(service.data_dir/'adapters.json',{'schema':'dcs-companion/adapters/1',
            'generated_utc':utc_now(),'validation':'Not yet flight-tested',
            'adapters':list_adapters(service.install_root, service.saved_games)})
    print(json.dumps({k:result[k] for k in ('aircraft','status','dcs_version','source_fingerprint','counts','safe_actions')},indent=2))

if __name__=='__main__': raise SystemExit(main())
