"""Kite Connect login (research only: historical data, never orders).

Flow (``orb login``), once per trading day (access tokens expire around 06:00 IST
the next morning):
  1. reads KITE_API_KEY and KITE_API_SECRET from the environment or ``.env``;
  2. prints the Kite login URL; you log in in a browser and are redirected to
     your app's redirect URL with ``?request_token=...``;
  3. you paste that redirect URL (or the token) back; the token is exchanged
     for an access token via ``generate_session``;
  4. the access token is saved to ``data/_secrets/kite_session.json`` (mode 600,
     gitignored) and picked up by ``orb download``.

Secrets are never printed or logged.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, time, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

API_KEY_ENV = "KITE_API_KEY"
API_SECRET_ENV = "KITE_API_SECRET"
ACCESS_TOKEN_ENV = "KITE_ACCESS_TOKEN"
IST = ZoneInfo("Asia/Kolkata")
EXPIRY_TIME = time(6, 0)  # Kite access tokens are invalidated around 06:00 IST


class KiteAuthError(RuntimeError):
    pass


def load_dotenv(path: str | Path = ".env") -> list[str]:
    """Minimal .env reader: KEY=VALUE lines, # comments, optional quotes.
    Never overrides variables already set. Returns the keys it set."""
    p = Path(path)
    if not p.exists():
        return []
    set_keys = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val
            set_keys.append(key)
    return set_keys


def session_path(data_root: str | Path) -> Path:
    return Path(data_root) / "_secrets" / "kite_session.json"


def extract_request_token(text: str) -> str:
    """Accept the full redirect URL or the bare request_token."""
    text = text.strip()
    if "request_token=" in text:
        qs = parse_qs(urlparse(text).query)
        tok = qs.get("request_token", [""])[0]
    else:
        tok = text
    if not re.fullmatch(r"[A-Za-z0-9]{8,64}", tok):
        raise KiteAuthError("could not find a request_token in the input")
    return tok


def _require(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise KiteAuthError(f"{name} is not set (add it to .env)")
    return v


def login_url(kite_factory=None) -> str:
    kite = (kite_factory or _kite)(_require(API_KEY_ENV))
    return kite.login_url()


def _kite(api_key: str):
    from kiteconnect import KiteConnect  # optional dependency: uv sync --extra kite

    return KiteConnect(api_key=api_key)


def create_session(
    request_token: str, data_root: str | Path, kite_factory=None, now: datetime | None = None
) -> dict:
    """Exchange the request token, save the session, return non-secret metadata."""
    api_key, secret = _require(API_KEY_ENV), _require(API_SECRET_ENV)
    kite = (kite_factory or _kite)(api_key)
    data = kite.generate_session(request_token, api_secret=secret)
    token = data.get("access_token")
    if not token:
        raise KiteAuthError("Kite returned no access_token")
    now = now or datetime.now(IST)
    meta = {
        "user_id": data.get("user_id"),
        "login_time": now.isoformat(timespec="seconds"),
        "expires_after": expiry_for(now).isoformat(timespec="seconds"),
        "api_key_suffix": api_key[-4:],
    }
    p = session_path(data_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({**meta, "access_token": token}))
    os.chmod(tmp, 0o600)
    tmp.replace(p)
    return meta


def expiry_for(login: datetime) -> datetime:
    """Next 06:00 IST after the login time."""
    local = login.astimezone(IST)
    cut = datetime.combine(local.date(), EXPIRY_TIME, tzinfo=IST)
    return cut if local < cut else cut + timedelta(days=1)


def access_token(data_root: str | Path, now: datetime | None = None) -> str:
    """KITE_ACCESS_TOKEN from the environment, else today's saved session."""
    env = os.environ.get(ACCESS_TOKEN_ENV, "").strip()
    if env:
        return env
    p = session_path(data_root)
    if not p.exists():
        raise KiteAuthError("no Kite session: run `orb login`")
    s = json.loads(p.read_text())
    now = now or datetime.now(IST)
    if now >= datetime.fromisoformat(s["expires_after"]):
        raise KiteAuthError(f"Kite session from {s['login_time']} has expired: run `orb login`")
    return s["access_token"]
