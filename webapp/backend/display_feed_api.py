"""Display feed routes share the existing paired-cookie and exact-origin boundary."""
import asyncio
import time
from fastapi import HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .display_feed import FeedError
from .pairing import local_pc_request


class StrictBody(BaseModel):
    model_config = ConfigDict(extra='forbid')


class PlanBody(StrictBody):
    monitor_id: str = Field(min_length=1, max_length=160)


class ApplyBody(StrictBody):
    plan_id: str = Field(min_length=1, max_length=128)


class EnableBody(StrictBody):
    enabled: bool = Field(strict=True)


def register_display_feed_routes(app, holder, settings):
    def service():
        value = getattr(holder['service'], 'display_feed', None)
        if value is None:
            raise HTTPException(503, 'Display capture is unavailable.')
        return value

    def pc(request):
        if not local_pc_request(request, settings):
            raise HTTPException(403, 'Configure or enable display capture on the DCS computer.')

    def invoke(method, *args):
        try:
            return getattr(service(), method)(*args)
        except FeedError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(409, 'The display setup files are unavailable. Check the PC setup and retry.') from exc

    def status(request):
        result = invoke('status')
        result['can_edit'] = local_pc_request(request, settings)
        return result

    @app.get('/api/display-feed')
    def get_status(request: Request):
        return status(request)

    @app.post('/api/display-feed/plan')
    def prepare(body: PlanBody, request: Request):
        pc(request)
        invoke('prepare', body.monitor_id)
        return status(request)

    @app.post('/api/display-feed/apply')
    def apply(body: ApplyBody, request: Request):
        pc(request)
        invoke('apply', body.plan_id)
        return status(request)

    @app.post('/api/display-feed/restore')
    def restore(body: StrictBody, request: Request):
        pc(request)
        invoke('restore')
        return status(request)

    @app.post('/api/display-feed/enabled')
    def enabled(body: EnableBody, request: Request):
        pc(request)
        invoke('enable', body.enabled)
        return status(request)

    @app.get('/api/display-feed/stream')
    async def stream(request: Request, panels: str = Query(min_length=1, max_length=64),
                     revision: str = Query(min_length=1, max_length=128)):
        feed = service()
        app.state.auth.require(request)
        subscription_task = asyncio.create_task(asyncio.to_thread(feed.subscribe, panels.split(','), revision))
        try:
            # Brief setup proof happens off-loop. The long-lived stream itself
            # consumes no Starlette worker and performs no native/file work.
            subscriber = await asyncio.shield(subscription_task)
        except asyncio.CancelledError:
            # to_thread cannot be cancelled mid-proof. Clean up a subscription
            # that finishes after its HTTP caller has already disconnected.
            def release_late(task):
                try:
                    feed.unsubscribe(task.result())
                except Exception:
                    pass
            subscription_task.add_done_callback(release_late)
            raise
        except FeedError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(409, 'Display setup could not be read. Refresh the PC setup.') from exc

        async def generate():
            sequences, previous_status, next_status = {}, None, 0.0
            try:
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        # Memory-only cookie/origin check each loop, including
                        # before every frame; revoked sessions receive no more pixels.
                        app.state.auth.require(request)
                    except HTTPException:
                        yield feed.packet({'type': 'status', 'enabled': False, 'status': 'Unauthorized',
                                           'detail': 'Pair this browser again to view display streams.',
                                           'revision': '', 'aircraft': None, 'panels': []})
                        return
                    state, frames, ended = feed.stream_snapshot(subscriber, sequences)
                    signature = tuple(state.get(key) for key in ('enabled', 'status', 'detail', 'revision', 'aircraft'))
                    now = time.monotonic()
                    if ended or signature != previous_status or now >= next_status:
                        yield feed.packet(state)
                        previous_status, next_status = signature, now+.5
                    if ended:
                        return
                    for panel, sequence, stamp, packet in frames:
                        # A slow receiver may have blocked a previous yield.
                        # Recheck authorization/context/cache before sending each
                        # next panel; never drain an old per-client frame queue.
                        try:
                            app.state.auth.require(request)
                        except HTTPException:
                            return
                        latest, available, ended = feed.stream_snapshot(subscriber, sequences)
                        if ended:
                            yield feed.packet(latest)
                            return
                        current = next((item for item in available if item[0] == panel), None)
                        if current is None:
                            continue
                        panel, sequence, stamp, packet = current
                        if not 0 <= feed.clock()-stamp <= feed.MAX_FRAME_AGE:
                            continue
                        sequences[panel] = sequence
                        yield packet
                    await asyncio.sleep(.02)
            finally:
                feed.unsubscribe(subscriber)

        return StreamingResponse(generate(), media_type='application/x-ndjson', headers={
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'X-Accel-Buffering': 'no',
            'X-Display-Revision': revision})

    @app.get('/api/display-feed/{panel_id}/frame.jpg')
    def frame(panel_id: str, revision: str = Query(min_length=1, max_length=128)):
        item = invoke('frame', panel_id, revision)
        return Response(content=item['jpeg'], media_type='image/jpeg', headers={
            'Cache-Control': 'no-store', 'X-Captured-At': str(item['captured_epoch']),
            'X-Display-Revision': item['revision'], 'X-Content-Type-Options': 'nosniff'})
