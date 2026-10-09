import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db2_host: str
    db2_port: int
    db2_database: str
    db2_user: str
    db2_password: str
    pool_min_size: int
    pool_max_size: int
    pool_acquire_timeout: float
    app_port: int
    log_level: str
    log_file: str | None = None

    @property
    def dsn(self) -> str:
        return (
            f"DATABASE={self.db2_database};HOSTNAME={self.db2_host};PORT={self.db2_port};"
            f"PROTOCOL=TCPIP;UID={self.db2_user};PWD={self.db2_password};"
        )


def load_settings() -> Settings:
    env = os.environ
    return Settings(
        db2_host=env.get("DB2_HOST", "localhost"),
        db2_port=int(env.get("DB2_PORT", "50000")),
        db2_database=env.get("DB2_DATABASE", "COMMERCE"),
        db2_user=env.get("DB2_USER", "db2inst1"),
        db2_password=env.get("DB2_PASSWORD", ""),
        pool_min_size=int(env.get("POOL_MIN_SIZE", "2")),
        pool_max_size=int(env.get("POOL_MAX_SIZE", "20")),
        pool_acquire_timeout=float(env.get("POOL_ACQUIRE_TIMEOUT", "0.05")),
        app_port=int(env.get("APP_PORT", "8000")),
        log_level=env.get("LOG_LEVEL", "INFO").upper(),
        log_file=env.get("LOG_FILE") or None,
    )
