"""Closed catalogue of installed, verified momentary cockpit buttons."""
import json

HORNET = 'FA-18C_hornet'


def definitions():
    rows = []
    for panel, name, device in [('ampcd', 'AMPCD', 37), ('left_mdi', 'Left MDI', 35), ('right_mdi', 'Right MDI', 36)]:
        for number in range(1, 21):
            rows.append((f'hornet.{panel}.pb.{number}', f'{name} PB {number}', panel, str(number), device, 3010+number))
    for digit in range(10):
        rows.append((f'hornet.ufc.digit.{digit}', f'UFC Keyboard Pushbutton - {digit}', 'ufc', str(digit), 25, 3018+digit))
    for key, command in [('CLR', 3028), ('ENT', 3029)]:
        rows.append((f'hornet.ufc.{key.lower()}', f'UFC Keyboard Pushbutton - {key}', 'ufc', key, 25, command))
    for number in range(1, 6):
        rows.append((f'hornet.ufc.option.{number}', f'UFC Option Select Pushbutton {number}', 'ufc', f'Option {number}', 25, 3009+number))
    for key, slug, command in [('A/P', 'ap', 3001), ('IFF', 'iff', 3002), ('TCN', 'tcn', 3003), ('ILS', 'ils', 3004), ('D/L', 'dl', 3005), ('BCN', 'bcn', 3006), ('ON/OFF', 'on_off', 3007)]:
        rows.append((f'hornet.ufc.function.{slug}', f'UFC Function Selector Pushbutton - {key}', 'ufc', key, 25, command))
    return rows


def build_catalog(snapshot, safe_actions=()):
    aircraft = snapshot.get('aircraft')
    fingerprint = snapshot.get('source_fingerprint')
    current = bool(fingerprint and fingerprint == snapshot.get('current_fingerprint') and snapshot.get('status') == 'Current')
    result = []
    if aircraft == 'F-22A':
        for action in safe_actions:
            if action.get('id') not in ('f22.nav.next', 'f22.nav.previous', 'f22.nav.mode'):
                continue
            result.append({**action, 'panel': 'navigation', 'label': action['name'], 'available': current and not action.get('conflicts'),
                           'reason': None if current else 'Current binding sources are required.', 'bindings_fingerprint': fingerprint})
        return result
    if aircraft != HORNET:
        return []
    from input_sender import KeySender, _scan
    keyboard = [row for row in snapshot.get('actions', []) if row.get('device_id') == 'keyboard' and row.get('kind') == 'key']

    def send_key(combo):
        if not isinstance(combo, str) or not combo:
            return None
        scans = [_scan(part) for part in combo.split('+')]
        if any(code is None for code, _ in scans):
            return None
        # Modifier order and letter case do not make a different physical key.
        return tuple(sorted(scans[:-1])), scans[-1]

    owners = {}
    for index, row in enumerate(keyboard):
        combos = row.get('combos', [])
        for candidate in combos if isinstance(combos, list) else []:
            key = send_key(candidate)
            if key is not None:
                owners.setdefault(key, set()).add(index)
    for ident, name, panel, label, device, command in definitions():
        matches = [row for row in keyboard if row.get('name') == name]
        reason, combo = None, None
        if not current:
            reason = 'Current installed binding sources are required.'
        elif len(matches) != 1:
            reason = 'Exact installed keyboard action is absent or ambiguous.'
        else:
            row = matches[0]
            expected = dict(cockpit_device_id=device, down=command, up=command, value_down=1, value_up=0)
            try:
                signature = json.loads(row.get('signature', 'null'))
            except (ValueError, TypeError):
                signature = None
            command_id = f'd{command}pnilu{command}cd{device}vd1vpnilvu0'
            if row.get('verified_default') is not True or signature != expected or row.get('command_id') not in (None, command_id):
                reason = 'Installed momentary button command identity is unverified.'
            elif not row.get('combos'):
                reason = 'No keyboard binding in this profile.'
            elif not isinstance(row['combos'], list):
                reason = 'Installed keyboard binding information is unverified.'
            else:
                conflicts = row.get('conflicts', [])
                if not isinstance(conflicts, list) or any(not isinstance(c, str) or c not in row['combos'] for c in conflicts):
                    reason = 'Keyboard binding conflict information is unverified.'
                else:
                    conflicted = {send_key(c) for c in conflicts}
                    candidates = [(c, send_key(c)) for c in row['combos']]
                    combo = next((c for c, key in candidates if key is not None and key not in conflicted and len(owners.get(key, ())) == 1), None)
                    if combo is None and any(key is not None and (key in conflicted or len(owners.get(key, ())) != 1) for _, key in candidates):
                        reason = 'Conflicting keyboard binding; resolve it in DCS first.'
                if not combo or KeySender.classify(name)[0] != 'ok':
                    reason = reason or 'Existing guarded sender cannot safely resolve this control.'
        result.append({'id': ident, 'name': name, 'panel': panel, 'label': label, 'aircraft': aircraft,
                       'combo': combo, 'available': reason is None, 'reason': reason, 'risk': 'benign',
                       'observable': False, 'allowlisted': reason is None, 'conflicts': [],
                       'bindings_fingerprint': fingerprint,
                       'note': 'Momentary cockpit button. Its effect depends on the current page; verify in DCS.'})
    return result
