import copy
import json
from pathlib import Path
import tempfile
import unittest
import pytest
from uuid import UUID
from unittest.mock import patch

from webapp.backend.bindings import BindingService, ROOT, fingerprint, merge_actions, detect_conflicts


def action(name, key, **extra):
    return {'name':name,'category':['Navigation'],'combos':[{'key':key}] if key else [], **extra}


class BindingMergeTests(unittest.TestCase):
    def test_defaults_removed_added_unbound_and_global_conflicts(self):
        defaults=[action('Next','N'),action('Unused','U'),action('Module','X')]
        diff={'keyDiffs':{'id1':{'name':'Next','removed':[{'key':'N'}],
            'added':[{'key':'P','reformers':['LAlt','LCtrl']}]},
            'id2':{'name':'Unused','removed':[{'key':'U'}]}}}
        merged=merge_actions(defaults,diff,'keyboard')
        rows={a['name']:a for a in merged}
        self.assertEqual(rows['Next']['combos'],['LCtrl+LAlt+P'])
        self.assertEqual(rows['Unused']['combos'],[])
        globals_=merge_actions([action('Global','X')],{},'keyboard','UiLayer')
        conflict=detect_conflicts(merged+globals_)
        self.assertEqual(conflict[0]['actions'],['Module','Global'])
        self.assertEqual(conflict[0]['severity'],'conflict')

    def test_same_button_on_different_guids_is_not_conflict(self):
        one=merge_actions([action('Next','JOY_BTN1')],{},'joystick:ONE')
        two=merge_actions([action('Fire','JOY_BTN1')],{},'joystick:TWO')
        self.assertEqual(detect_conflicts(one+two),[])

    def test_axis_changed_reapplied_as_assignment_like_installed_dcs_loader(self):
        result=merge_actions([action('Pitch',None,kind='axis')],
            {'axisDiffs':{'a2001cdnil':{'name':'Pitch','changed':[{'key':'JOY_Y','filter':{'deadzone':0.2}}]}}},'joy')
        self.assertEqual(result[0]['combos'],['JOY_Y'])
        self.assertEqual(result[0]['axis_filters'][0]['filter']['deadzone'],0.2)

    def test_changed_axis_clears_other_default_per_dcs_loader(self):
        result=merge_actions([action('Pitch','JOY_Y',kind='axis'),action('Roll',None,kind='axis')],
            {'axisDiffs':{'a2002cdnil':{'name':'Roll','changed':[{'key':'JOY_Y'}]}}},'joy')
        rows={a['name']:a for a in result}
        self.assertEqual(rows['Pitch']['combos'],[])
        self.assertEqual(rows['Roll']['combos'],['JOY_Y'])

    def test_user_only_control_is_explicitly_unverified(self):
        result=merge_actions([],{'keyDiffs':{'id':{'name':'Invented','added':[{'key':'X'}]}}},'keyboard')
        self.assertFalse(result[0]['verified_default'])

    def test_menu_overlap_is_context_dependent(self):
        a=merge_actions([action('Cockpit','F1')],{},'keyboard')
        b=merge_actions([action('Menu item','F1')],{},'keyboard','CommandMenu')
        self.assertEqual(detect_conflicts(a+b)[0]['severity'],'context-dependent')

    def test_changed_removed_and_new_sources_invalidate_allowlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)
            saved=base/'Saved'
            directory=saved/'Config'/'Input'/'F-22A'/'keyboard'
            directory.mkdir(parents=True)
            source=directory/'Keyboard.diff.lua'
            source.write_text('return {}')
            service=BindingService(base/'data',base/'Install',saved)
            default=service.module_root/'keyboard'/'default.lua'
            default.parent.mkdir(parents=True)
            default.write_text('return {}')
            service._snapshot={'source_fingerprint':fingerprint(service.current_sources()),
                               'safe_actions':[{'id':'fixture'}]}
            self.assertTrue(service.check_current()['valid'])
            source.write_text('return {keyDiffs={}}')
            self.assertFalse(service.check_current()['valid'])
            self.assertEqual(service.get_safe_actions(),[])
            service._snapshot['source_fingerprint']=fingerprint(service.current_sources())
            source.unlink()
            self.assertFalse(service.check_current()['valid'])
            service._snapshot['source_fingerprint']=fingerprint(service.current_sources())
            (directory/'new.diff.lua').write_text('return {}')
            self.assertFalse(service.check_current()['valid'])

    def test_html_changes_are_not_binding_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            service=BindingService(Path(tmp)/'data',Path(tmp)/'Install',Path(tmp)/'Saved')
            before=fingerprint(service.current_sources())
            html=service.saved_games/'InputLayoutsTxt'/'F-22A'/'Keyboard.html'
            html.parent.mkdir(parents=True)
            html.write_text('<tr><td>X</td><td>Next Waypoint</td></tr>')
            self.assertEqual(fingerprint(service.current_sources()),before)
            self.assertFalse(any(str(p).endswith('.html') for p in service.source_paths()))

    def test_other_aircraft_never_inherit_bindings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service=BindingService(root/'data',root/'Install',root/'Saved')
            for aircraft in ['FA-18C_hornet','F-15C','F-23A','VSN_F35A','Unknown']:
                self.assertEqual(service.snapshot(aircraft)['actions'],[])
                self.assertEqual(service.get_safe_actions(aircraft),[])


