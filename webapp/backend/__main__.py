from pathlib import Path
import sys
import json
import os
import threading
import time
import socket
import psutil
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import uvicorn
from .config import Settings
from .app import create_app


def local_listeners(settings):
    """Tablet mode keeps this PC's loopback page working; never bind a wildcard."""
    addresses = [settings.host]
    if settings.lan_enabled:
        addresses.append("127.0.0.1")
    listeners = []
    try:
        for address in addresses:
            if address in ("0.0.0.0", "::"):
                raise ValueError("A specific local interface is required.")
            listener = socket.socket(socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM)
            listeners.append(listener)
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind((address, settings.port))
            listener.listen(2048)
            listener.setblocking(False)
        return listeners
    except Exception:
        for listener in listeners:
            listener.close()
        raise

if __name__ == "__main__":
    settings = Settings.from_env()
    app = create_app(settings)
    server = uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port,
                                         access_log=False, workers=1, timeout_graceful_shutdown=3))
    created = psutil.Process(os.getpid()).create_time()
    def watch_stop():
        marker = settings.runtime_dir / "stop-request.json"
        while not server.should_exit:
            try:
                request = json.loads(marker.read_text())
                if request.get("pid") == os.getpid() and request.get("created") == created and hasattr(app.state, "dashboard_service"):
                    app.state.dashboard_service.queue.stop()
                    app.state.connection_diagnostics.event("restart_requested")
                    app.state.connection_diagnostics.stopping = True
                    server.should_exit = True
                    return
            except (OSError, ValueError):
                pass
            time.sleep(.2)
    threading.Thread(target=watch_stop, daemon=True).start()
    sockets = local_listeners(settings)
    try:
        server.run(sockets=sockets)
    finally:
        for listener in sockets:
            listener.close()
