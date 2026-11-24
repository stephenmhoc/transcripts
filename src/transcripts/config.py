from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
import logging
import os

from dotenv import load_dotenv


def quiet_http_logs():
    """Request logs carry Overcast codes, tokens and worker URLs; keep them out."""
    for name in ("httpx","httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def origin_only(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment):
        raise ValueError("Base URLs must be HTTP(S) origins without paths or credentials")
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Non-loopback origins require HTTPS")
    # Browsers send Origin and Host without a default port or trailing slash.
    default = {"http":80,"https":443}[parsed.scheme]
    port = "" if parsed.port in {None,default} else f":{parsed.port}"
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"{parsed.scheme}://{host}{port}"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    origin: str
    owner_token: str
    mcp_token: str
    worker_token: str
    session_secret: str
    public_origin: str = ""  # Optional separate share host; defaults to origin (/p/ paths).
    demo: bool = False
    discovery_enabled: bool = True
    poll_seconds: int = 43200
    initial_days: int = 7
    min_progress: int = 300
    queue_limit: int = 20
    lease_seconds: int = 900
    max_attempts: int = 3
    # Cloudflare Access: a verified Access identity for this email signs the owner in.
    access_team: str = ""
    access_aud: str = ""
    access_email: str = ""

    @property
    def db_path(self):
        return self.data_dir / "library.sqlite3"

    @property
    def public_db_path(self):
        return self.data_dir / "public" / "shares.sqlite3"

    @property
    def share_origin(self):
        return self.public_origin or self.origin

    @property
    def cookie_secure(self):
        return self.origin.startswith("https:")

    def validate(self):
        origin_only(self.origin)
        if self.public_origin:
            origin_only(self.public_origin)
        secrets = [self.owner_token, self.mcp_token, self.worker_token, self.session_secret]
        if any(len(s) < 32 or s.startswith("replace-") for s in secrets) or len(set(secrets)) != 4:
            raise ValueError("Configure four different random secrets of at least 32 characters; run transcripts init")
        if self.poll_seconds < 43200:
            raise ValueError("Overcast polling is limited to twice daily")
        if not 1 <= self.queue_limit <= 100 or not 1 <= self.initial_days <= 365:
            raise ValueError("Invalid discovery bounds")
        access = [self.access_team, self.access_aud, self.access_email]
        if any(access) and not all(access):
            raise ValueError("Cloudflare Access needs ACCESS_TEAM_DOMAIN, ACCESS_AUD and ACCESS_EMAIL together")
        if self.access_team and not self.access_team.startswith("https://"):
            raise ValueError("ACCESS_TEAM_DOMAIN must be an HTTPS origin")
        return self

    @property
    def access_enabled(self):
        return bool(self.access_team)

    @classmethod
    def from_env(cls):
        load_dotenv(os.environ.get("TRANSCRIPTS_ENV", ".env"))
        return cls(
            data_dir=Path(os.environ.get("DATA_DIR", "data")).resolve(),
            origin=origin_only(os.environ.get("APP_ORIGIN", "http://127.0.0.1:8765")),
            owner_token=os.environ.get("OWNER_TOKEN", ""),
            mcp_token=os.environ.get("MCP_TOKEN", ""),
            worker_token=os.environ.get("WORKER_TOKEN", ""),
            session_secret=os.environ.get("SESSION_SECRET", ""),
            public_origin=origin_only(os.environ["PUBLIC_BASE_URL"]) if os.environ.get("PUBLIC_BASE_URL") else "",
            demo=os.environ.get("DEMO_MODE", "false").lower() == "true",
            discovery_enabled=os.environ.get("DISCOVERY_ENABLED", "true").lower() == "true",
            access_team=origin_only(os.environ["ACCESS_TEAM_DOMAIN"]) if os.environ.get("ACCESS_TEAM_DOMAIN") else "",
            access_aud=os.environ.get("ACCESS_AUD", ""),
            access_email=os.environ.get("ACCESS_EMAIL", "").strip().lower(),
        ).validate()
