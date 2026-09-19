"""Explicit, session-scoped remote controls and durable, non-replaying workflows."""
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from .bindings import atomic_json
from .command_queue import CommandQueue, IDENTITY
from .remote_catalog import build_catalog
from .sender import GuardedSender
from .workflows import WorkflowStore, validate_workflow


class RemoteError(ValueError):
    pass


def _copy(value):
    return json.loads(json.dumps(value, allow_nan=False))


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _uuid(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise RemoteError('A canonical UUID is required.')
    return value


class InputCoordinator:
    """Nonblocking lease plus physical-send mutex shared with the old guide.

    An old guide send fails closed while a macro owns the lease. It never waits
    behind a macro and then sends using validation performed before that wait.
    """
    def __init__(self):
        self.lock = threading.Lock()
        self.emit_lock = threading.Lock()
        self.owner = None

    def reserve(self, owner):
        with self.lock:
            if self.owner is not None or self.emit_lock.locked():
                return False
            self.owner = owner
            return True

    def release(self, owner):
        with self.lock:
            if self.owner == owner:
                self.owner = None

    def invoke(self, owner, function):
        with self.lock:
            if self.owner != owner or not self.emit_lock.acquire(blocking=False):
                return {'ok': False, 'sent': False, 'message': 'Another cockpit operation owns the input channel.'}
        try:
            return function()
        finally:
            self.emit_lock.release()

    def guide(self, sender):
        return lambda action, request: self.invoke(None, lambda: sender(action, request))


class RemoteService:
    HEARTBEAT_S = 8
    ARM_S = 4 * 3600
    RUN_S = 90

    def __init__(self, runtime_dir, context_provider, catalog_provider, cockpit_provider,
                 *, coordinator=None, guide_busy=None, sender=None, clock=time.time,
                 script_provider=None, script_prepare=None, script_execute=None):
        self.directory = Path(runtime_dir)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.context_provider, self.catalog_provider, self.cockpit_provider = context_provider, catalog_provider, cockpit_provider
        self.coordinator = coordinator or InputCoordinator()
        self.guide_busy = guide_busy or (lambda: False)
        self.clock, self.injected_sender = clock, sender
        self.script_provider = script_provider or (lambda: [])
        self.script_prepare, self.script_execute = script_prepare, script_execute
        self.workflows = WorkflowStore(self.directory / 'remote-workflows.json')
        self.lock = threading.RLock()
        self.controllers = {}
        self.arm = None
        self.reason = 'Preview mode. Connect controls, then enable live on this screen when ready.'
        self.active = None
        self.prepared_scripts = {}
        self.closed = False
        self.inflight = False
        self.idle = threading.Event()
        self.idle.set()
        self.next_send_at = 0
        self.db = sqlite3.connect(self.directory / 'remote.sqlite3', check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS remote_runs (id TEXT PRIMARY KEY, intent TEXT NOT NULL, payload TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 0)')
        self.db.execute('CREATE UNIQUE INDEX IF NOT EXISTS remote_one_active ON remote_runs(active) WHERE active=1')
        for (payload,) in self.db.execute('SELECT payload FROM remote_runs WHERE active=1').fetchall():
            run = json.loads(payload)
            run.update(status='Unconfirmed' if run.get('attempted') else 'Cancelled', message='Backend restarted. Consumed requests are never replayed.', finished_at=self.clock())
            self.db.execute('UPDATE remote_runs SET payload=?,active=0 WHERE id=?', (json.dumps(run), run['id']))
        self.db.commit()
        self.senders = {mode: GuardedSender(self.directory / 'remote-allowlist.json', self.directory / 'remote-input-audit.log', dry_run=mode == 'preview', ensure_release=True) for mode in ('preview', 'live')}

    @staticmethod
    def _identity(ctx):
        return {key: ctx.get(key) for key in IDENTITY}

    def _context_reason(self, ctx):
        if ctx.get('setup_matches_process') is not True:
            return 'The selected installation and profile must match the running DCS process.'
        dummy = {'name': 'Cockpit Pushbutton', 'risk': 'benign', 'combo': 'allowed', 'aircraft': ctx.get('aircraft'), 'allowlisted': True}
        return CommandQueue.validation(dummy, ctx)

    def _catalog(self, aircraft):
        snapshot, safe = self.catalog_provider(aircraft)
        return build_catalog(snapshot, safe)

    def _fresh_controller(self, owner):
        return owner in self.controllers and 0 <= self.clock() - self.controllers[owner]['at'] < self.HEARTBEAT_S

    def _armed_controller(self, owner):
        return bool(self.arm and self._fresh_controller(owner)
                    and self.arm['controllers'].get(owner) == self.controllers[owner]['connection_id'])

    def _save(self, run, active=None):
        self.db.execute('UPDATE remote_runs SET payload=?,active=? WHERE id=?',
                        (json.dumps(run, allow_nan=False), int(self.active is run if active is None else active), run['id']))
        self.db.commit()

    def _finish(self, status, message):
        run = self.active
        if run:
            run.update(status=status, message=message, finished_at=self.clock())
            self._save(run, False)
            self.coordinator.release(run['id'])
            self.active = None
            self.prepared_scripts = {}

    def _disarm(self, reason):
        self.arm, self.reason = None, reason
        if self.active and self.active['mode'] == 'live':
            self._finish('Unconfirmed' if self.active.get('attempted') else 'Cancelled', reason + ' No further steps will run; an in-flight send cannot be recalled.')

    def _audit_state(self, ctx):
        if self.arm:
            reason = self._context_reason(ctx)
            if self._identity(ctx) != self.arm['context'] or reason:
                self._disarm(reason or 'Aircraft, session, process or binding generation changed.')
            elif self.clock() >= self.arm['expires_at']:
                self._disarm('Live enable expired. Enable it again on the screen you want to use.')
            elif any(not self._fresh_controller(owner) or self.controllers[owner]['connection_id'] != generation for owner, generation in self.arm['controllers'].items()):
                self._disarm('The live controller expired or reconnected. Connect controls and enable live again on that screen.')
        if self.active and not self._fresh_controller(self.active['owner']):
            self._finish('Unconfirmed' if self.active.get('attempted') else 'Cancelled', 'Controller heartbeat expired. No further steps; no automatic retry.')

    def heartbeat(self, owner, connection_id):
        _uuid(connection_id)
        with self.lock:
            previous = self.controllers.get(owner)
            if self.arm and owner in self.arm['controllers'] and (not previous or previous['connection_id'] != connection_id or not self._fresh_controller(owner)):
                self._disarm('The live controller reconnected. Enable live again on this screen.')
            if previous and (previous['connection_id'] != connection_id or not self._fresh_controller(owner)) and self.active and self.active['owner'] == owner:
                self._finish('Unconfirmed' if self.active.get('attempted') else 'Cancelled', 'Controller reconnected; the previous run was not replayed.')
            if owner not in self.controllers and len(self.controllers) >= 64:
                self.controllers = {key: value for key, value in self.controllers.items() if self._fresh_controller(key)}
                if len(self.controllers) >= 64:
                    raise RemoteError('Too many connected controllers.')
            self.controllers[owner] = {'connection_id': connection_id, 'at': self.clock()}
            return self.status(owner, include_catalog=False)

    def disconnect(self, owner, connection_id):
        """Disconnect this exact screen generation without stopping another owner."""
        _uuid(connection_id)
        with self.lock:
            previous = self.controllers.get(owner)
            if previous and previous['connection_id'] == connection_id:
                if self.arm and owner in self.arm['controllers']:
                    self._disarm('Live controls disconnected. Connect and enable live again to resume.')
                if self.active and self.active['owner'] == owner:
                    self._finish('Unconfirmed' if self.active.get('attempted') else 'Cancelled',
                                 'Controller disconnected. No further steps; no automatic retry.')
                del self.controllers[owner]
            return self.status(owner, include_catalog=False)

    def enable(self, enabled, owner):
        with self.lock:
            if not enabled:
                if self.arm and owner in self.arm['controllers']:
                    self._disarm('Live controls disabled on this screen.')
            else:
                ctx = self.context_provider()
                reason = self._context_reason(ctx)
                if reason or not self._fresh_controller(owner) or self.active or self.inflight:
                    raise RemoteError(reason or 'Connect controls and finish or stop the current run first.')
                self.arm = {'context': self._identity(ctx), 'expires_at': self.clock()+self.ARM_S,
                            'controllers': {owner: self.controllers[owner]['connection_id']}}
                self.reason = 'Live momentary controls enabled for this screen. DCS must be focused for every send.'
            return self.status(owner, include_catalog=False)

    def status(self, owner=None, include_catalog=True, ctx=None):
        # Read-only status must not hold the macro state lock while filesystem
        # binding checks wait behind another reader. Execution methods retain
        # their own fresh context/catalog checks under the existing guards.
        ctx = ctx if ctx is not None else self.context_provider()
        rows = self._catalog(ctx.get('aircraft')) if include_catalog else []
        with self.lock:
            self._audit_state(ctx)
            labels = {'ufc': 'UFC', 'ampcd': 'AMPCD', 'left_mdi': 'Left DDI', 'right_mdi': 'Right DDI', 'navigation': 'Navigation'}
            last = self.db.execute('SELECT payload FROM remote_runs ORDER BY rowid DESC LIMIT 1').fetchone()
            armed = bool(self.arm) if owner is None else self._armed_controller(owner)
            reason = ('Preview mode on this screen. Connect controls and enable live here to use its buttons.'
                      if self.arm and not armed else self.reason)
            result = {'status': 'Running' if self.active else 'Ready', 'mode': 'live' if armed else 'preview', 'armed': armed,
                      'arm_expires_at': self.arm['expires_at'] if armed else None, 'heartbeat_timeout_s': self.HEARTBEAT_S,
                      'controller_connected': self._fresh_controller(owner), 'busy': bool(self.active or self.inflight),
                      'active_run': self._public(self.active), 'last_run': self._public(json.loads(last[0])) if last else None,
                      'aircraft': ctx.get('aircraft'), 'reason': reason,
                      'panels': [{'id': panel, 'label': label, 'buttons': [row for row in rows if row['panel'] == panel]} for panel, label in labels.items() if any(row['panel'] == panel for row in rows)],
                      'workflows': [], 'scripts': [], 'notes': ['Button sends are not confirmation of aircraft effect.', 'Stop prevents future steps; input or Lua already handed off cannot be recalled.']}
            if include_catalog:
                try:
                    result['workflows'] = self.workflows.all()
                except (OSError, ValueError) as exc:
                    result['workflow_error'] = str(exc)
                result['scripts'] = self.script_provider()
            return result

    @staticmethod
    def _public(run):
        if not run:
            return None
        return {key: _copy(value) for key, value in run.items() if key not in ('owner', 'context', 'steps', 'actions', 'script_revisions', 'connection_id', 'step_started_at', 'settle_model', 'settle_at')}

    def save_workflow(self, value):
        with self.lock:
            actions = {a['id'] for a in self._catalog(value.get('aircraft'))}
            scripts = {s['id'] for s in self.script_provider()}
            validated = validate_workflow(value, actions, scripts)
            if self.active:
                raise RemoteError('Stop the current run before editing workflows.')
            self.workflows.save(validated)
            return validated

    def delete_workflow(self, ident):
        with self.lock:
            if self.active:
                raise RemoteError('Stop the current run before editing workflows.')
            self.workflows.delete(ident)
            return {'deleted': ident}

    def start(self, request_id, owner, *, action_id=None, workflow_id=None, script_id=None):
        _uuid(request_id)
        if sum(value is not None for value in (action_id, workflow_id, script_id)) != 1:
            raise RemoteError('Select exactly one action, workflow or registered script.')
        intent = json.dumps([owner, action_id, workflow_id, script_id])
        with self.lock:
            old = self.db.execute('SELECT intent,payload FROM remote_runs WHERE id=?', (request_id,)).fetchone()
            if old:
                if old[0] != intent:
                    raise RemoteError('Idempotency key already belongs to another request.')
                return self._public(json.loads(old[1]))
            if self.closed or self.active or self.inflight or self.guide_busy():
                raise RemoteError('Another cockpit operation is active; this request was not queued.')
            ctx = self.context_provider()
            observed_at = self.clock()
            self._audit_state(ctx)
            if not self._fresh_controller(owner):
                raise RemoteError('Connect controls before running an operation.')
            reason = self._context_reason(ctx)
            if reason:
                raise RemoteError(reason)
            actions = {a['id']: a for a in self._catalog(ctx['aircraft'])}
            if workflow_id:
                matches = [w for w in self.workflows.all() if w.get('id') == workflow_id]
                if len(matches) != 1:
                    raise RemoteError('Saved workflow not found.')
                workflow = validate_workflow(matches[0], set(actions), {s['id'] for s in self.script_provider()})
                if workflow['aircraft'] != ctx['aircraft']:
                    raise RemoteError('Workflow belongs to another aircraft.')
                steps, name = workflow['steps'], workflow['name']
            else:
                steps = [{'type': 'action', 'action_id': action_id}] if action_id else [{'type': 'mission_script', 'script_id': script_id}]
                name = action_id or script_id
            prepared = {}
            selected = {}
            for index, step in enumerate(steps):
                if step['type'] == 'action':
                    action = actions.get(step['action_id'])
                    if not action or not action.get('available') or action.get('bindings_fingerprint') != ctx['bindings_hash']:
                        raise RemoteError((action or {}).get('reason') or 'Control is not in the aircraft allowlist.')
                    selected[step['action_id']] = action
                elif step['type'] == 'mission_script':
                    if not self.script_prepare or not self.script_execute:
                        raise RemoteError('Registered script execution is unavailable.')
                    item = self.script_prepare(step['script_id'])
                    allowed = item.get('aircraft', [])
                    if allowed and '*' not in allowed and ctx['aircraft'] not in allowed:
                        raise RemoteError('Registered script belongs to another aircraft.')
                    prepared[index] = _copy(item)
            if not self.coordinator.reserve(request_id):
                raise RemoteError('Input channel is busy; request was not queued.')
            run = {'id': request_id, 'request_id': request_id, 'name': name, 'owner': owner, 'context': self._identity(ctx),
                   'connection_id': self.controllers[owner]['connection_id'], 'mode': 'live' if self._armed_controller(owner) else 'preview',
                   'status': 'Queued', 'created_at': self.clock(), 'expires_at': self.clock()+(3 if action_id else self.RUN_S),
                   'step_index': 0, 'step_count': len(steps), 'steps': steps, 'actions': selected,
                   'script_revisions': {str(k): _digest(v) for k, v in prepared.items()}, 'attempted': 0,
                   'events': [], 'message': 'Accepted once; no automatic retries.'}
            # The acceptance check already observed fresh focus and model time.
            # Count its settling window now, rather than adding a whole worker
            # cycle before taking the same first sample. The worker must still
            # establish current focus, advancement and every execution guard.
            if action_id and run['mode'] == 'live' and ctx.get('focused'):
                run.update(settle_at=observed_at+.25, settle_model=ctx['model_time'])
            try:
                self.db.execute('INSERT INTO remote_runs VALUES (?,?,?,1)', (request_id, intent, json.dumps(run, allow_nan=False)))
                self.db.commit()
            except Exception:
                self.coordinator.release(request_id)
                raise
            self.active, self.prepared_scripts = run, prepared
            return self._public(run)

    def stop(self):
        with self.lock:
            self._finish('Cancelled', 'Stopped. No further steps; already handed-off input or Lua cannot be recalled.')
            self.arm = None
            self.reason = 'Stopped and disarmed.'
            return {'stopped': True, 'inflight': self.inflight, 'message': self.reason}

    def _guard(self, run, ctx, focused=False):
        reason = self._context_reason(ctx)
        if reason:
            return reason
        if self._identity(ctx) != run['context']:
            return 'Aircraft, mission, process, session or binding generation changed.'
        if not self._fresh_controller(run['owner']) or self.controllers[run['owner']]['connection_id'] != run['connection_id']:
            return 'Controller disconnected or reconnected.'
        if self.clock() >= run['expires_at']:
            return 'Operation expired.'
        if run['mode'] == 'live' and (not self._armed_controller(run['owner']) or self.clock() >= self.arm['expires_at']):
            return 'Live enable expired or was disabled.'
        if focused and not ctx.get('focused'):
            return 'DCS is not the foreground window. No input sent.'
        return None

    def worker_interval(self):
        """Scheduling hint only: no I/O, lock waits or cached authorization."""
        run = self.active
        if run is None:
            return .15
        index, steps = run.get('step_index', 0), run.get('steps', ())
        if index >= len(steps):
            return .025
        step = steps[index]
        if run['mode'] == 'live' and step['type'] == 'wait':
            return .15
        deadline = 0
        if step['type'] in ('action', 'mission_script'):
            deadline = self.next_send_at
            if run['mode'] == 'live' and not run.get('focus_settled'):
                deadline = max(deadline, run.get('settle_at', 0))
        elif run['mode'] == 'live' and step['type'] == 'delay' and 'step_started_at' in run:
            deadline = run['step_started_at'] + step['seconds']
        # Do not repeatedly hash context while a known wait is outstanding.
        # The ordinary 150 ms audit cadence remains the longest scheduled wait.
        return min(.15, max(.025, deadline - self.clock()))

    def tick(self):
        with self.lock:
            if self.closed or self.inflight or not (self.active or self.arm):
                return
            ctx = self.context_provider()
            self._audit_state(ctx)
            run = self.active
            if not run:
                return
            reason = self._guard(run, ctx)
            if reason:
                self._finish('Unconfirmed' if run['attempted'] else 'Rejected', reason)
                return
            run['status'] = 'Running'
            if run['step_index'] >= len(run['steps']):
                self._finish('Completed' if run['mode'] == 'preview' else 'Unconfirmed', 'Preview complete. No inputs sent.' if run['mode'] == 'preview' else 'Steps completed; cockpit effects are not confirmed.')
                return
            step = run['steps'][run['step_index']]
            run.setdefault('step_started_at', self.clock())
            if step['type'] in ('delay', 'wait'):
                if run['mode'] == 'preview':
                    self._advance(run, 'Preview: ' + step['type'] + ' validated; no observation or input claimed.')
                elif step['type'] == 'delay' and self.clock() >= run['step_started_at'] + step['seconds']:
                    self._advance(run, 'Delay completed.')
                elif step['type'] == 'wait':
                    cockpit = self.cockpit_provider()
                    display = next((d for d in cockpit.get('displays', []) if d.get('id') == step['display_id']), {})
                    matches = [e for e in display.get('elements', []) if e.get('name') == step['name']]
                    received = cockpit.get('received_epoch_by_id', {}).get(step['display_id'])
                    if (cockpit.get('aircraft') == ctx['aircraft'] and cockpit.get('remote_session') == run['context']['session']
                            and isinstance(received, (int, float)) and not isinstance(received, bool)
                            and run['step_started_at'] < received <= self.clock()+.05
                            and 0 <= self.clock()-received <= 3 and display.get('status') == 'Available'
                            and len(matches) == 1 and matches[0].get('value') == step['equals']):
                        self._advance(run, 'Exact fresh display condition observed; causation is not established.')
                    elif self.clock() >= run['step_started_at'] + step['timeout_s']:
                        self._finish('Unconfirmed', 'Display condition timed out. No further actions and no retry.')
                return
            if self.clock() < self.next_send_at:
                return
            if run['mode'] == 'live':
                if not ctx.get('focused'):
                    self._finish('Rejected' if not run['attempted'] else 'Unconfirmed', 'Focus DCS, then submit a new explicit request. This one will not retry.')
                    return
                if not run.get('focus_settled') and 'settle_at' not in run:
                    run.update(settle_at=self.clock()+.25, settle_model=ctx['model_time'])
                    self._save(run)
                    return
                if not run.get('focus_settled') and self.clock() < run['settle_at']:
                    return
                if not run.get('focus_settled') and ctx['model_time'] <= run['settle_model']:
                    self._finish('Rejected' if not run['attempted'] else 'Unconfirmed', 'Model time did not advance while focus settled.')
                    return
                run['focus_settled'] = True
            action, prepared = None, None
            if step['type'] == 'action':
                action = next((a for a in self._catalog(ctx['aircraft']) if a['id'] == step['action_id']), None)
                if not action or not action.get('available') or action.get('bindings_fingerprint') != ctx['bindings_hash'] or action != run['actions'][step['action_id']]:
                    self._finish('Rejected' if not run['attempted'] else 'Unconfirmed', 'Current binding action changed or became unavailable.')
                    return
                atomic_json(self.directory / 'remote-allowlist.json', {'aircraft': ctx['aircraft'], 'source_fingerprint': ctx['bindings_hash'],
                            'actions': [{'name': action['name'], 'combos': [action['combo']]}], 'by_combo': {action['combo']: [action['name']]}})
            else:
                try:
                    prepared = self.script_prepare(step['script_id'])
                    if _digest(prepared) != run['script_revisions'][str(run['step_index'])]:
                        raise RemoteError('Registered script revision changed.')
                except Exception as exc:
                    self._finish('Rejected', str(exc))
                    return
            # Full context/binding SHA check after resolving the isolated index.
            final_ctx = self.context_provider()
            reason = self._guard(run, final_ctx, focused=run['mode'] == 'live')
            if reason:
                self._finish('Rejected' if not run['attempted'] else 'Unconfirmed', reason)
                return
            run['attempted'] += 1
            run['events'].append({'step': run['step_index'], 'status': 'Attempted', 'at': self.clock()})
            self._save(run)  # Durable before handing anything to a sender.
            self.inflight = True
            self.idle.clear()
            owner = run['id']
            request = {'request_id': str(uuid.uuid5(uuid.UUID(owner), f"step:{run['step_index']}"))}
            mode = run['mode']
        try:
            def dispatch():
                # Stop/disarm can race after the durable claim: never send in that case.
                with self.lock:
                    if self.active is not run:
                        return {'ok': False, 'sent': False, 'message': 'Stopped before handoff.'}
                    reason = self._guard(run, self.context_provider(), focused=mode == 'live')
                    if reason:
                        return {'ok': False, 'sent': False, 'message': reason}
                if action:
                    sender = self.injected_sender or self.senders[mode]
                    return sender(action, {**request, 'dry_run': mode == 'preview'})
                def authorized():
                    with self.lock:
                        current = self.context_provider()
                        self._audit_state(current)
                        return self.active is run and self._guard(run, current, focused=mode == 'live') is None
                # Private callable is handed only to the registered executor.
                # Never serialize it in the ledger, snapshot or public events.
                script_ctx = {**final_ctx, '_remote_authorized': authorized}
                return self.script_execute(prepared, script_ctx, request['request_id'], dry_run=mode == 'preview')
            result = self.coordinator.invoke(owner, dispatch)
        except Exception as exc:
            result = {'ok': False, 'sent': False, 'ambiguous': True, 'message': f'Execution error ({type(exc).__name__}); no automatic retry.'}
        with self.lock:
            self.inflight = False
            self.next_send_at = self.clock()+.25
            try:
                # Preserve Stop/disarm outcome while recording the actual handoff result.
                run['events'][-1].update(status='Preview' if result.get('dry_run') else 'Sent' if result.get('sent') else 'Unconfirmed' if result.get('ambiguous') else 'Rejected', message=str(result.get('message', ''))[:1000])
                if self.active is not run:
                    self._save(run, False)
                    return
                if result.get('ok') and ((mode == 'preview' and result.get('dry_run') and not result.get('sent')) or (mode == 'live' and result.get('sent'))):
                    self._advance(run, result.get('message', 'Step handed off.'), finalize=True)
                else:
                    if mode == 'live' and (result.get('ambiguous') or result.get('sent')):
                        self.arm = None
                        self.reason = 'Execution was ambiguous. Live controls are disabled; inspect the cockpit before enabling again.'
                    self._finish('Unconfirmed' if result.get('ambiguous') or result.get('sent') else 'Rejected', result.get('message', 'Sender refused. No automatic retry.'))
            finally:
                self.idle.set()

    def _advance(self, run, message, *, finalize=False):
        run['step_index'] += 1
        run['message'] = str(message)[:1000]
        for key in ('step_started_at', 'settle_at', 'settle_model'):
            run.pop(key, None)
        if finalize and run['step_index'] >= len(run['steps']):
            # The bounded sender has returned after release and its receipt is
            # recorded above. No further tick or cockpit observation is needed
            # to release the run slot; actual cockpit effects remain unconfirmed.
            self._finish('Completed' if run['mode'] == 'preview' else 'Unconfirmed',
                         'Preview complete. No inputs sent.' if run['mode'] == 'preview'
                         else 'Steps completed; cockpit effects are not confirmed.')
        else:
            self._save(run)

    def close(self):
        with self.lock:
            self.closed = True
            self.stop()
        # asyncio cancelling to_thread does not stop its underlying OS thread.
        # Never close SQLite while a handed-off sender can still write its result.
        self.idle.wait()
        with self.lock:
            self.db.close()
