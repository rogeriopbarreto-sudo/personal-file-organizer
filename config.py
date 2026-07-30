"""Configuração via variáveis de ambiente (Coolify)."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Carrega todas as env vars necessárias no startup."""

    # Google Drive API (service account)
    google_service_account_json: str = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")

    # Pasta do Drive IDs
    drive_folder_01: str = os.environ.get("DRIVE_FOLDER_01", "")  # BTG Notas de Corretagem
    drive_folder_02: str = os.environ.get("DRIVE_FOLDER_02", "")  # Relatorio de Performance
    drive_folder_03: str = os.environ.get("DRIVE_FOLDER_03", "")  # Extrato Investimentos
    drive_folder_04: str = os.environ.get("DRIVE_FOLDER_04", "")  # BTG Extratos Banking (raiz)

    # Anthropic API (fallback LLM)
    anthropic_api_key: str = os.environ.get("ANTHROPIC_API_KEY", "")

    # Telegram
    telegram_bot_token: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.environ.get("TELEGRAM_CHAT_ID", "")

    # Webhook (Drive push notifications)
    webhook_base_url: str = os.environ.get("WEBHOOK_BASE_URL", "http://localhost:8000")
    webhook_token: str = os.environ.get("WEBHOOK_TOKEN", "")

    # Polling (legacy, mantém compatibilidade)
    poll_interval_seconds: int = int(os.environ.get("POLL_INTERVAL_SECONDS", "300"))

    # Modo seco (testa sem fazer rename real)
    dry_run: bool = os.environ.get("DRY_RUN", "false").lower() in ("true", "1", "yes")

    def validar(self) -> list[str]:
        """Retorna lista de variáveis obrigatórias que faltam."""
        faltando = []
        for campo in (
            "google_service_account_json",
            "drive_folder_01",
            "drive_folder_02",
            "drive_folder_03",
            "drive_folder_04",
            "anthropic_api_key",
            "telegram_bot_token",
            "telegram_chat_id",
            "webhook_base_url",
            "webhook_token",
        ):
            if not getattr(self, campo):
                faltando.append(campo.upper())
        return faltando


settings = Settings()
