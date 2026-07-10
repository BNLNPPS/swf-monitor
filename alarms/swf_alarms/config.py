"""Engine config - engine-level + DB + email.

Alarm configurations live in the DB as kind='alarm' entries, not in TOML.
Operators edit them via the /swf-monitor/alarms/<name>/edit/ UI.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus


@dataclass
class EngineConfig:
    service_base_url: str
    request_timeout: int = 20
    log_path: str | None = None


@dataclass
class EmailConfig:
    provider: str
    from_addr: str
    region: str = ""
    smtp_host: str = ""
    smtp_port: int = 25
    smtp_timeout: int = 20


@dataclass
class Config:
    engine: EngineConfig
    email: EmailConfig
    db_dsn: str
    raw: dict


def _parse_dotenv(path: str) -> dict:
    out: dict[str, str] = {}
    if not os.path.exists(path):
        return out
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                v = v[1:-1]
            out[k.strip()] = v
    return out


def _compose_dsn(db_section: dict) -> str:
    if db_section.get('dsn'):
        return str(db_section['dsn'])

    env_path = os.path.expanduser(
        db_section.get('env_path', '/opt/swf-monitor/config/env/production.env'))
    env = _parse_dotenv(env_path)

    def pick(k, ek, default=None):
        if db_section.get(k) is not None:
            return db_section[k]
        if env.get(ek) is not None:
            return env[ek]
        if k in os.environ:
            return os.environ[k]
        return default

    host = pick('host', 'DB_HOST', 'localhost')
    port = pick('port', 'DB_PORT', '5432')
    name = pick('name', 'DB_NAME', 'swfdb')
    user = pick('user', 'DB_USER', 'admin')
    password = pick('password', 'DB_PASSWORD', '')

    userinfo = quote_plus(str(user))
    if password:
        userinfo += ':' + quote_plus(str(password))
    return f"postgresql://{userinfo}@{host}:{port}/{name}"


def load(path: str | Path) -> Config:
    with open(path, 'rb') as f:
        raw = tomllib.load(f)
    eng = raw['engine']
    service_base_url = (
        eng.get('service_base_url') or eng.get('swf_remote_base_url') or ''
    ).rstrip('/')
    if not service_base_url:
        raise KeyError('engine.service_base_url')
    engine = EngineConfig(
        service_base_url=service_base_url,
        request_timeout=int(eng.get('request_timeout', 20)),
        log_path=os.path.expanduser(eng['log_path']) if eng.get('log_path') else None,
    )
    e = raw['email']
    email = EmailConfig(
        provider=e.get('provider', 'smtp'),
        from_addr=e['from'],
        region=e.get('region', ''),
        smtp_host=e.get('smtp_host') or e.get('host') or '',
        smtp_port=int(e.get('smtp_port') or e.get('port') or 25),
        smtp_timeout=int(e.get('smtp_timeout') or e.get('timeout') or 20),
    )
    db_dsn = _compose_dsn(raw.get('db', {}))
    return Config(engine=engine, email=email, db_dsn=db_dsn, raw=raw)
