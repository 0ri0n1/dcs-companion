"""Explicit aircraft capabilities: static source support is never a flight verdict."""
from pathlib import Path
import copy

ROOT = Path(__file__).resolve().parents[2]


def capability(status='Unverified', *, confirmable=False, detail='', source=None):
    return {'status':status,'confirmable':confirmable,'detail':detail,'source':str(source) if source else None}


def _base(identifier, name, identifiers, module_path, input_path, family):
    return {'id':identifier,'name':name,'identifiers':identifiers,
        'installed':module_path.is_dir(),'module_path':str(module_path),'input_path':str(input_path),
        'family':family,'status':'Not yet flight-tested' if module_path.is_dir() else 'Unsupported',
        'validation':'Not yet flight-tested','command_enabled':False,
        'telemetry':{'source':'collector.py → state/state.json','supported_fields':['latitude','longitude',
            'heading_true','heading_magnetic','ground_speed','ground_track','altitude','vertical_speed','attitude'],
            'status':'Unverified','detail':'Field availability and freshness are evaluated independently from the active stream.'},
        'capabilities':{key:capability() for key in ['cockpit_displays','tacan','ils','icls','radio_tuning',
            'waypoint_entry','route_sequencing','autopilot','coupled_navigation','sensor_contacts']},
        'limitations':[],'safe_action_ids':[]}


def list_adapters(install_root=None, saved_games=None, *, setup_error=None):
    from .bindings import resolve_binding_roots
    from .discovery import SetupUnavailable
    if setup_error:
        return []
    try:
        install, saved = resolve_binding_roots(install_root, saved_games)
    except SetupUnavailable:
        return []
    mod=saved/'Mods'/'aircraft'/'f-22a'
    f22=_base('f22','F-22A Raptor',['F-22A'],mod,mod/'Input'/'F-22A','fc-navigation-with-custom-cockpit')
    f22['binding_status']='Static checks passed' if mod.is_dir() else 'Unsupported'
    f22['command_enabled']=mod.is_dir()
    f22['safe_action_ids']=['f22.nav.next','f22.nav.previous','f22.nav.mode'] if mod.is_dir() else []
    f22['capabilities']['navigation_selection']=capability('Static checks passed',
        detail='Installed module inherits next/previous selection and navigation-mode actions. Runtime effect is unverified.',
        source=install/'Config'/'Input'/'Aircrafts'/'base_keyboard_binding.lua')
    f22['capabilities']['cockpit_displays']=capability('Unverified',
        detail='Custom MFD/ICP definitions exist. Indicator IDs and reliable readback require an instrumented flight.',
        source=mod/'Cockpit'/'Scripts'/'device_init.lua')
    f22['capabilities']['ils']=capability('Unverified',
        detail='Installed MFD code contains ILS flags/deviation parameters. Export readback and mode selection have not been flight-tested.',
        source=mod/'Cockpit'/'Scripts'/'Systems'/'MFD_System.lua')
    f22['capabilities']['sensor_contacts']=capability('Unverified',
        detail='No tactical layer: previous contact data was inconsistent. Require fresh onboard-only export and flight verification.')
    f22['limitations']=['All queued actions remain Unconfirmed because this adapter has no validated observable transition.',
        'No TACAN/radio tuning, cockpit waypoint entry, automatic sequence, autopilot or flight-control actuation is enabled.',
        'Tartarus DCS joystick assignments are separate from Unknown Synapse keycap-to-key mappings.',
        'No world-object fallback is used for radar contacts.']

    mod18=install/'Mods'/'aircraft'/'FA-18C'
    f18=_base('fa18','F/A-18C Hornet',['FA-18C_hornet'],mod18,mod18/'Input'/'FA-18C','full-fidelity')
    f18['binding_status']='Static checks passed' if mod18.is_dir() else 'Unsupported'
    f18['binding_catalog']={'aircraft':'FA-18C_hornet','display_only':True,
        'saved_input_path':str(saved/'Config'/'Input'/'FA-18C_hornet')}
    f18['remote_panels']={'status':'Not yet flight-tested', 'panels':['ufc','left_mdi','right_mdi','ampcd'],
        'detail':'Remote resolves momentary panel buttons from current keyboard bindings; preview is the default and live requires explicit PC enable.'}
    f18['limitations']=['Controls and Guide remain references; the separate Remote workspace permits resolved momentary panel buttons after explicit live enable.',
        'F-22 command IDs, key bindings and display IDs are never inherited.']
    f18['reuse']={'display_maps':[str(ROOT/'binds'/p) for p in ['ampcd-map.json','ddi-left-map.json','ddi-right-map.json']],
        'tools':[str(ROOT/'tools'/p) for p in ['waypoint_macro.py','set_waypoint.py','approach_setup.py','glidepath_monitor.py','descent_copilot.py']],
        'validation':'Local reference assets; compatibility with this installation is unverified.'}
    for feature in ['cockpit_displays','tacan','icls','waypoint_entry','route_sequencing','autopilot','coupled_navigation']:
        f18['capabilities'][feature]=capability('Not yet flight-tested',detail='Existing measured Hornet Copilot implementation is available for future adapter integration; browser implementation is not enabled.')

    mod15=install/'Mods'/'aircraft'/'F-15C'
    f15=_base('f15','F-15C Eagle',['F-15C'],mod15,mod15/'Input'/'F-15C','unverified')
    mod23=saved/'Mods'/'aircraft'/'F-23A'
    f23=_base('f23','F-23A mod',['F-23A'],mod23,mod23/'Input'/'F-23A','unverified')
    mod35=saved/'Mods'/'aircraft'/'VSN_F35'
    f35=_base('f35','VSN F-35 mod',['VSN_F35A','VSN_F35A_AG','VSN_F35B','VSN_F35B_AG','VSN_F35C','VSN_F35C_AG'],mod35,mod35/'Input','unverified')
    for adapter in [f15,f23,f35]:
        adapter['limitations']=['Installed status does not establish runtime avionics compatibility.',
            'No bindings or avionics capabilities are inherited from F-22A.',
            'Shared navigation remains usable with valid ownship/terrain data; cockpit programming is unavailable.']
    return [f22,f18,f15,f23,f35]


def get_adapter(aircraft, install_root=None, saved_games=None, *, setup_error=None):
    for adapter in list_adapters(install_root, saved_games, setup_error=setup_error):
        if aircraft in adapter['identifiers']:
            return copy.deepcopy(adapter)
    return {'id':'unknown','name':aircraft or 'No aircraft','identifiers':[],
        'installed':False,'status':'Unsupported','validation':'Unsupported','command_enabled':False,
        'safe_action_ids':[],'capabilities':{},'limitations':['No cockpit binding inheritance. Generic navigation can still use valid ownship data.']}


def can_confirm(aircraft, action_id, before=None, after=None):
    """No F-22 transition has passed the required instrumented flight verification."""
    return False
