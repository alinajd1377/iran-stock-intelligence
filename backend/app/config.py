from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    app_name: str = "Iran Stock Intelligence"
    app_env: str = "development"
    database_url: str = "postgresql+psycopg://isi:isi_password@db:5432/iran_stock_intelligence"
    codal_base_url: str = "https://search.codal.ir"
    tsetmc_base_url: str = "https://cdn.tsetmc.com/api"
    raw_data_dir: str = "/app/data/raw"
    admin_username: str = ""
    admin_password: str = ""
    admin_session_secret: str = ""
    admin_session_ttl_seconds: int = 28800
    admin_cookie_secure: bool = False
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
