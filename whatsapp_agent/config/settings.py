from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    # App
    APP_NAME: str = "WhatsApp Agent Bancario"
    DEBUG: bool = False

    # Groq (clasificador IA - GRATIS)
    GROQ_API_KEY: str

    # Meta / WhatsApp Business API
    META_PHONE_NUMBER_ID: str
    META_WHATSAPP_TOKEN: str
    META_VERIFY_TOKEN: str = "mi_token_verificacion_webhook"
    META_API_VERSION: str = "v20.0"

    # Base de datos PostgreSQL (Railway la da automáticamente)
    DATABASE_URL: str

    # Redis (Railway la da automáticamente)
    REDIS_URL: str = "redis://localhost:6379"

    # Mensaje por defecto
    MENSAJE_DEFAULT: str = (
        "Hola {nombre}, le contactamos con una propuesta especial para usted. "
        "¿Le gustaría conocer más detalles?"
    )

    # Límite de envíos por segundo
    RATE_LIMIT_POR_SEGUNDO: int = 10

    # Score mínimo para emitir alerta
    SCORE_ALERTA_MINIMO: int = 60

    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()
