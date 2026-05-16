from dataclasses import dataclass


@dataclass
class Settings:
    db_path: str = ".greenfield.db"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8000


settings = Settings()
