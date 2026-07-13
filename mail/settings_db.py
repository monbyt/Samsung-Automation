"""
App-wide key/value settings — used for Knox Mail API credentials.
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


KNOX_KEYS = (
    "knox_mail_api_base",
    "knox_mail_bearer_token",
    "knox_mail_system_id",
    "knox_mail_sender_user_id",
    "knox_mail_sender_email",
)

DEFAULTS = {
    "knox_mail_api_base": "https://openapi.stage.samsung.net/mail/api/v2.0",
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


def get_knox_config() -> dict:
    return {k: get_setting(k) for k in KNOX_KEYS}


def is_knox_configured() -> bool:
    cfg = get_knox_config()
    required = ("knox_mail_bearer_token", "knox_mail_system_id",
                "knox_mail_sender_user_id", "knox_mail_sender_email")
    return all(cfg.get(k) for k in required)
