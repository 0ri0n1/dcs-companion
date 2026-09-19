import unittest
from webapp.backend.adapters import get_adapter,list_adapters,can_confirm


class AdapterTests(unittest.TestCase):
    def test_f22_static_is_not_flight_evidence(self):
        adapter=get_adapter('F-22A')
        self.assertEqual(adapter['validation'],'Not yet flight-tested')
        self.assertEqual(adapter['capabilities']['sensor_contacts']['status'],'Unverified')
        for key in adapter['safe_action_ids']:
            self.assertFalse(can_confirm('F-22A',key,{'wp':1},{'wp':2}))

    def test_fa18_is_separate_full_fidelity(self):
        adapter=get_adapter('FA-18C_hornet')
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
