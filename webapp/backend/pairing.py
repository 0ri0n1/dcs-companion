"""Local QR rendering and configured LAN origins; no external QR service."""
import ipaddress
from urllib.parse import urlsplit

from fastapi import HTTPException
import qrcode
from qrcode.image.svg import SvgPathFillImage


TICKET_SECONDS = 120
MAX_TICKETS = 8


def _address(value):
    try:
        return ipaddress.ip_address(str(value).strip('[]'))
    except ValueError:
        return None


def local_pc_request(request, settings):
    """Use the server's client address, never client-supplied forwarding headers."""
    client = request.client.host if request.client else None
    if client in ('127.0.0.1', '::1', 'testclient'):
        return True
    candidate, configured = _address(client), _address(settings.host)
    return bool(settings.lan_enabled and candidate and configured and candidate == configured)


def lan_root(settings):
    if not settings.lan_enabled:
        raise HTTPException(403, 'Phone pairing needs LAN access. Start the dashboard with its tablet/LAN option first.')
    address = _address(settings.host)
    if address is None or address.is_loopback or address.is_unspecified or address.is_multicast:
        raise HTTPException(403, 'Phone pairing needs one configured LAN address for this DCS computer.')
    origins = []
    for value in settings.origins:
        try:
            parsed = urlsplit(value)
            port = parsed.port or (443 if parsed.scheme == 'https' else 80)
            if (parsed.scheme in ('http', 'https') and _address(parsed.hostname) == address and
                    port == settings.port and parsed.username is None and parsed.password is None and
                    parsed.path in ('', '/') and not parsed.query and not parsed.fragment):
                origins.append((parsed.scheme == 'https', value.rstrip('/')))
        except (ValueError, TypeError):
            pass
    if not origins:
        raise HTTPException(403, 'The configured LAN address is not an allowed dashboard origin.')
    return max(origins)[1]


def render_pairing_qr(url):
    """Render an application-constructed URL as path-only black-on-white SVG."""
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=4)
    qr.add_data(url, optimize=0)
    qr.make(fit=True)
    return qr.make_image(image_factory=SvgPathFillImage).to_string(encoding='unicode')
