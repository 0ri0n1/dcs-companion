from dataclasses import dataclass, field
from pathlib import Path
import os

ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = ROOT / "webapp"


@dataclass
class Settings:
    state_path: Path = ROOT / "state" / "state.json"
    runtime_dir: Path = APP_ROOT / "runtime"
    data_dir: Path = APP_ROOT / "data"
    host: str = "127.0.0.1"
    port: int = 18787
    dry_run: bool = True
    lan_enabled: bool = False
    pairing_token: str = ""
    origins: tuple[str, ...] = field(default_factory=lambda: (
        "http://127.0.0.1:18787", "http://localhost:18787"))
    expiry_s: float = 15.0
    settle_s: float = 0.8
    mission_path: Path | None = None
    mission_awareness: bool = True
    install_root: Path | None = None
    saved_games: Path | None = None

    @classmethod
    def from_env(cls):
        host = os.getenv("DCS_DASH_HOST", "127.0.0.1")
        port = int(os.getenv("DCS_DASH_PORT", "18787"))
        lan = host not in ("127.0.0.1", "localhost", "::1")
        origins = tuple(x.strip() for x in os.getenv("DCS_DASH_ORIGINS", "").split(",") if x.strip())
        if not origins:
            origins = (f"http://127.0.0.1:{port}", f"http://localhost:{port}")
        token = os.getenv("DCS_DASH_PAIRING_TOKEN", "")
        if lan and (len(token) < 24 or not any(host in x for x in origins)):
            raise ValueError("Tablet mode requires an exact LAN address, restricted origins and a pairing token (24+ characters).")
        if host in ("0.0.0.0", "::") or any("*" in x for x in origins):
            raise ValueError("Use one explicit interface address and exact origins; wildcards are refused.")
        mission = os.getenv("DCS_DASH_MISSION")
        return cls(host=host, port=port, lan_enabled=lan, pairing_token=token,
                   origins=origins, dry_run=os.getenv("DCS_DASH_ARM", "") != "YES",
                   expiry_s=max(3, min(30, float(os.getenv("DCS_DASH_EXPIRY", "15")))),
                   mission_path=Path(mission) if mission else None,
                   install_root=Path(os.environ["DCS_DASH_INSTALL_ROOT"]) if os.getenv("DCS_DASH_INSTALL_ROOT") else None,
                   saved_games=Path(os.environ["DCS_DASH_SAVED_GAMES"]) if os.getenv("DCS_DASH_SAVED_GAMES") else None,
                   mission_awareness=os.getenv("DCS_DASH_MISSION_AWARENESS", "YES") == "YES")
