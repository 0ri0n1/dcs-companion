"""Bounded declarative workflows; never Python, arbitrary keys, or client Lua."""
import json
import math
import re
from pathlib import Path
from .bindings import atomic_json


def validate_workflow(value, action_ids, script_ids=()):
    if not isinstance(value, dict) or set(value) != {'id', 'name', 'aircraft', 'steps'}:
        raise ValueError('Workflow requires exactly id, name, aircraft and steps.')
    if not isinstance(value['id'], str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,47}', value['id']):
        raise ValueError('Workflow ID must be 1–48 lowercase letters, digits, underscores or hyphens.')
    if not isinstance(value['name'], str) or not 1 <= len(value['name'].strip()) <= 80:
        raise ValueError('Workflow name must be 1–80 characters.')
    if value['aircraft'] not in ('F-22A', 'FA-18C_hornet'):
        raise ValueError('Workflow aircraft is unsupported.')
    steps = value['steps']
    if not isinstance(steps, list) or not 1 <= len(steps) <= 32:
        raise ValueError('Use 1–32 workflow steps.')
    budget = 0
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError('Every step must be an object.')
        kind = step.get('type')
        if kind == 'action' and set(step) == {'type', 'action_id'} and isinstance(step['action_id'], str) and step['action_id'] in action_ids:
            budget += .5
        elif kind == 'mission_script' and set(step) == {'type', 'script_id'} and isinstance(step['script_id'], str) and step['script_id'] in script_ids:
            budget += 15
        elif kind == 'delay' and set(step) == {'type', 'seconds'} and _number(step['seconds'], .1, 5):
            budget += step['seconds']
        elif kind == 'wait' and set(step) == {'type', 'display_id', 'name', 'equals', 'timeout_s'}:
            if (step['display_id'] not in tuple('1234567') or not isinstance(step['name'], str)
                    or not 1 <= len(step['name']) <= 160 or not isinstance(step['equals'], str)
                    or len(step['equals']) > 200 or not _number(step['timeout_s'], 1, 15)):
                raise ValueError('Wait requires a known display, exact text name/value and 1–15 second timeout.')
            budget += step['timeout_s']
        else:
            raise ValueError('Unknown, unsupported or malformed workflow step.')
    if budget > 90 or len(json.dumps(value, allow_nan=False)) > 16000:
        raise ValueError('Workflow exceeds its 90-second or size limit.')
    return json.loads(json.dumps(value, allow_nan=False))


def _number(value, low, high):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and low <= value <= high


class WorkflowStore:
    def __init__(self, path):
        self.path = Path(path)

    def all(self):
        if not self.path.exists():
            return []
        if self.path.stat().st_size > 256000:
            raise ValueError('Workflow file exceeds its limit.')
        data = json.loads(self.path.read_text(encoding='utf-8'))
        if not isinstance(data, list) or len(data) > 24:
            raise ValueError('Workflow file must contain at most 24 definitions.')
        if any(not isinstance(x, dict) or not isinstance(x.get('id'), str) for x in data) or len({x['id'] for x in data}) != len(data):
            raise ValueError('Workflow IDs must be unique.')
        from .remote_catalog import definitions
        hornet_ids = {row[0] for row in definitions()}
        f22_ids = {'f22.nav.next', 'f22.nav.previous', 'f22.nav.mode'}
        validated = []
        for value in data:
            steps = value.get('steps')
            if not isinstance(steps, list):
                raise ValueError('Saved workflow steps must be a bounded list.')
            script_ids = {step['script_id'] for step in steps if isinstance(step, dict)
                          and isinstance(step.get('script_id'), str)
                          and re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', step['script_id'])}
            validated.append(validate_workflow(value, hornet_ids if value.get('aircraft') == 'FA-18C_hornet' else f22_ids, script_ids))
        return validated

    def save(self, value):
        rows = [row for row in self.all() if row['id'] != value['id']] + [value]
        if len(rows) > 24 or len(json.dumps(rows)) > 256000:
            raise ValueError('At most 24 bounded workflows may be saved.')
        atomic_json(self.path, rows)

    def delete(self, ident):
        atomic_json(self.path, [row for row in self.all() if row['id'] != ident])
