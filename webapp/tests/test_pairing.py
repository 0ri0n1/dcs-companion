from concurrent.futures import ThreadPoolExecutor
import hashlib
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from fastapi import HTTPException
import pytest
from starlette.requests import Request

from webapp.backend.auth import Auth, COOKIE
from webapp.backend.config import Settings
from webapp.backend.pairing import lan_root


ORIGIN = 'http://192.168.20.10:18787'


def settings(**changes):
    result = Settings(host='192.168.20.10', lan_enabled=True, pairing_token='PRIVATE-main-LAN-secret-'+'x'*32,
                      origins=(ORIGIN, 'http://127.0.0.1:18787'))
    for key, value in changes.items(): setattr(result, key, value)
    return result


def request(host='127.0.0.1', *, cookie=None, origin=ORIGIN, headers=None):
    values = {'host': '192.168.20.10:18787'}
    if origin is not None: values['origin'] = origin
    if cookie: values['cookie'] = COOKIE+'='+cookie
    values.update(headers or {})
    return Request({'type': 'http', 'method': 'POST', 'path': '/api/pairing', 'scheme': 'http',
                    'server': ('192.168.20.10', 18787), 'client': (host, 54321),
                    'headers': [(key.encode(), value.encode()) for key, value in values.items()]})


def fixture():
    now = [1_800_000_000.]
    auth = Auth(settings(), clock=lambda: now[0])
    cookie = auth.pair(request(), '')
    return auth, now, request(cookie=cookie)


def ticket(result):
    return urlsplit(result['url']).fragment.removeprefix('pair=')


def test_issue_generates_safe_local_svg_with_only_short_lived_fragment_ticket():
    auth, now, local = fixture()
    result = auth.issue_pairing_ticket(local)
    parsed = urlsplit(result['url'])
    value = ticket(result)
    assert parsed.scheme == 'http' and parsed.netloc == '192.168.20.10:18787'
    assert parsed.path == '/' and parsed.query == '' and len(value) == 43
    assert result['expires_at'] == now[0]+120 and result['expires_in'] == 120
    assert auth.settings.pairing_token not in str(result)
    assert value not in str(auth.pairing_tickets)
    assert list(auth.pairing_tickets) == [hashlib.sha256(value.encode('ascii')).hexdigest()]
    tree = ET.fromstring(result['qr_svg'])
    assert tree.tag == '{http://www.w3.org/2000/svg}svg'
    assert {element.tag.rsplit('}', 1)[-1] for element in tree.iter()} <= {'svg', 'rect', 'path'}
    assert any(element.get('fill') == 'white' for element in tree.iter())
    assert any(element.tag.endswith('path') and element.get('d') for element in tree.iter())
    assert 'http' not in result['qr_svg'].replace('http://www.w3.org/2000/svg', '')


def test_phone_redeems_once_to_same_twelve_hour_cookie_session_model():
    auth, now, local = fixture()
    code = ticket(auth.issue_pairing_ticket(local))
    phone = request('192.168.20.55')
    session = auth.pair(phone, token='irrelevant', ticket=code)
    assert auth.sessions[session]['expires'] == now[0]+12*3600
    assert auth.require(request('192.168.20.55', cookie=session))['last_seen'] == now[0]
    assert auth.pairing_tickets == {}
    with pytest.raises(HTTPException) as error:
        auth.redeem_pairing_ticket(phone, code)
    assert error.value.status_code == 401


@pytest.mark.parametrize('elapsed,success', [(119.999, True), (120, False), (121, False)])
def test_ticket_two_minute_expiry_is_exact(elapsed, success):
    auth, now, local = fixture()
    code = ticket(auth.issue_pairing_ticket(local))
    now[0] += elapsed
    if success:
        assert auth.redeem_pairing_ticket(request('192.168.20.55'), code)
    else:
        with pytest.raises(HTTPException) as error:
            auth.redeem_pairing_ticket(request('192.168.20.55'), code)
        assert error.value.status_code == 401
        assert auth.pairing_tickets == {}


def test_issuance_requires_authenticated_cookie():
    auth, _, _ = fixture()
    with pytest.raises(HTTPException) as error:
        auth.issue_pairing_ticket(request())
    assert error.value.status_code == 401 and auth.pairing_tickets == {}


@pytest.mark.parametrize('host', ['127.0.0.1', '::1', 'testclient', '192.168.20.10'])
def test_only_real_local_pc_source_can_issue(host):
    auth, _, _ = fixture()
    cookie = auth.pair(request(host), auth.settings.pairing_token)
    assert auth.issue_pairing_ticket(request(host, cookie=cookie))['url'].startswith(ORIGIN)


def test_authenticated_phone_cannot_issue_or_spoof_local_source_with_headers():
    auth, _, _ = fixture()
    cookie = auth.pair(request('192.168.20.55'), auth.settings.pairing_token)
    remote = request('192.168.20.55', cookie=cookie,
                     headers={'x-forwarded-for': '127.0.0.1', 'x-real-ip': '192.168.20.10'})
    with pytest.raises(HTTPException) as error:
        auth.issue_pairing_ticket(remote)
    assert error.value.status_code == 403 and auth.pairing_tickets == {}


