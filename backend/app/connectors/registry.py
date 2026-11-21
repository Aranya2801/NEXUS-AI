"""Per-kind config validation and connector construction."""
from __future__ import annotations

from typing import Callable

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.connectors.base import SourceConnector
from app.connectors.google_drive import GoogleDriveConnector
from app.connectors.http import SourceHttp
from app.connectors.microsoft import SharePointConnector, TeamsConnector
from app.connectors.rest import RestConnector
from app.connectors.security import assert_public_url
from app.connectors.slack import SlackConnector
from app.connectors.sql_source import SqlConnector, check_dialect, check_select_only
from app.core.config import Settings
from app.core.exceptions import ValidationFailed

KINDS = ("google_drive", "sharepoint", "slack", "teams", "sql", "rest")
ClientFactory = Callable[[], httpx.AsyncClient | None]


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DriveCfg(_Cfg):
    folder_id: str | None = Field(default=None, pattern=r"^[\w-]{5,80}$")


class SharePointCfg(_Cfg):
    drive_id: str = Field(pattern=r"^[\w!.-]{5,120}$")


class SlackCfg(_Cfg):
    channels: list[str] = Field(min_length=1, max_length=50)


class TeamsCfg(_Cfg):
    team_id: str = Field(pattern=r"^[\w-]{5,80}$")
    channel_ids: list[str] = Field(min_length=1, max_length=50)


class _Declared(_Cfg):
    readers: list[str] | None = None      # sources with no per-row ACL: the admin declares who may read the data
    public: bool = False


class SqlCfg(_Declared):
    query: str = Field(min_length=6, max_length=4000)
    id_column: str = Field(pattern=r"^\w{1,64}$")
    text_columns: list[str] | None = None
    title_column: str | None = None
    updated_column: str | None = None


class RestCfg(_Declared):
    url: str = Field(max_length=1000)
    items_path: str | None = None
    id_field: str = "id"
    name_field: str | None = None
    text_fields: list[str] | None = None
    updated_field: str | None = None
    next_path: str | None = None


CONFIGS = {"google_drive": DriveCfg, "sharepoint": SharePointCfg, "slack": SlackCfg, "teams": TeamsCfg, "sql": SqlCfg, "rest": RestCfg}
SECRET_KEYS = {"google_drive": ("access_token",), "sharepoint": ("access_token",), "slack": ("bot_token",), "teams": ("access_token",),
               "sql": ("url",), "rest": ()}   # rest: optional `headers` secret (auth), validated below


def validate(kind: str, config: dict, secrets: dict, s: Settings) -> tuple[dict, dict]:
    """Returns (clean config, clean secrets) or raises ValidationFailed. Performs the static safety checks (SSRF, SELECT-only)."""
    if kind not in KINDS:
        raise ValidationFailed(f"Unknown connector kind '{kind}'")
    try:
        cfg = CONFIGS[kind](**config)
    except ValidationError as exc:
        raise ValidationFailed("Invalid connector config: " + "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())) from exc
    required = SECRET_KEYS[kind]
    missing = [k for k in required if not secrets.get(k)]
    if missing:
        raise ValidationFailed(f"Missing credential(s): {', '.join(missing)}")
    clean_secrets = {k: secrets[k] for k in required}
    if kind == "sql":
        check_select_only(cfg.query)
        check_dialect(clean_secrets["url"], s)
    if kind == "rest":
        assert_public_url(cfg.url, s)
        h = secrets.get("headers") or {}
        if not isinstance(h, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in h.items()):
            raise ValidationFailed("headers must be an object of strings")
        clean_secrets["headers"] = h
    return cfg.model_dump(), clean_secrets


def build(kind: str, config: dict, secrets: dict, s: Settings, client_factory: ClientFactory | None = None) -> SourceConnector:
    client = client_factory() if client_factory else None
    cap = s.connector_max_item_mb * 1024 * 1024

    def http(headers: dict[str, str]) -> SourceHttp:
        return SourceHttp(client, headers=headers, max_bytes=cap)

    allowed = s.allowed_extensions
    if kind == "google_drive":
        return GoogleDriveConnector(http({"Authorization": f"Bearer {secrets['access_token']}"}), config.get("folder_id"), allowed)
    if kind == "sharepoint":
        return SharePointConnector(http({"Authorization": f"Bearer {secrets['access_token']}"}), config["drive_id"], allowed)
    if kind == "teams":
        return TeamsConnector(http({"Authorization": f"Bearer {secrets['access_token']}"}), config["team_id"], config["channel_ids"])
    if kind == "slack":
        return SlackConnector(http({"Authorization": f"Bearer {secrets['bot_token']}"}), config["channels"])
    if kind == "sql":
        return SqlConnector(secrets["url"], config["query"], config["id_column"], config.get("text_columns"), config.get("title_column"),
                            config.get("updated_column"), config.get("readers"), config.get("public", False), s)
    return RestConnector(http(secrets.get("headers", {})), config["url"], config.get("items_path"), config["id_field"], config.get("name_field"),
                         config.get("text_fields"), config.get("updated_field"), config.get("next_path"), config.get("readers"),
                         config.get("public", False), s, allow_private=False)
