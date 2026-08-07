"""
App-wide key/value settings — used for the Samsung Agent (mail) API
and Samsung SSO login credentials (W1 / NERP / recorded RPA).
"""
import os

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

SSO_KEYS = (
    "sso_username",
    "sso_password",
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


def get_sso_username() -> str:
    """Samsung SSO username for W1 / NERP / recorded RPA logins."""
    stored = get_setting("sso_username")
    if stored:
        return stored
    import config
    return (getattr(config, "NERP_USERNAME", None) or "").strip()


def get_sso_password() -> str:
    """Samsung SSO password for W1 / NERP / recorded RPA logins."""
    stored = get_setting("sso_password")
    if stored:
        return stored
    import config
    return (getattr(config, "NERP_PASSWORD", None) or "").strip()


def save_sso_credentials(username: str, password: str, *, keep_password_if_blank: bool = True):
    """Persist SSO credentials to the settings DB and sync into .env."""
    username = (username or "").strip()
    password = password if password is not None else ""
    set_setting("sso_username", username)
    dotenv_updates = {"NERP_USERNAME": username}
    if password or not keep_password_if_blank:
        set_setting("sso_password", password.strip())
        dotenv_updates["NERP_PASSWORD"] = password.strip()
    _sync_dotenv(dotenv_updates)
    # Refresh in-process config so the next RPA run picks them up without restart.
    try:
        import config
        if username:
            config.NERP_USERNAME = username
        if password or not keep_password_if_blank:
            config.NERP_PASSWORD = password.strip()
    except Exception:
        pass


def _sync_dotenv(updates: dict):
    """Merge key=value pairs into the project .env (create if missing)."""
    import config

    path = os.path.join(config.BASE_DIR, ".env")
    existing: dict = {}
    order: list = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.rstrip("\n")
                if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                    order.append(("raw", line))
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                existing[key] = val.strip().strip('"').strip("'")
                order.append(("key", key))
    for k, v in updates.items():
        if not k:
            continue
        if k not in existing:
            order.append(("key", k))
        existing[k] = v
    lines = []
    seen = set()
    for kind, item in order:
        if kind == "raw":
            lines.append(item)
            continue
        if item in seen:
            continue
        seen.add(item)
        lines.append(f"{item}={existing.get(item, '')}")
    for k, v in existing.items():
        if k not in seen:
            lines.append(f"{k}={v}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")
