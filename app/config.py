from functools import lru_cache
from os import getenv


class Settings:
    def __init__(self) -> None:
        self.secret_key = getenv("APP_SECRET_KEY", "dev-secret-change-me")
        self.admin_username = getenv("APP_ADMIN_USERNAME", "admin")
        self.admin_password = getenv("APP_ADMIN_PASSWORD", "admin")
        self.database_path = getenv("APP_DATABASE_PATH", "data/paypanel.db")
        self.base_url = getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/")
        self.session_max_age = int(getenv("APP_SESSION_MAX_AGE", "28800"))


@lru_cache
def get_settings() -> Settings:
    return Settings()
