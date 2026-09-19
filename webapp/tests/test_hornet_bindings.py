"""Independent Hornet catalogs, source changes and display-only authority."""
import json
from pathlib import Path
import tempfile
import unittest
import pytest
from unittest.mock import patch

from webapp.backend.bindings import BindingService, fingerprint, SUPPORTED_AIRCRAFT


HORNET = 'FA-18C_hornet'
HOTAS = 'T.Flight Hotas One {00000002-0002-4002-8002-000000000002}'
TARTARUS = 'Razer Tartarus Pro {00000003-0003-4003-8003-000000000003}'


class HornetBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.f18 = BindingService(self.root/'data', self.root/'Install', self.root/'Saved', aircraft=HORNET)
        self.f22 = BindingService(self.root/'data', self.root/'Install', self.root/'Saved')

    def write(self, path, text='return {}'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def mark_current(self, service):
        default=service.module_root/'keyboard/default.lua'
        if not default.exists():
            self.write(default)
        service._snapshot = {'aircraft': service.aircraft,
            'source_fingerprint': fingerprint(service.current_sources()),
            'actions': [{'name':'Owned row'}], 'safe_actions': [{'id':'forged'}]}

    def test_explicit_paths_and_unsupported_identifiers(self):
        self.assertEqual(self.f18.module_root, self.root/'Install/Mods/aircraft/FA-18C/Input/FA-18C')
        self.assertEqual(self.f18.user_input_root, self.root/'Saved/Config/Input/FA-18C_hornet')
        self.assertEqual(self.f18.cockpit_root, self.root/'Install/Mods/aircraft/FA-18C/Cockpit/Scripts')
        self.assertEqual(self.f22.aircraft, 'F-22A')
        self.assertIn(HORNET, SUPPORTED_AIRCRAFT)
        for value in ('F/A-18C', 'F-15C', '', None):
            with self.assertRaises(ValueError):
                BindingService(aircraft=value)

    def test_cache_names_and_reload_stay_independent(self):
        self.assertEqual(self.f18.cache_path.name, 'f18-snapshot.json')
        self.assertEqual(self.f18.allowlist_path.name, 'f18-allowlist.json')
        for service in (self.f18, self.f22):
            self.mark_current(service)
            self.write(service.cache_path, json.dumps(service._snapshot))
        loaded = BindingService(self.root/'data', self.root/'Install', self.root/'Saved', aircraft=HORNET)
        self.assertEqual(loaded.snapshot()['aircraft'], HORNET)
        self.assertTrue(loaded.check_current()['valid'])
        self.assertNotEqual(loaded.cache_path, self.f22.cache_path)

    def test_hornet_never_inherits_or_enables_cached_commands(self):
        self.mark_current(self.f18)
        self.assertEqual(self.f18.snapshot()['safe_actions'], [])
        self.assertEqual(self.f18.get_safe_actions(), [])
        self.assertEqual(self.f18.safe_command_ids(), {})
        self.assertEqual(self.f18.snapshot('F-22A')['actions'], [])
        self.assertEqual(self.f22.snapshot(HORNET)['actions'], [])
        self.f18._snapshot['aircraft'] = 'F-22A'
        self.assertFalse(self.f18.check_current()['valid'])
        self.assertEqual(self.f18.snapshot()['actions'], [])

    def test_per_aircraft_diffs_invalidate_only_owned_catalog(self):
        for owner, other in ((self.f18, self.f22), (self.f22, self.f18)):
            for service in (owner, other): self.mark_current(service)
            source = self.write(owner.user_input_root/'keyboard/Keyboard.diff.lua')
            self.assertFalse(owner.check_current()['valid'])
            self.assertTrue(other.check_current()['valid'])
            self.mark_current(owner)
            source.write_text('return {keyDiffs={}}')
            self.assertFalse(owner.check_current()['valid'])
            self.mark_current(owner)
            source.unlink()
            self.assertFalse(owner.check_current()['valid'])

    def test_installed_hornet_definitions_and_supercarrier_are_dependencies(self):
        paths = [self.f18.module_root/'keyboard/default.lua',
                 self.f18.cockpit_root/'devices.lua', self.f18.cockpit_root/'command_defs.lua',
                 self.f18.install_root/'Config/Input/Supercarrier/Input/keyboard.lua']
        for path in paths:
            for service in (self.f18,self.f22): self.mark_current(service)
            self.write(path, 'return {fixture_dependency_change=true}')
            self.assertFalse(self.f18.check_current()['valid'], str(path))
            self.assertTrue(self.f22.check_current()['valid'], str(path))

    def test_global_layers_and_modifiers_invalidate_catalog(self):
        for suffix in ('UiLayer/keyboard/Keyboard.diff.lua', 'VoiceChat/joystick/new.diff.lua',
                       'CommandMenu/keyboard/Keyboard.diff.lua', 'modifiers.lua', 'wizard.lua', 'disabled.lua'):
            for service in (self.f18,self.f22): self.mark_current(service)
            self.write(self.f18.saved_games/'Config/Input'/suffix)
            self.assertFalse(self.f18.check_current()['valid'], suffix)
            self.assertFalse(self.f22.check_current()['valid'], suffix)

    def test_hornet_custom_modifier_change_is_watched(self):
        self.mark_current(self.f18)
        self.write(self.f18.user_input_root/'modifiers.lua')
        self.assertFalse(self.f18.check_current()['valid'])

    def test_refresh_keeps_global_conflicts_axes_and_empty_allowlist(self):
        self.write(self.f18.module_root/'keyboard/default.lua')
        self.write(self.f18.module_root/'joystick/default.lua')
        self.write(self.f18.user_input_root/'keyboard/Keyboard.diff.lua')
        self.write(self.f18.user_input_root/'joystick'/(HOTAS+'.diff.lua'))
        self.write(self.f18.user_input_root/'modifiers.lua')
        self.write(self.f18.install_root/'Config/Input/UiLayer/keyboard/default.lua')
        self.write(self.f18.install_root/'autoupdate.cfg', '{"version":"fixture-version"}')

        def extract(path, device='Keyboard'):
            if path.name == 'modifiers.lua':
                return {'raw': {'MOD1':{'device':HOTAS,'key':'JOY_BTN9','switch':False}}}
            if path.name.endswith('.diff.lua'):
                raw = {'keyDiffs':{'d1':{'name':'Gear','removed':[{'key':'G'}], 'added':[{'key':'K'}]}}} if device == 'Keyboard' else {
                    'axisDiffs':{'a2001':{'name':'Pitch', 'changed':[{'key':'JOY_Y','filter':{'deadzone':0.15}}]}}}
                return {'raw':raw}
            if 'UiLayer' in path.parts:
                return {'actions':[{'name':'Global pause','combos':[{'key':'K'}]}]}
            return {'actions':[{'name':'Gear','combos':[{'key':'G'}]},
                               {'name':'Pitch','kind':'axis','combos':[]}]}

        with patch.object(self.f18, '_extract', side_effect=extract), patch.object(self.f18, 'last_seen_devices', return_value=[TARTARUS]):
            result = self.f18.refresh()
        self.assertEqual(result['aircraft'], HORNET)
        self.assertTrue(result['display_only'])
        self.assertEqual(result['safe_actions'], [])
        self.assertEqual(len(result['devices']), 3)
        self.assertEqual(result['conflicts'][0]['combo'], 'K')
        hotas_id = next(device['id'] for device in result['devices'] if device['name'] == HOTAS)
        pitch = next(row for row in result['actions'] if row['name']=='Pitch' and row['device_id'] == hotas_id)
        self.assertEqual(pitch['combos'], ['JOY_Y'])
        self.assertEqual(pitch['axis_filters'][0]['filter']['deadzone'], 0.15)
        self.assertEqual(result['modifiers']['MOD1']['key'], 'JOY_BTN9')
        self.assertTrue(all(row['aircraft']==HORNET for row in result['actions']))
        self.assertEqual(json.loads(self.f18.allowlist_path.read_text())['actions'], [])
        self.assertFalse(self.f22.cache_path.exists())
        tartarus = next(device for device in result['devices'] if device['type']=='tartarus')
        self.assertEqual(tartarus['physical_mapping_status'], 'Unknown')
        self.assertEqual(tartarus['connected'], 'Unknown')


@pytest.mark.local_integration
class InstalledHornetBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.service = BindingService(Path(cls.tmp.name), aircraft=HORNET)
        if not (cls.service.module_root/'keyboard/default.lua').is_file():
            raise unittest.SkipTest('Installed Hornet sources unavailable')
        cls.snapshot = cls.service.refresh()

    def lookup(self, name, device_type):
        devices = {row['id']:row for row in self.snapshot['devices']}
        return next(row for row in self.snapshot['actions'] if row['name']==name and
                    row['scope']=='aircraft' and devices[row['device_id']]['type']==device_type)

    def test_current_keyboard_and_hotas_assignments(self):
        self.assertIn('LCtrl+T', self.lookup('UFC Function Selector Pushbutton - TCN','keyboard')['combos'])
        self.assertIn('RAlt+1', self.lookup('Left MDI PB 1','keyboard')['combos'])
        self.assertIn('MOD1+JOY_BTN8', self.lookup('RAID/FLIR FOV Select Button','hotas')['combos'])
        self.assertIn('JOY_SLIDER1', self.lookup('Zoom View','hotas')['combos'])
        self.assertEqual(self.snapshot['modifiers']['MOD1']['key'], 'JOY_BTN9')
        self.assertEqual(self.snapshot['modifiers']['MOD2']['key'], 'JOY_BTN11')

    def test_hornet_has_no_tartarus_profile_or_f22_commands(self):
        # A fresh isolated build may have no census after DCS rotates its logs.
        # Do not turn an earlier machine inventory into required live evidence.
        observed = set(self.service.last_seen_devices())
        for device in self.snapshot['devices']:
            if device['type']=='tartarus':
                self.assertIn(device['name'], observed)
                self.assertEqual(device['status'], 'Last seen in DCS; defaults')
                self.assertEqual(self.lookup('Pitch','tartarus')['combos'], [])
        self.assertEqual(self.snapshot['safe_actions'], [])
        self.assertFalse(any(row['name']=='ICP ALT' for row in self.snapshot['actions']))
        self.assertTrue(self.service.check_current()['valid'])

    def test_every_evaluated_dependency_is_fingerprinted(self):
        watched = {str(Path(row['path']).resolve()).lower() for row in self.snapshot['sources']}
        for device in self.snapshot['devices']:
            dtype = 'keyboard' if device['id']=='keyboard' else 'joystick'
            extracted = self.service._extract(self.service.module_root/dtype/'default.lua', device['name'])
            for path in extracted['sources']:
                self.assertIn(str(Path(path).resolve()).lower(), watched)
        self.assertFalse(any('binds.json' in row['path'] for row in self.snapshot['sources']))


if __name__ == '__main__': unittest.main()