def test_lan_disabled_has_readable_reason_for_issue_and_redeem():
    auth, _, local = fixture()
    auth.settings.lan_enabled = False
    for operation in (lambda: auth.issue_pairing_ticket(local),
                      lambda: auth.redeem_pairing_ticket(request(), 'x'*43)):
        with pytest.raises(HTTPException) as error: operation()
        assert error.value.status_code == 403 and 'LAN' in error.value.detail


@pytest.mark.parametrize('host,origins', [('0.0.0.0', ('http://0.0.0.0:18787',)),
                                       ('example.com', ('http://example.com:18787',)),
                                       ('127.0.0.1', ('http://127.0.0.1:18787',)),
                                       ('192.168.20.10', ('http://other.example:18787',)),
                                       ('192.168.20.10', ('http://192.168.20.10:18787/path',)),
                                       ('192.168.20.10', ('http://name:secret@192.168.20.10:18787',))])
def test_qr_origin_must_be_exact_configured_lan_root(host, origins):
    with pytest.raises(HTTPException): lan_root(settings(host=host, origins=origins))


def test_configured_ipv6_lan_root_and_local_source_are_supported():
    config = settings(host='fd00::10', origins=('http://[fd00::10]:18787',))
    assert lan_root(config) == 'http://[fd00::10]:18787'


@pytest.mark.parametrize('origin,extra', [('http://evil.example', {}), (None, {}),
                                        (ORIGIN, {'host': 'evil.example'}),
                                        (ORIGIN, {'sec-fetch-site': 'cross-site'})])
def test_bad_origin_or_host_never_consumes_ticket(origin, extra):
    auth, _, local = fixture()
    code = ticket(auth.issue_pairing_ticket(local))
    with pytest.raises(HTTPException) as error:
        auth.redeem_pairing_ticket(request('192.168.20.55', origin=origin, headers=extra), code)
    assert error.value.status_code == 403 and len(auth.pairing_tickets) == 1
    assert auth.redeem_pairing_ticket(request('192.168.20.55'), code)


def test_invalid_ticket_does_not_fall_back_to_correct_main_secret_or_local_empty_pairing():
    auth, _, _ = fixture()
    for client in ('127.0.0.1', '192.168.20.55'):
        with pytest.raises(HTTPException) as error:
            auth.pair(request(client), token=auth.settings.pairing_token, ticket='bad')
        assert error.value.status_code == 401


@pytest.mark.parametrize('value', ['', 'a'*10000, 'ü'*43, ['a'*43], None])
def test_malformed_ticket_is_bounded_and_rejected(value):
    auth, _, _ = fixture()
    with pytest.raises(HTTPException) as error:
        auth.redeem_pairing_ticket(request('192.168.20.55'), value)
    assert error.value.status_code == 401


def test_ticket_failures_share_existing_pair_rate_limit_and_expire_after_one_minute():
    auth, now, local = fixture()
    code = ticket(auth.issue_pairing_ticket(local))
    phone = request('192.168.20.55')
    for _ in range(10):
        with pytest.raises(HTTPException) as error: auth.redeem_pairing_ticket(phone, 'invalid')
        assert error.value.status_code == 401
    with pytest.raises(HTTPException) as error: auth.redeem_pairing_ticket(phone, code)
    assert error.value.status_code == 429 and len(auth.pairing_tickets) == 1
    now[0] += 60
    assert auth.redeem_pairing_ticket(phone, code)


def test_eight_ticket_bound_and_expired_cleanup(monkeypatch):
    monkeypatch.setattr('webapp.backend.auth.render_pairing_qr', lambda url: '<svg/>')
    auth, now, local = fixture()
    for _ in range(8): auth.issue_pairing_ticket(local)
    with pytest.raises(HTTPException) as error: auth.issue_pairing_ticket(local)
    assert error.value.status_code == 429 and len(auth.pairing_tickets) == 8
    now[0] += 120
    assert auth.issue_pairing_ticket(local)
    assert len(auth.pairing_tickets) == 1


def test_simultaneous_redemption_creates_exactly_one_session():
    auth, _, local = fixture()
    code = ticket(auth.issue_pairing_ticket(local))
    initial_sessions = len(auth.sessions)
    def redeem(_):
        try: return auth.redeem_pairing_ticket(request('192.168.20.55'), code)
        except HTTPException as error: return error.status_code
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(redeem, range(8)))
    assert sum(isinstance(outcome, str) for outcome in outcomes) == 1
    assert outcomes.count(401) == 7
    assert len(auth.sessions) == initial_sessions+1


def test_existing_long_token_and_local_pairing_still_work_and_sessions_expire():
    auth, now, _ = fixture()
    cookie = auth.pair(request('192.168.20.55'), auth.settings.pairing_token)
    assert auth.require(request('192.168.20.55', cookie=cookie))
    now[0] += 12*3600
    with pytest.raises(HTTPException) as error: auth.require(request('192.168.20.55', cookie=cookie))
    assert error.value.status_code == 401 and auth.clients() == 0


def test_restart_forgets_tickets_and_invalid_unicode_main_token_does_not_raise_server_error():
    auth, now, local = fixture()
    code = ticket(auth.issue_pairing_ticket(local))
    restarted = Auth(auth.settings, clock=lambda: now[0])
    with pytest.raises(HTTPException) as error:
        restarted.redeem_pairing_ticket(request('192.168.20.55'), code)
    assert error.value.status_code == 401
    with pytest.raises(HTTPException) as error:
        auth.pair(request('192.168.20.55'), '\u00fc'*43)
    assert error.value.status_code == 401
