"""Notificações via Telegram (padrão stdlib, sem dependências extras)."""
from __future__ import annotations

import logging
import urllib.parse
import urllib.request
from typing import Optional

from .config import settings

log = logging.getLogger("file_organizer.notifier")


def tg_send(mensagem: str, aviso: bool = False) -> bool:
    """Envia mensagem curta via Telegram.

    Retorna True se sucesso, False se falha (mas nunca lança exceção).
    Se aviso=True, usa emoji ⚠️ no prefixo.
    """
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        log.warning("Credenciais Telegram faltando, mensagem não será enviada")
        return False

    try:
        # Limitar a 4000 chars (limite do Telegram)
        if len(mensagem) > 4000:
            mensagem = mensagem[:3996] + "..."

        prefix = "⚠️ " if aviso else "✅ "
        texto = f"{prefix}{mensagem}"

        url = (
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
            f"?chat_id={settings.telegram_chat_id}&text={urllib.parse.quote(texto)}"
            f"&parse_mode=HTML"
        )

        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as response:
            status = response.status
            if status == 200:
                log.info("Mensagem Telegram enviada")
                return True
            else:
                log.warning("Telegram retornou status %d", status)
                return False

    except Exception as e:
        log.error("Erro ao enviar Telegram: %s", e)
        return False


def notificar_arquivo_sem_dados(nome_arquivo: str, folder_num: int) -> None:
    """Notifica quando um arquivo não teve nenhum campo reconhecido."""
    msg = (
        f"❌ Arquivo não processado (sem dados reconhecidos):\n"
        f"<b>{nome_arquivo}</b>\n"
        f"Pasta {folder_num}\n\n"
        f"Verifique manualmente no Drive e autorize o rename se necessário."
    )
    tg_send(msg, aviso=True)


def notificar_pdf_protegido(nome_arquivo: str, folder_num: int) -> None:
    """Notifica quando um PDF está protegido por senha."""
    msg = (
        f"🔒 PDF protegido por senha:\n"
        f"<b>{nome_arquivo}</b>\n"
        f"Pasta {folder_num}\n\n"
        f"Não é possível extrair o conteúdo. Remova a senha e tente novamente."
    )
    tg_send(msg, aviso=True)


def notificar_duplicata_suspeita(nome_arquivo: str, nome_existente: str, folder_num: int) -> None:
    """Notifica suspeita de duplicata (mesmo nome, diferente tamanho)."""
    msg = (
        f"📋 Possível duplicata:\n"
        f"Novo: <b>{nome_arquivo}</b>\n"
        f"Existente: <b>{nome_existente}</b>\n"
        f"Pasta {folder_num}\n\n"
        f"Nenhum arquivo foi renomeado. Verificar manualmente."
    )
    tg_send(msg, aviso=True)


def notificar_banco_novo(nome_banco: str, nome_arquivo: str) -> None:
    """Notifica quando aparece um banco novo sem adapter em Pasta 04."""
    msg = (
        f"🏦 Banco novo detectado (sem adapter):\n"
        f"<b>{nome_banco}</b>\n"
        f"Arquivo: <b>{nome_arquivo}</b>\n\n"
        f"É necessário implementar um parser específico para este banco."
    )
    tg_send(msg, aviso=True)


def notificar_erro_autenticacao() -> None:
    """Notifica quando há erro de autenticação com o Drive."""
    msg = (
        f"🚨 Erro de autenticação com Google Drive!\n\n"
        f"Verificar se o refresh token expirou ou as credenciais estão inválidas."
    )
    tg_send(msg, aviso=True)


def notificar_sucesso_ciclo(arquivos_processados: int, arquivos_renomeados: int) -> None:
    """Notifica sucesso ao final de um ciclo de polling."""
    if arquivos_renomeados > 0:
        msg = (
            f"✅ Ciclo concluído!\n"
            f"Processados: {arquivos_processados}\n"
            f"Renomeados: {arquivos_renomeados}"
        )
        tg_send(msg, aviso=False)
