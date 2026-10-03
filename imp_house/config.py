import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


class Secret:
    def __init__(self, value: str) -> None:
        self._value = value

    def get(self) -> str:
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __repr__(self) -> str:
        return "Secret('**********')"

    __str__ = __repr__


@dataclass(frozen=True)
class Settings:
    database_url: str
    secret_key: Secret
    replicate_api_key: Secret
    camera_url: str
    data_dir: Path
    timezone: ZoneInfo
    cookie_secure: bool
    session_days: int = 7
    replicate_base_url: str = "https://api.replicate.com/v1"
    static_dir: Path | None = field(default=None)


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@lru_cache
def get_settings() -> Settings:
    load_dotenv(override=False)
    secret_key = os.environ.get("SECRET_KEY", "")
    if len(secret_key) < 32:
        raise RuntimeError("SECRET_KEY must be set and at least 32 characters long")
    static_dir = os.environ.get("STATIC_DIR")
    return Settings(
        database_url=os.environ["DATABASE_URL"],
        secret_key=Secret(secret_key),
        replicate_api_key=Secret(os.environ.get("REPLICATE_API_KEY", "")),
        camera_url=os.environ.get("CAMERA_URL", "http://192.168.178.94:81/stream"),
        data_dir=Path(os.environ.get("DATA_DIR", "./data")).resolve(),
        timezone=ZoneInfo(os.environ.get("TZ", "Europe/Berlin")),
        cookie_secure=_bool(os.environ.get("COOKIE_SECURE")),
        static_dir=Path(static_dir).resolve() if static_dir else None,
    )
