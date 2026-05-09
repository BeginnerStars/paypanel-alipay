from functools import lru_cache
from os import environ, getenv
from pathlib import Path


def load_env_file(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        environ.setdefault(key, value)


class Settings:
    def __init__(self) -> None:
        load_env_file()
        self.secret_key = getenv("APP_SECRET_KEY", "dev-secret-change-me")
        self.admin_username = getenv("APP_ADMIN_USERNAME", "admin")
        self.admin_password = getenv("APP_ADMIN_PASSWORD", "admin")
        self.database_path = getenv("APP_DATABASE_PATH", "data/paypanel.db")
        self.base_url = getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/")
        self.session_max_age = int(getenv("APP_SESSION_MAX_AGE", "28800"))
        self.host = getenv("APP_HOST", "0.0.0.0")
        self.port = int(getenv("APP_PORT", "8000"))


@lru_cache
def get_settings() -> Settings:
    return Settings()
