from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    paypal_base_url: str = "https://api-m.sandbox.paypal.com"
    demo_recipient_email: str = ""

    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

    mongodb_uri: str = "mongodb://localhost:27017"
    mongodb_db: str = "flowpilot"

    frontend_origin: str = "http://localhost:5173"

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.frontend_origin.split(",") if o.strip()]


settings = Settings()