class DeviceHistoryTests(unittest.TestCase):
    DEVICE='Joystick (Razer Tartarus Pro) {00000003-0003-4003-8003-000000000003}'

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.service=BindingService(self.root/'data',self.root/'Install',self.root/'Saved',aircraft='FA-18C_hornet')
        self.log=self.service.saved_games/'Logs'/'dcs.log'
        self.log.parent.mkdir(parents=True)
        default=self.service.module_root/'keyboard'/'default.lua'
        default.parent.mkdir(parents=True)
        default.write_text('return {}')

    def line(self, name=None):
        return '2026 INFO INPUT: created [Joystick] with full id ['+(name or self.DEVICE)+']\n'

    def cache(self, service=None, *, legacy=False, backup=False):
        service=service or self.service
        sources=service.current_sources()
        result={'schema':'dcs-companion/bindings/1','aircraft':service.aircraft,
                'source_fingerprint':fingerprint(sources),'sources':sources,
                'generated_utc':'2026-09-18T20:00:00Z',
                'devices':[{'name':self.DEVICE,'status':'Last seen in DCS; defaults'}],
                'actions':[{'name':'Do not inherit this assignment','combos':['X']}],
                'observed_devices':[] if legacy else list(service._device_history.values())}
        path=service.cache_path
        if backup: path=path.parent/'backups'/(path.name+'.fixture')
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(result))
        return path

    def test_log_rotation_does_not_remove_observed_identity_or_invalidate(self):
        self.log.write_text(self.line())
        self.assertEqual(self.service.last_seen_devices(),[self.DEVICE])
        revision=fingerprint(self.service.current_sources())
        self.log.write_text('DCS started; no INPUT census yet.\n')
        self.assertEqual(self.service.last_seen_devices(),[self.DEVICE])
        self.assertEqual(fingerprint(self.service.current_sources()),revision)
        self.log.unlink()
        self.assertEqual(fingerprint(self.service.current_sources()),revision)

    def test_previous_log_is_evidence_and_new_identity_invalidates(self):
        self.log.with_name('dcs.log.old').write_text(self.line())
        self.assertEqual(self.service.last_seen_devices(),[self.DEVICE])
        revision=fingerprint(self.service.current_sources())
        second='HOTAS {00000001-0001-4001-8001-000000000001}'
        self.log.write_text(self.line(second))
        self.assertEqual(set(self.service.last_seen_devices()),{self.DEVICE,second})
        self.assertNotEqual(fingerprint(self.service.current_sources()),revision)

    def test_current_cache_retains_observation_after_restart(self):
        self.log.write_text(self.line())
        self.cache()
        self.log.write_text('Rotated without a census')
        reloaded=BindingService(self.root/'data',self.root/'Install',self.root/'Saved',aircraft='FA-18C_hornet')
        self.assertEqual(reloaded.last_seen_devices(),[self.DEVICE])
        self.assertEqual(reloaded._device_history[self.DEVICE]['source_path'],str(self.log))
        self.assertEqual(len(reloaded._device_history[self.DEVICE]['source_sha256']),64)

    def test_legacy_own_backup_restores_identity_not_assignments(self):
        self.log.write_text(self.line())
        path=self.cache(legacy=True,backup=True)
        self.log.write_text('Rotated without a census')
        reloaded=BindingService(self.root/'data',self.root/'Install',self.root/'Saved',aircraft='FA-18C_hornet')
        self.assertEqual(reloaded.last_seen_devices(),[self.DEVICE])
        self.assertIsNone(reloaded._snapshot)
        self.assertEqual(reloaded._device_history[self.DEVICE]['evidence_cache_path'],str(path))
        self.assertEqual(reloaded.snapshot()['actions'],[])
        other=BindingService(self.root/'data',self.root/'Install',self.root/'Saved')
        self.assertEqual(other.last_seen_devices(),[])

    def test_foreign_or_modified_backup_cannot_seed_devices(self):
        self.log.write_text(self.line())
        path=self.cache(legacy=True,backup=True)
        self.log.write_text('Rotated without a census')
        original=json.loads(path.read_text())
        for field,value in [('aircraft','F-22A'),('source_fingerprint','changed')]:
            damaged=copy.deepcopy(original)
            damaged[field]=value
            path.write_text(json.dumps(damaged))
            loaded=BindingService(self.root/'data',self.root/'Install',self.root/'Saved',aircraft='FA-18C_hornet')
            self.assertEqual(loaded.last_seen_devices(),[])

    def test_staging_inherits_evidence_only_and_rejects_other_aircraft(self):
        self.log.write_text(self.line())
        self.service.last_seen_devices()
        self.log.write_text('Rotated without a census')
        candidate=BindingService(self.root/'staging',self.root/'Install',self.root/'Saved',aircraft='FA-18C_hornet')
        candidate.inherit_device_history(self.service)
        self.assertEqual(candidate.last_seen_devices(),[self.DEVICE])
        self.assertIsNone(candidate._snapshot)
        self.assertEqual(fingerprint(candidate.current_sources()),fingerprint(self.service.current_sources()))
        wrong=BindingService(self.root/'other',self.root/'Install',self.root/'Saved')
        with self.assertRaises(ValueError): wrong.inherit_device_history(self.service)


