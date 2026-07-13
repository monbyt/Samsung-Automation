"""
App-wide key/value settings — used for the Samsung Agent (mail) API.
"""
from sqlalchemy import Column, MetaData, String, Table, Text, select

from db import engine, init_db

metadata = MetaData()

app_settings = Table(
    "app_settings", metadata,
    Column("key", String(64), primary_key=True),
    Column("value", Text),
)


def _ensure_table():
    init_db()
    metadata.create_all(engine)


AGENT_KEYS = (
    "agent_api_url",
    "agent_api_key",
    "agent_mail_component_id",
)

DEFAULTS = {
    "agent_mail_component_id": "knox_portal_mail-1irUi",
}


def get_setting(key: str, default: str = "") -> str:
    _ensure_table()
    with engine.connect() as conn:
        row = conn.execute(select(app_settings.c.value).where(app_settings.c.key == key)).first()
    if row and row[0]:
        return row[0]
    return DEFAULTS.get(key, default)


def set_setting(key: str, value: str):
    _ensure_table()
    value = (value or "").strip()
    with engine.begin() as conn:
        existing = conn.execute(select(app_settings.c.key).where(app_settings.c.key == key)).first()
        if existing:
            conn.execute(app_settings.update().where(app_settings.c.key == key).values(value=value))
        else:
            conn.execute(app_settings.insert().values(key=key, value=value))


def get_agent_config() -> dict:
    return {k: get_setting(k) for k in AGENT_KEYS}


def is_agent_configured() -> bool:
    cfg = get_agent_config()
    required = ("agent_api_url", "agent_api_key", "agent_mail_component_id")
    return all(cfg.get(k) for k in required)
