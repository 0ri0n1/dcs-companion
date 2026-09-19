"""Paired-browser controls; workflow editing requires the DCS computer."""
import hashlib
from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from .auth import COOKIE
from .pairing import local_pc_request


class StrictBody(BaseModel):
    model_config = ConfigDict(extra='forbid')


class HeartbeatBody(StrictBody):
    connection_id: str = Field(min_length=36, max_length=36)


class ArmBody(StrictBody):
    enabled: bool


class TapBody(StrictBody):
    idempotency_key: str = Field(min_length=36, max_length=36)
    action_id: str = Field(min_length=1, max_length=100)


class RunBody(StrictBody):
    idempotency_key: str = Field(min_length=36, max_length=36)
    workflow_id: str = Field(min_length=1, max_length=48)


class ScriptBody(StrictBody):
    idempotency_key: str = Field(min_length=36, max_length=36)
    script_id: str = Field(min_length=1, max_length=80)


class WorkflowBody(StrictBody):
    id: str = Field(min_length=1, max_length=48)
    name: str = Field(min_length=1, max_length=80)
    aircraft: str = Field(max_length=40)
    steps: list[dict] = Field(min_length=1, max_length=32)


def register_remote_routes(app, holder, settings):
    def owner(request):
        # Auth middleware has already validated this secret cookie. Never expose
        # its value or let client-provided IDs substitute for session ownership.
        return hashlib.sha256(request.cookies[COOKIE].encode()).hexdigest()

    def pc(request):
        if not local_pc_request(request, settings):
            raise HTTPException(403, 'Edit workflows on the DCS computer.')

    def invoke(method, *args, **kwargs):
        try:
            return getattr(holder['service'].remote, method)(*args, **kwargs)
        except (ValueError, TypeError, KeyError, OSError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get('/api/remote')
    def status(request: Request):
        result = invoke('status', owner(request))
        result['can_edit'] = local_pc_request(request, settings)
        result['can_arm'] = True
        return result

    @app.get('/api/remote/status')
    def compact_status(request: Request):
        result = invoke('status', owner(request), include_catalog=False)
        result['can_edit'] = local_pc_request(request, settings)
        result['can_arm'] = True
        return result

    @app.post('/api/remote/heartbeat')
    def heartbeat(body: HeartbeatBody, request: Request):
        return invoke('heartbeat', owner(request), body.connection_id)

    @app.post('/api/remote/disconnect')
    def disconnect(body: HeartbeatBody, request: Request):
        return invoke('disconnect', owner(request), body.connection_id)

    @app.post('/api/remote/arm')
    def arm(body: ArmBody, request: Request):
        return invoke('enable', body.enabled, owner(request))

    @app.post('/api/remote/tap')
    def tap(body: TapBody, request: Request):
        return invoke('start', body.idempotency_key, owner(request), action_id=body.action_id)

    @app.post('/api/remote/run')
    def run(body: RunBody, request: Request):
        return invoke('start', body.idempotency_key, owner(request), workflow_id=body.workflow_id)

    @app.post('/api/remote/script')
    def script(body: ScriptBody, request: Request):
        return invoke('start', body.idempotency_key, owner(request), script_id=body.script_id)

    @app.post('/api/remote/stop')
    def stop(body: StrictBody):
        return invoke('stop')

    @app.post('/api/remote/workflows')
    def save(body: WorkflowBody, request: Request):
        pc(request)
        return invoke('save_workflow', body.model_dump())

    @app.delete('/api/remote/workflows/{ident}')
    def delete(ident: str, request: Request):
        pc(request)
        return invoke('delete_workflow', ident)
