"""Resolve a physical bezel key without discarding its clean alternate binding."""
import json

import pytest

from webapp.backend.remote_catalog import build_catalog


def snapshot(combos, conflicts=(), other=()):
    action = dict(
        name='AMPCD PB 16', device_id='keyboard', kind='key',
        verified_default=True, combos=combos, conflicts=list(conflicts),
        signature=json.dumps(dict(cockpit_device_id=37, down=3026, up=3026, value_down=1, value_up=0)),
        command_id='d3026pnilu3026cd37vd1vpnilvu0')
    return dict(aircraft='FA-18C_hornet', status='Current', source_fingerprint='current',
                current_fingerprint='current', actions=[action, *other])


def other_action(combo, **extra):
    return dict(name='Chat show/hide', device_id='keyboard', kind='key',
                combos=[combo], conflicts=[], **extra)


def button(data):
    return next(row for row in build_catalog(data) if row['id'] == 'hornet.ampcd.pb.16')


def test_clean_right_shift_binding_survives_conflicting_alternate():
    result = button(snapshot(['RShift+Y', 'LCtrl+LShift+Y'], ['LCtrl+LShift+Y'],
                             [other_action('LCtrl+LShift+Y')]))
    assert result['available'] and result['allowlisted']
    assert result['combo'] == 'RShift+Y'
    assert result['reason'] is None


def test_conflicting_first_choice_is_skipped_without_reordering_clean_alternates():
    result = button(snapshot(['LCtrl+LShift+Y', 'RShift+Y', 'RAlt+Y'], ['LCtrl+LShift+Y'],
                             [other_action('LCtrl+LShift+Y')]))
    assert result['available'] and result['combo'] == 'RShift+Y'


@pytest.mark.parametrize('marked', [True, False])
def test_only_conflicting_binding_is_unavailable_even_without_conflict_annotation(marked):
    result = button(snapshot(['LCtrl+LShift+Y'], ['LCtrl+LShift+Y'] if marked else [],
                             [other_action('LCtrl+LShift+Y')]))
    assert not result['available'] and not result['allowlisted']
    assert result['combo'] is None and 'Conflicting' in result['reason']


def test_cross_action_collision_is_filtered_even_if_annotation_is_missing():
    result = button(snapshot(['LCtrl+LShift+Y', 'RShift+Y'], other=[other_action('LCtrl+LShift+Y')]))
    assert result['available'] and result['combo'] == 'RShift+Y'


def test_modifier_order_and_letter_case_do_not_hide_a_collision():
    result = button(snapshot(['LCtrl+LShift+Y', 'RShift+Y'], other=[other_action('LShift+LCtrl+y')]))
    assert result['available'] and result['combo'] == 'RShift+Y'


def test_joystick_assignment_does_not_conflict_with_keyboard_combo():
    peer = other_action('RShift+Y')
    peer['device_id'] = 'joystick:fixture'
    result = button(snapshot(['RShift+Y'], other=[peer]))
    assert result['available'] and result['combo'] == 'RShift+Y'


@pytest.mark.parametrize('combos', [['UnknownKey'], ['RShift+UnknownKey'], ['RShift++Y'], [''], [None]])
def test_unsupported_combos_fail_closed(combos):
    result = button(snapshot(combos))
    assert not result['available'] and result['combo'] is None


def test_unsupported_alternate_is_skipped_for_verified_sendable_key():
    result = button(snapshot(['UnknownKey', 'RShift+Y']))
    assert result['available'] and result['combo'] == 'RShift+Y'


@pytest.mark.parametrize('combos', ['RShift+Y', {'RShift+Y': True}, None])
def test_malformed_binding_container_fails_closed(combos):
    result = button(snapshot(combos))
    assert not result['available'] and result['combo'] is None


@pytest.mark.parametrize('conflicts', [['unexpected'], [None], 'RShift+Y'])
def test_unverifiable_conflict_metadata_fails_closed(conflicts):
    data = snapshot(['RShift+Y'])
    data['actions'][0]['conflicts'] = conflicts
    result = button(data)
    assert not result['available'] and result['combo'] is None


def test_stale_snapshot_and_unverified_command_remain_unavailable():
    data = snapshot(['RShift+Y', 'LCtrl+LShift+Y'], ['LCtrl+LShift+Y'])
    data['current_fingerprint'] = 'changed'
    assert not button(data)['available']
    data['current_fingerprint'] = 'current'
    data['actions'][0]['signature'] = '{}'
    assert not button(data)['available']