@pytest.mark.local_integration
class InstalledF22BindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path=ROOT/'webapp'/'data'/'bindings'/'f22-snapshot.json'
        if not cls.path.is_file(): raise unittest.SkipTest('Run build_bindings.py for local-source validation')
        cls.snapshot=json.loads(cls.path.read_text(encoding='utf-8'))

    def lookup(self,name,kind):
        devices={d['id']:d for d in self.snapshot['devices']}
        return next(a for a in self.snapshot['actions'] if a['name']==name and
                    devices[a['device_id']]['type']==kind and a['scope']=='aircraft')

    def test_actual_keyboard_default_and_user_diff(self):
        self.assertEqual(self.lookup('Next Waypoint, Airfield Or Target','keyboard')['combos'],['LCtrl+`'])
        self.assertEqual(self.lookup('(1) Navigation Modes','keyboard')['combos'],['1'])
        self.assertIn('RCtrl+1',self.lookup('Weapon Bay: Select Left','keyboard')['combos'])
        self.assertEqual(self.lookup('Weapon Bay: Select Left','keyboard')['command_id'],'d10000pnilunilcdnilvdnilvpnilvunil')

    def test_actual_tartarus_profile_and_unknown_physical_mapping(self):
        row=self.lookup('ICP ALT','tartarus')
        self.assertEqual(row['combos'],['JOY_BTN8'])
        device=next(d for d in self.snapshot['devices'] if d['type']=='tartarus')
        self.assertEqual(str(UUID(device['guid'])).upper(), device['guid'])
        self.assertEqual(device['physical_mapping_status'],'Unknown')
        # DCS DefaultAssignments gives Tartarus AUX_PANEL_DEFAULT, not auto axes.
        self.assertEqual(self.lookup('Pitch','tartarus')['combos'],[])

    def test_sources_and_allowlist_are_isolated(self):
        self.assertTrue(all(s['sha256'] for s in self.snapshot['sources'] if s['exists']))
        self.assertTrue(any('DefaultAssignments.lua' in s['path'] for s in self.snapshot['sources']))
        service=BindingService()
        self.assertNotEqual(service.allowlist_path,ROOT/'binds'/'binds.json')
        allowlist=json.loads(service.allowlist_path.read_text(encoding='utf-8'))
        self.assertEqual(len(allowlist['actions']),3)
        self.assertTrue(all(len(a['combos'])==1 for a in allowlist['actions']))
        self.assertTrue(all(not a['observable'] for a in self.snapshot['safe_actions']))

    def test_actual_numeric_identity_and_mislabelled_diff_fails_closed(self):
        service=BindingService()
        self.assertEqual(service.safe_command_ids()['f22.nav.next'],'d102pnilunilcdnilvdnilvpnilvunil')
        original=service._diff
        def forged(path,device):
            if path.name=='Keyboard.diff.lua':
                return {'keyDiffs':{'d999pnilunilcdnilvdnilvpnilvunil':{
                    'name':'Next Waypoint, Airfield Or Target','added':[{'key':'N'}]}}}
            return original(path,device)
        with tempfile.TemporaryDirectory() as tmp:
            service.data_dir=Path(tmp)
            service.cache_path=Path(tmp)/'snapshot.json'
            service.allowlist_path=Path(tmp)/'allowlist.json'
            with patch.object(service,'_diff',side_effect=forged):
                result=service.refresh()
            self.assertNotIn('f22.nav.next',[a['id'] for a in result['safe_actions']])


if __name__=='__main__': unittest.main()
