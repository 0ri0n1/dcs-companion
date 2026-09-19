"""One demand-driven display producer shared by all paired browser streams.

HTTP readers never call native capture. The producer captures approved regions
sequentially, keeps only the latest frame per panel, and encodes each NDJSON
frame once. Context/file/native work never holds the service state lock.
"""
import base64
import copy
import hashlib
import json
import math
import threading
import time
import uuid

PANELS = {'left_mdi': 'Left DDI', 'right_mdi': 'Right DDI', 'ampcd': 'AMPCD'}


class FeedError(ValueError):
    def __init__(self, message, status_code=503):
        super().__init__(message)
        self.status_code = status_code


class DisplayFeedService:
    TARGET_FPS = 15
    INTERVAL = 1 / TARGET_FPS
    MAX_FRAME_AGE = 1.0
    MAX_GATE_AGE = 1.0
    LAYOUT_PROOF_MAX_AGE = .250
    MAX_BYTES = 2 * 1024 * 1024
    MAX_PACKET_BYTES = 3 * 1024 * 1024
    MAX_SUBSCRIBERS = 8
    SUBSCRIBER_LEASE = 3.0
    LEGACY_LEASE = .75

    def __init__(self, setup, context_provider, *, capture=None, clock=time.time, monotonic=time.monotonic):
        from .display_capture import capture_jpeg
        self.setup, self.context_provider = setup, context_provider
        self.capture = capture or capture_jpeg
        self.clock, self.monotonic = clock, monotonic
        self.enabled = False
        self.closed = False
        self.lock = threading.RLock()
        self.configuration_lock = threading.Lock()
        self.layout_proof_lock = threading.Lock()
        self._layout_proof = None
        self._layout_generation = 0
        self.frames = {}
        self.last_error = None
        self.last_context_error = None
        self.review = None
        self.epoch = 0
        # Sequence numbers restart with this service, including backend restarts.
        # Give that new sequence domain a distinct revision even if DCS/layout
        # and the enable epoch are otherwise unchanged.
        self._instance_id = uuid.uuid4().hex
        self.sequences = {panel: 0 for panel in PANELS}
        self.subscribers = {}
        self.legacy_until = 0.0
        self.producer = None
        self.wakeup = threading.Event()
        self.gate = {'status': 'Disabled', 'detail': 'Enable display capture on the DCS computer.',
                     'revision': '', 'aircraft': None, 'checked_mono': 0.0, 'started_mono': 0.0,
                     'epoch': 0, 'plan': None, 'context': {}}

    @classmethod
    def packet(cls, value):
        encoded = json.dumps(value, separators=(',', ':'), allow_nan=False).encode('utf-8') + b'\n'
        if len(encoded) > cls.MAX_PACKET_BYTES:
            raise FeedError('Display stream packet exceeds its size limit.')
        return encoded

    @staticmethod
    def _context_evidence(ctx):
        """Bounded diagnosis only: never publish arbitrary source values/text."""
        def number(value):
            return value if (type(value) in (int, float) and abs(value) <= 1e16
                             and math.isfinite(value)) else None
        def fingerprint(value):
            return hashlib.sha256(value.encode()).hexdigest()[:16] if isinstance(value, str) else None
        evidence = ctx.get('telemetry_evidence')
        evidence = evidence if isinstance(evidence, dict) else {}
        result = {key: fingerprint(ctx.get(key)) for key in ('process', 'session')}
        result.update({key: ctx.get(key) is True for key in
                       ('telemetry_valid', 'model_advancing', 'focused', 'setup_matches_process')})
        result.update({key: number(ctx.get(key)) for key in ('sample_epoch', 'model_time')})
        result.update({key: number(evidence.get(key)) for key in (
            'generation', 'collector_epoch', 'written_epoch', 'hello', 'bye',
            'process_matches', 'process_disappeared', 'read_failures', 'read_attempts')})
        result['read_status'] = evidence.get('read_status') if evidence.get('read_status') in (
            'ok', 'missing', 'permission', 'io', 'invalid') else None
        result['generation_reason'] = evidence.get('generation_reason') if evidence.get('generation_reason') in (
            'model-reset', 'collector-epoch') else None
        error = evidence.get('last_read_error')
        if isinstance(error, dict):
            result['last_read_error'] = {
                'status': error.get('status') if error.get('status') in ('missing', 'permission', 'io', 'invalid') else None,
                'epoch': number(error.get('epoch')), 'winerror': number(error.get('winerror'))}
        return result

    def _layout_for_context(self, ctx, epoch, *, force_fresh=False):
        """Single-flight file/layout proof, fresh for at most 250 ms from start.

        This cache never covers telemetry, focus, command/binding authorization,
        or native per-image checks. Its exact process/session/epoch key prevents
        a proof from crossing DCS missions, restarts, or configuration changes.
        Stored and returned plans are independent copies, never caller-owned.
        Returns (proof, reread_context, freshly_validated); a waiting cache hit
        needs a context reread but is still a reused proof.
        """
        key = (epoch, ctx.get('process'), ctx.get('session'))
        acquired = self.layout_proof_lock.acquire(blocking=False)
        waited = not acquired
        if waited:
            self.layout_proof_lock.acquire()
        try:
            with self.lock:
                cached = self._layout_proof
                generation = self._layout_generation
            if (not force_fresh and cached and cached['key'] == key and
                    0 <= self.monotonic()-cached['started_mono'] <= self.LAYOUT_PROOF_MAX_AGE):
                # A caller that waited must re-read its pre-wait context too.
                return copy.deepcopy(cached), waited, False
            with self.lock:
                self._layout_proof = None
            started = self.monotonic()
            # A failed or slow refresh cannot fall back to the previous proof.
            plan = copy.deepcopy(self.setup.capture_plan())
            if not 0 <= self.monotonic()-started <= self.LAYOUT_PROOF_MAX_AGE:
                raise FeedError('The display layout proof expired during validation.')
            record = {'key': key, 'started_mono': started, 'plan': plan, 'generation': generation,
                      'context_evidence': self._context_evidence(ctx)}
            with self.lock:
                if epoch != self.epoch or generation != self._layout_generation or self.closed:
                    raise FeedError('Display configuration changed during validation.')
                self._layout_proof = record
            return copy.deepcopy(record), True, True
        finally:
            self.layout_proof_lock.release()

    def _refresh_gate(self):
        # Snapshot flags, perform potentially slow reads without our lock, then
        # commit only when the generation and ordering still match.
        with self.lock:
            epoch, enabled, closed = self.epoch, self.enabled, self.closed
        started = self.monotonic()
        plan, ctx, revision = None, {}, ''
        proof_generation = None
        context_mismatch = None
        try:
            ctx = self.context_provider()
            for attempt in range(2):
                proof, reread_context, freshly_validated = self._layout_for_context(
                    ctx, epoch, force_fresh=bool(attempt))
                proof_generation = proof['generation']
                if reread_context:
                    # File/monitor I/O or a lock wait may have taken time. A
                    # post-capture gate needs fresh focus/session after that I/O.
                    ctx = self.context_provider()
                if (epoch, ctx.get('process'), ctx.get('session')) != proof['key']:
                    context_mismatch = {
                        'changed': [name for name, previous, current in zip(
                            ('epoch', 'process', 'session'), proof['key'],
                            (epoch, ctx.get('process'), ctx.get('session'))) if previous != current],
                        'before': proof['context_evidence'], 'after': self._context_evidence(ctx)}
                    raise FeedError('DCS process or mission session changed during layout validation.')
                age = self.monotonic()-proof['started_mono']
                if 0 <= age <= self.LAYOUT_PROOF_MAX_AGE:
                    break
                if attempt or freshly_validated or age < 0:
                    raise FeedError('The display layout proof expired before its context was verified.')
                # A reused proof may cross its expiry between lookup and this
                # final check. Discard it, then validate exactly once from fresh
                # files and fresh context; never extend or accept its old age.
                with self.lock:
                    if (epoch != self.epoch or proof_generation != self._layout_generation or self.closed):
                        raise FeedError('Display configuration changed during validation.')
                    self._layout_proof = None
            plan = proof['plan']
            if plan:
                revision = hashlib.sha256(json.dumps([self._instance_id, plan['revision'], ctx.get('session'),
                                                      ctx.get('process'), epoch]).encode()).hexdigest()
            if closed or not enabled:
                status, detail = 'Disabled', 'Enable display capture on the DCS computer.'
            elif not plan:
                status, detail = 'Setup required', 'Prepare and apply a display layout on the PC, then restart DCS.'
            elif not ctx.get('process') or not ctx.get('session') or not ctx.get('aircraft'):
                status, detail = 'Waiting for DCS', 'Enter a Hornet cockpit after starting DCS with the display layout.'
            elif ctx.get('aircraft') != 'FA-18C_hornet':
                status, detail = 'Unsupported', 'These display exports are configured for the F/A-18C Hornet.'
            elif ctx.get('setup_matches_process') is not True:
                status, detail = 'Unavailable', 'The selected DCS installation and profile must match the running game.'
            elif ctx.get('telemetry_valid') is not True:
                status, detail = 'Unavailable', 'Fresh aircraft telemetry is required for display streams.'
            elif ctx.get('model_advancing') is not True:
                status, detail = 'Paused', 'The simulation is paused or its clock is not advancing.'
            elif ctx.get('focused') is not True:
                status, detail = 'Paused', 'Return focus to DCS to view its exported displays.'
            else:
                status, detail = 'Ready', 'Continuous DCS display streams are ready.'
        except Exception as exc:
            # Keep one bounded, safe cause after recovery. Only our own typed
            # messages may be exposed; arbitrary exception text can contain
            # paths, context values, or credentials and remains redacted.
            detail = (str(exc)[:240] if isinstance(exc, FeedError) else
                      'The current DCS display context could not be verified.')
            with self.lock:
                self._layout_proof = None
                self._layout_generation += 1
                self.last_error = detail
                self.last_context_error = {'occurred_epoch': self.clock(),
                                           'type': type(exc).__name__[:64], 'detail': detail}
                if context_mismatch is not None:
                    self.last_context_error['mismatch'] = context_mismatch
            status = 'Unavailable'
        gate = {'status': status, 'detail': detail, 'revision': revision, 'aircraft': ctx.get('aircraft'),
                'checked_mono': self.monotonic(), 'started_mono': started, 'epoch': epoch,
                'plan': plan, 'context': ctx}
        with self.lock:
            if epoch == self.epoch and started >= self.gate['started_mono']:
                if status == 'Ready' and proof_generation != self._layout_generation:
                    status = gate['status'] = 'Unavailable'
                    gate['detail'] = 'The display layout changed while validating its context.'
                if status != 'Ready' or revision != self.gate['revision']:
                    self.frames.clear()
                self.gate = gate
            return dict(self.gate)

    def _invalidate_locked(self, status, detail):
        self.epoch += 1
        self._layout_proof = None
        self._layout_generation += 1
        self.frames.clear()
        self.gate = {'status': status, 'detail': detail, 'revision': '', 'aircraft': None,
                     'checked_mono': self.monotonic(), 'started_mono': self.monotonic(),
                     'epoch': self.epoch, 'plan': None, 'context': {}}
        self.wakeup.set()

    def _fresh_locked(self, panel, revision):
        image = self.frames.get(panel)
        if image and image['revision'] == revision and 0 <= self.clock()-image['captured_epoch'] <= self.MAX_FRAME_AGE:
            return image
        if image:
            self.frames.pop(panel, None)
        return None

    def _status_locked(self):
        gate = self.gate
        status, detail = gate['status'], gate['detail']
        if status == 'Ready' and not 0 <= self.monotonic()-gate['checked_mono'] <= self.MAX_GATE_AGE:
            status, detail = 'Unavailable', 'The display producer is waiting for a fresh DCS context.'
            self.frames.clear()
        return {'type': 'status', 'enabled': self.enabled and not self.closed, 'status': status,
                'detail': detail, 'revision': gate['revision'], 'aircraft': gate['aircraft'],
                'panels': [{'id': panel, 'label': label, 'status': status, 'detail': detail,
                            'width': ((gate['plan'] or {}).get('panels', {}).get(panel) or {}).get('width', 512),
                            'height': ((gate['plan'] or {}).get('panels', {}).get(panel) or {}).get('height', 512),
                            'last_sequence': (image or {}).get('sequence'),
                            'last_captured_epoch': (image or {}).get('captured_epoch')}
                           for panel, label in PANELS.items()
                           for image in (self._fresh_locked(panel, gate['revision']),)]}

    def status(self):
        setup = self.setup.snapshot()
        if setup.get('status') in ('changed', 'unavailable', 'not-configured', 'pending-restart'):
            with self.lock:
                self._layout_proof = None
                self._layout_generation += 1
                self.frames.clear()
                self.gate = {**self.gate, 'status': 'Unavailable',
                             'detail': 'The configured display layout must be revalidated.'}
        self._refresh_gate()
        with self.lock:
            if self.review and setup.get('status') in ('not-configured', 'unavailable'):
                setup['plan'] = dict(self.review)
            setup['can_apply'] = bool(setup.get('can_apply') and self.review)
            status = self._status_locked()
            status.pop('type')
            if status['status'] == 'Setup required' and setup.get('detail'):
                status['detail'] = setup['detail']
            status.update(frame_interval_ms=round(self.INTERVAL*1000), target_fps=self.TARGET_FPS,
                          layout_proof_max_age_ms=round(self.LAYOUT_PROOF_MAX_AGE*1000),
                          transport='ndjson', last_error=self.last_error, setup=setup,
                          last_context_error=copy.deepcopy(self.last_context_error),
                          subscribers=len(self.subscribers))
            return status

    def enable(self, enabled):
        if type(enabled) is not bool:
            raise FeedError('Choose whether to enable capture.', 409)
        if enabled and not self.setup.capture_plan():
            raise FeedError('Apply the display layout and restart DCS before enabling capture.', 409)
        with self.lock:
            self._require_open()
            if enabled != self.enabled:
                self.enabled = enabled
                self._invalidate_locked('Waiting for DCS' if enabled else 'Disabled',
                                        'Waiting for a current display context.' if enabled else 'Display capture is disabled.')
            self.last_error = None

    def prepare(self, monitor_id):
        with self.configuration_lock:
            with self.lock:
                self._require_open()
            plan = self.setup.plan(monitor_id)
            with self.lock:
                self.review = {**plan, 'id': plan.get('id', plan.get('plan_id')),
                               'summary': plan.get('summary', plan.get('detail')),
                               'export_monitor': plan.get('export_monitor', plan.get('monitor_name'))}
                return dict(self.review)

    def _configuration_change(self, method, *args):
        with self.configuration_lock:
            with self.lock:
                self._require_open()
                self.enabled = False
                self._invalidate_locked('Disabled', 'Display configuration is changing.')
            result = getattr(self.setup, method)(*args)
            with self.lock:
                self.review = None
            return result

    def apply(self, plan_id):
        return self._configuration_change('apply', plan_id)

    def restore(self):
        return self._configuration_change('restore')

    def _ensure_producer_locked(self):
        if self.producer is None:
            self.producer = threading.Thread(target=self._produce, name='DCS-display-producer', daemon=True)
            self.producer.start()
        self.wakeup.set()

    def _demand_locked(self):
        now = self.monotonic()
        self.subscribers = {key: item for key, item in self.subscribers.items()
                            if now-item['seen'] <= self.SUBSCRIBER_LEASE}
        return bool(self.subscribers) or now < self.legacy_until

    def subscribe(self, panels, revision):
        if (not isinstance(panels, (list, tuple)) or not 1 <= len(panels) <= 3 or
                any(not isinstance(panel, str) or panel not in PANELS for panel in panels) or len(set(panels)) != len(panels)):
            raise FeedError('Choose one to three different registered cockpit displays.', 422)
        self._refresh_gate()
        with self.lock:
            self._require_open()
            status = self._status_locked()
            if status['status'] != 'Ready':
                raise FeedError(status['detail'])
            if not revision or revision != status['revision']:
                raise FeedError('The display session changed. Refresh display status.', 409)
            self._demand_locked()
            if len(self.subscribers) >= self.MAX_SUBSCRIBERS:
                raise FeedError('Eight display viewers are already connected.', 429)
            ident = uuid.uuid4().hex
            self.subscribers[ident] = {'panels': tuple(panels), 'revision': revision, 'seen': self.monotonic()}
            self._ensure_producer_locked()
            return ident

    def unsubscribe(self, ident):
        with self.lock:
            self.subscribers.pop(ident, None)
            self.wakeup.set()

    def stream_snapshot(self, ident, sequences):
        """Fast memory-only read for the async response generator; no native I/O."""
        with self.lock:
            item = self.subscribers.get(ident)
            status = self._status_locked()
            ended = bool(not item or self.closed or status['status'] != 'Ready' or
                         not status['enabled'] or status['revision'] != item['revision'])
            if ended:
                self.subscribers.pop(ident, None)
                return status, [], True
            item['seen'] = self.monotonic()
            frames = []
            for panel in item['panels']:
                image = self._fresh_locked(panel, item['revision'])
                if image and image['sequence'] > sequences.get(panel, 0):
                    frames.append((panel, image['sequence'], image['captured_epoch'], image['packet']))
            return status, frames, False

    def frame(self, panel_id, revision):
        """Legacy single-frame reader; only leases/reads the shared producer."""
        if panel_id not in PANELS:
            raise FeedError('Unknown cockpit display.', 404)
        self._refresh_gate()
        with self.lock:
            status = self._status_locked()
            if status['status'] != 'Ready':
                raise FeedError(status['detail'])
            if not revision or revision != status['revision']:
                raise FeedError('Display or aircraft session changed. Refresh display status.', 409)
            self.legacy_until = self.monotonic()+self.LEGACY_LEASE
            self._ensure_producer_locked()
            image = self._fresh_locked(panel_id, revision)
            if image is None:
                raise FeedError('Waiting for the shared display producer to publish a fresh frame.')
            return {key: image[key] for key in ('jpeg', 'captured_epoch', 'width', 'height', 'process_identity', 'revision')}

    def _validate_image(self, image, process, region):
        content, stamp = image.get('jpeg'), image.get('captured_epoch')
        if (not isinstance(content, bytes) or not 4 <= len(content) <= self.MAX_BYTES
                or not content.startswith(b'\xff\xd8') or not content.endswith(b'\xff\xd9')
                or type(stamp) not in (int, float) or not 0 <= self.clock()-stamp <= self.MAX_FRAME_AGE
                or image.get('process_identity') != process
                or image.get('width') != region['width'] or image.get('height') != region['height']):
            raise FeedError('Capture did not produce a fresh, matching display image.')
        return {key: image[key] for key in ('jpeg', 'captured_epoch', 'width', 'height', 'process_identity')}

    def _produce_cycle(self):
        before = self._refresh_gate()
        if before['status'] != 'Ready':
            return
        plan, process = before['plan'], before['context']['process']
        try:
            pending = {}
            for panel in PANELS:
                with self.lock:
                    if self.closed or not self.enabled or self.epoch != before['epoch'] or not self._demand_locked():
                        return
                region = dict(plan['panels'][panel])
                result = self.capture(region, process, dict(plan['window']))
                pending[panel] = self._validate_image(result, process, region)
            after = self._refresh_gate()
            if (after['status'] != 'Ready' or after['revision'] != before['revision'] or
                    after['epoch'] != before['epoch']):
                return
            with self.lock:
                next_sequences = {panel: self.sequences[panel]+1 for panel in PANELS}
            for panel, image in pending.items():
                if not 0 <= self.clock()-image['captured_epoch'] <= self.MAX_FRAME_AGE:
                    raise FeedError('A captured display expired before the shared cycle completed.')
                image.update(revision=before['revision'], sequence=next_sequences[panel])
                image['packet'] = self.packet({'type': 'frame', 'panel': panel, 'revision': image['revision'],
                                                'sequence': image['sequence'], 'captured_epoch': image['captured_epoch'],
                                                'width': image['width'], 'height': image['height'],
                                                'jpeg_base64': base64.b64encode(image['jpeg']).decode('ascii')})
            with self.lock:
                if (not self.closed and self.enabled and self.epoch == before['epoch'] and
                        self.gate['status'] == 'Ready' and self.gate['revision'] == before['revision'] and
                        self._demand_locked()):
                    self.frames = pending
                    self.sequences = next_sequences
                    self.last_error = None
        except Exception as exc:
            from .display_capture import CaptureError
            message = str(exc) if isinstance(exc, (CaptureError, FeedError)) else 'DCS display capture is unavailable.'
            with self.lock:
                if self.epoch == before['epoch']:
                    self.frames.clear()
                    self.last_error = message
                    self.gate = {**self.gate, 'status': 'Unavailable', 'detail': message,
                                 'checked_mono': self.monotonic()}

    def _produce(self):
        next_cycle = 0.0
        while True:
            with self.lock:
                if self.closed:
                    return
                wanted = self.enabled and self._demand_locked()
                if not wanted:
                    self.frames.clear()
            if not wanted:
                next_cycle = 0.0
                self.wakeup.wait(.25)
                self.wakeup.clear()
                continue
            delay = next_cycle-self.monotonic()
            if delay > 0:
                self.wakeup.wait(delay)
                self.wakeup.clear()
                continue
            next_cycle = self.monotonic()+self.INTERVAL
            self._produce_cycle()

    def _require_open(self):
        if self.closed:
            raise FeedError('Restart the companion after changing the selected DCS setup.', 409)

    def close(self):
        with self.lock:
            self.closed = True
            self.enabled = False
            self._invalidate_locked('Disabled', 'Display streaming has stopped.')
            self.subscribers.clear()
            producer = self.producer
        if producer and producer is not threading.current_thread():
            producer.join(timeout=1)
