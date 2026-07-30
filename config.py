"""Configuração via variáveis de ambiente (Coolify)."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _bool_env(nome: str, padrao: bool = False) -> bool:
    return os.environ.get(nome, str(padrao)).strip().lower() in ("true", "1", "yes", "sim")


@dataclass(frozen=True)
class Settings:
    """Carrega todas as env vars necessárias no startup."""

    # Google Drive API (service account)
    google_service_account_json: str = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")

    # IDs das pastas do Drive
    drive_folder_01: str = os.environ.get("DRIVE_FOLDER_01", "")  # BTG Notas de Corretagem
    drive_folder_02: str = os.environ.get("DRIVE_FOLDER_02", "")  # Relatório de Performance
    drive_folder_03: str = os.environ.get("DRIVE_FOLDER_03", "")  # Extrato Investimentos
    drive_folder_04: str = os.environ.get("DRIVE_FOLDER_04", "")  # Extratos Banking (raiz)

    # Anthropic API (fallback quando o parser determinístico não fecha)
    anthropic_api_key: str = os.environ.get("ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
    usar_llm_fallback: bool = _bool_env("USAR_LLM_FALLBACK", True)

    # Telegram
    telegram_bot_token: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.environ.get("TELEGRAM_CHAT_ID", "")

    # Webhook do Drive (precisa ser HTTPS e acessível da internet)
    webhook_base_url: str = os.environ.get("WEBHOOK_BASE_URL", "")
    webhook_token: str = os.environ.get("WEBHOOK_TOKEN", "")
    # O Drive dispara várias notificações por upload; espera esse tempo para
    # agrupar a rajada numa única varredura.
    webhook_debounce_seconds: float = float(os.environ.get("WEBHOOK_DEBOUNCE_SECONDS", "3"))

    # Modo seco: loga o que faria, sem renomear nada no Drive
    dry_run: bool = _bool_env("DRY_RUN", False)

    OBRIGATORIAS = (
        "google_service_account_json",
        "drive_folder_01",
        "drive_folder_02",
        "drive_folder_03",
        "drive_folder_04",
        "telegram_bot_token",
        "telegram_chat_id",
        "webhook_base_url",
        "webhook_token",
    )

    def validar(self) -> list[str]:
        """Lista de problemas de configuração (vazia = tudo certo)."""
        problemas = [
            campo.upper() for campo in self.OBRIGATORIAS if not getattr(self, campo)
        ]
        # O Google recusa registrar canal em endereço que não seja HTTPS.
        if self.webhook_base_url and not self.webhook_base_url.startswith("https://"):
            problemas.append("WEBHOOK_BASE_URL precisa começar com https://")
        return problemas


settings = Settings()
