import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch
from webapp.backend.adapters import get_adapter,list_adapters,can_confirm


class AdapterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        install, saved = root / 'Install', root / 'Saved'
        for module in (install / 'Mods/aircraft/FA-18C', saved / 'Mods/aircraft/f-22a'):
            module.mkdir(parents=True)
        # Exercise the real adapter builder using only our synthetic installation.
        # No registry, process, Steam, or Saved Games discovery reaches this host.
        discovery = patch('webapp.backend.discovery.resolve_setup_paths', return_value=(install, saved))
        self.addCleanup(discovery.stop)
        discovery.start()

    def test_f22_static_is_not_flight_evidence(self):
        adapter=get_adapter('F-22A')
        self.assertTrue(adapter['installed'])
        self.assertTrue(adapter['safe_action_ids'])
        self.assertEqual(adapter['validation'],'Not yet flight-tested')
        self.assertEqual(adapter['capabilities']['sensor_contacts']['status'],'Unverified')
        for key in adapter['safe_action_ids']:
            self.assertFalse(can_confirm('F-22A',key,{'wp':1},{'wp':2}))

    def test_fa18_is_separate_full_fidelity(self):
        adapter=get_adapter('FA-18C_hornet')
        self.assertTrue(adapter['installed'])
        self.assertEqual(adapter['family'],'full-fidelity')
        self.assertFalse(adapter['command_enabled'])
        self.assertIn('display_maps',adapter['reuse'])

    def test_future_and_unknown_aircraft_never_inherit_controls(self):
        for aircraft in ['F-15C','F-23A','VSN_F35A','VSN_F35B','VSN_F35C','Some-Mod','F22']:
            adapter=get_adapter(aircraft)
            self.assertFalse(adapter['command_enabled'])
            self.assertEqual(adapter['safe_action_ids'],[])
            self.assertFalse(can_confirm(aircraft,'f22.nav.next'))

    def test_identifiers_unique_and_unknown_explicit(self):
        ids=[n for a in list_adapters() for n in a['identifiers']]
        self.assertEqual(len(ids),len(set(ids)))
        self.assertEqual(get_adapter('Some-Mod')['status'],'Unsupported')


if __name__=='__main__': unittest.main()
