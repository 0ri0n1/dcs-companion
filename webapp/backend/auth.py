"""Restricted-origin cookie pairing; QR fragments contain only one-use tickets."""
import hashlib
import re
import secrets
import threading
import time
from urllib.parse import urlsplit
from fastapi import HTTPException, Request
from .pairing import MAX_TICKETS, TICKET_SECONDS, lan_root, local_pc_request, render_pairing_qr

COOKIE = "dcs_dashboard_session"


class Auth:
    def __init__(self, settings, *, clock=time.time):
        self.settings = settings
        self.clock = clock
        self.lock = threading.RLock()
        self.sessions = {}
        self.attempts = {}
        self.pairing_tickets = {}
        self.allowed_hosts = {urlsplit(origin).netloc.lower() for origin in settings.origins}

    def validate_request(self, request: Request):
        if request.headers.get("host", "").lower() not in self.allowed_hosts:
            raise HTTPException(403, "Host is not configured for this dashboard.")
        origin = request.headers.get("origin")
        if origin is not None and origin not in self.settings.origins:
            raise HTTPException(403, "Origin is not allowed.")
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, "Cross-site access is refused.")
        if request.method not in ("GET", "HEAD", "OPTIONS") and not origin:
            raise HTTPException(403, "An exact allowed Origin is required for changes.")

    def _attempts_locked(self, host, now):
        self.attempts = {key: [at for at in values if at > now-60]
                         for key, values in self.attempts.items() if any(at > now-60 for at in values)}
        attempts = self.attempts.get(host, [])
        if len(attempts) >= 10 or host not in self.attempts and len(self.attempts) >= 256:
            raise HTTPException(429, "Too many pairing attempts. Wait one minute.")
        return attempts

    def _session_locked(self, now):
        self.sessions = {key: value for key, value in self.sessions.items() if value['expires'] > now}
        session = secrets.token_urlsafe(32)
        self.sessions[session] = {'expires': now+12*3600, 'last_seen': now}
        return session

    def pair(self, request, token='', ticket=None):
        if ticket is not None:
            return self.redeem_pairing_ticket(request, ticket)
        self.validate_request(request)
        now = self.clock()
        host = request.client.host if request.client else "unknown"
        with self.lock:
            attempts = self._attempts_locked(host, now)
            local = host in ("127.0.0.1", "::1", "testclient")
            if self.settings.lan_enabled and not (local and token == ""):
                if (not isinstance(token, str) or len(token) > 4096 or
                        not secrets.compare_digest(token.encode('utf-8'), self.settings.pairing_token.encode('utf-8'))):
                    self.attempts[host] = attempts + [now]
                    raise HTTPException(401, "Enter the pairing code shown on the DCS computer.")
            elif not local:
                raise HTTPException(403, "Loopback-only access.")
            return self._session_locked(now)

    def issue_pairing_ticket(self, request):
        self.require(request)
        root = lan_root(self.settings)
        if not local_pc_request(request, self.settings):
            raise HTTPException(403, 'Create the phone pairing code on the DCS computer.')
        with self.lock:
            now = self.clock()
            self.pairing_tickets = {key: value for key, value in self.pairing_tickets.items() if value['expires'] > now}
            if len(self.pairing_tickets) >= MAX_TICKETS:
                raise HTTPException(429, 'Eight pairing codes are already active. Wait two minutes before creating another.')
            ticket = secrets.token_urlsafe(32)
            digest = hashlib.sha256(ticket.encode('ascii')).hexdigest()
            url = root+'/#pair='+ticket
            svg = render_pairing_qr(url)
            self.pairing_tickets[digest] = {'expires': now+TICKET_SECONDS}
            return {'url': url, 'expires_at': now+TICKET_SECONDS, 'expires_in': TICKET_SECONDS, 'qr_svg': svg}

    def redeem_pairing_ticket(self, request, ticket):
        self.validate_request(request)
        if not self.settings.lan_enabled:
            raise HTTPException(403, 'Phone pairing is unavailable while LAN access is disabled.')
        host = request.client.host if request.client else 'unknown'
        with self.lock:
            now = self.clock()
            attempts = self._attempts_locked(host, now)
            self.pairing_tickets = {key: value for key, value in self.pairing_tickets.items() if value['expires'] > now}
            valid = isinstance(ticket, str) and re.fullmatch(r'[A-Za-z0-9_-]{43}', ticket) is not None
            digest = hashlib.sha256(ticket.encode('ascii')).hexdigest() if valid else None
            record = self.pairing_tickets.get(digest)
            if record is None:
                self.attempts[host] = attempts+[now]
                raise HTTPException(401, 'This pairing code is invalid, expired, or already used. Create a new code on the DCS computer.')
            del self.pairing_tickets[digest]
            return self._session_locked(now)

    def require(self, request):
        self.validate_request(request)
        with self.lock:
            token = request.cookies.get(COOKIE, "")
            session = self.sessions.get(token)
            now = self.clock()
            if not session or session['expires'] <= now:
                raise HTTPException(401, "Pair this browser before reading live state or sending commands.")
            session['last_seen'] = now
            return session

    def clients(self):
        with self.lock:
            now = self.clock()
            return sum(1 for session in self.sessions.values() if session['expires'] > now and session['last_seen'] > now-30)
