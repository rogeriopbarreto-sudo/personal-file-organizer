"""FastAPI: recebe webhook do Google Drive e processa organização de arquivos.

Fluxo: Drive detecta mudança → chama POST /drive-webhook → listamos as mudanças desde
o último page_token → processamos PDFs novos (renomear ou notificar) → registramos em state.

Não há agendamento: o único jeito de algo rodar aqui é o Drive avisar (ou o boot, que
registra o canal de notificação).
"""
from __future__ import annotations

import asyncio
import logging
import logging.config
import time
import traceback

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Response

from . import drive_client
from .config import settings
from .drive_client import Channel, download_pdf, rename_file
from .notifier import (
    notificar_arquivo_sem_dados,
    notificar_banco_novo,
    notificar_duplicata_suspeita,
    notificar_erro_autenticacao,
    notificar_pdf_protegido,
    notificar_sucesso_ciclo,
)
from .parser import (
    determinar_nome_novo,
    parse_pasta_04_banking_btg,
    parse_pasta_04_banking_itau,
    valida_padrão_final,
)
from .state import get_state_manager

# ============================================================================
# Logging
# ============================================================================

LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
)
log = logging.getLogger("file_organizer.main")

# ============================================================================
# FastAPI App
# ============================================================================

app = FastAPI(title="personal-file-organizer")

# Renovação de canal 12h antes de expirar
RENOVAR_COM_ANTECEDENCIA_MS = 12 * 60 * 60 * 1000


# ============================================================================
# Estado global
# ============================================================================

class AppState:
    """Estado compartilhado entre requests."""

    def __init__(self):
        self.page_token: str | None = None
        self.channel: Channel | None = None
        self.pastas_monitoradas = {
            1: settings.drive_folder_01,
            2: settings.drive_folder_02,
            3: settings.drive_folder_03,
            4: settings.drive_folder_04,
        }
        self.bancos_conhecidos = {
            "BTG": parse_pasta_04_banking_btg,
            "Itau": parse_pasta_04_banking_itau,
        }
        # Global lock pra evitar processamento paralelo do mesmo arquivo
        self.processando_ids: set[str] = set()


app_state = AppState()


# ============================================================================
# Processamento de arquivo (reutilizado do código antigo)
# ============================================================================


def processar_arquivo(file_id: str, file_name: str, folder_num: int) -> bool:
    """Processa um arquivo: baixa, parseia, renomeia (ou notifica).

    Retorna True se processado com sucesso, False se erro/ambíguo.
    """
    state_mgr = get_state_manager()

    # Se já visto, pular
    if state_mgr.ja_visto(file_id):
        log.debug("Arquivo %s já visto, pulando", file_name)
        return True

    # Checar se nome já está no padrão final (idempotência)
    if valida_padrão_final(folder_num, file_name):
        log.info("Arquivo %s já está no padrão final, pulando", file_name)
        state_mgr.registrar_sucesso(file_id, file_name, folder_num)
        return True

    log.info("Processando %s (pasta %d)", file_name, folder_num)

    try:
        # Baixar PDF
        pdf_bytes = download_pdf(file_id)
        if not pdf_bytes:
            log.warning("Falha ao baixar %s", file_name)
            return False

        if not isinstance(pdf_bytes, bytes):
            log.error("pdf_bytes não é bytes para %s (tipo: %s)", file_name, type(pdf_bytes))
            return False

        # Determinar nome novo
        if folder_num == 4:
            nome_banco = detectar_banco_pasta_04(file_name)
            if not nome_banco:
                log.warning("Não conseguiu detectar banco para %s", file_name)
                notificar_banco_novo("Desconhecido", file_name)
                state_mgr.registrar_notificado(file_id, file_name, folder_num, "Banco desconhecido")
                return False
        else:
            nome_banco = None

        try:
            nome_novo = determinar_nome_novo(folder_num, nome_banco, file_name, pdf_bytes)
        except Exception as e:
            log.error("Erro ao determinar nome novo para %s: %s", file_name, traceback.format_exc())
            notificar_arquivo_sem_dados(file_name, folder_num)
            state_mgr.registrar_notificado(file_id, file_name, folder_num, f"Erro: {str(e)}")
            return False

        if not nome_novo:
            log.warning("Arquivo %s sem dados reconhecidos (nome_novo é None/vazio)", file_name)
            notificar_arquivo_sem_dados(file_name, folder_num)
            state_mgr.registrar_notificado(file_id, file_name, folder_num, "Sem dados")
            return False

        # Checar duplicata (Pasta 03)
        if folder_num == 3:
            resultado = verificar_duplicata_pasta_03(file_id, file_name, nome_novo)
            if resultado == "duplicata":
                return False
            elif resultado == "mesmo_arquivo":
                state_mgr.registrar_sucesso(file_id, file_name, folder_num)
                return True

        # Renomear (se DRY_RUN, só logar)
        if settings.dry_run:
            log.info("[DRY_RUN] Teria renomeado: %s → %s", file_name, nome_novo)
        else:
            rename_file(file_id, nome_novo)
            log.info("Renomeado com sucesso: %s → %s", file_name, nome_novo)

        state_mgr.registrar_sucesso(file_id, file_name, folder_num)
        return True

    except Exception as e:
        log.error("Erro ao processar %s: %s", file_name, traceback.format_exc())
        return False


def detectar_banco_pasta_04(file_name: str) -> str | None:
    """Detecta banco pela heurística no nome do arquivo."""
    if "itau" in file_name.lower():
        return "Itau"
    elif "btg" in file_name.lower() or "banco" in file_name.lower():
        return "BTG"
    else:
        return None


def verificar_duplicata_pasta_03(file_id: str, file_name: str, nome_novo: str) -> str | None:
    """Verifica se arquivo é uma possível duplicata (Pasta 03 apenas)."""
    # TODO: implementar lógica de comparar tamanho/hash
    return "ok"


# ============================================================================
# Lifecycle
# ============================================================================


@app.on_event("startup")
async def startup() -> None:
    """Ao iniciar, registra o canal de notificação e pega o page_token inicial."""
    faltando = settings.validar()
    if faltando:
        log.error("Variáveis de ambiente faltando: %s — organizador não vai registrar o canal.", faltando)
        return

    try:
        app_state.page_token = drive_client.get_start_page_token()
        folder_ids = [
            app_state.pastas_monitoradas[i] for i in range(1, 5)
            if app_state.pastas_monitoradas[i]
        ]
        app_state.channel = drive_client.start_watch(app_state.page_token)
        asyncio.create_task(_renovar_canal_periodicamente())
        log.info("Organizador iniciado — canal de notificações registrado")
    except Exception as e:
        log.error("Falha ao iniciar: %s", traceback.format_exc())
        notificar_erro_autenticacao()


async def _renovar_canal_periodicamente() -> None:
    """Renova o canal de notificação a cada hora, se necessário."""
    while True:
        await asyncio.sleep(60 * 60)  # confere de hora em hora
        if not app_state.channel:
            continue
        restante = app_state.channel.expiration_ms - int(time.time() * 1000)
        if restante < RENOVAR_COM_ANTECEDENCIA_MS:
            log.info("Renovando canal do Drive (expira em %sms).", restante)
            try:
                drive_client.stop_watch(app_state.channel)
            except Exception:
                log.exception("Falha ao parar canal antigo (seguindo mesmo assim).")
            if app_state.page_token:
                app_state.channel = drive_client.start_watch(app_state.page_token)


# ============================================================================
# Endpoints
# ============================================================================


@app.get("/")
def health() -> dict:
    """Endpoint de health check."""
    return {
        "status": "ok",
        "canal_ativo": app_state.channel is not None,
        "dry_run": settings.dry_run,
    }


@app.post("/drive-webhook")
async def drive_webhook(
    background_tasks: BackgroundTasks,
    x_goog_resource_state: str = Header(default=""),
    x_goog_channel_token: str = Header(default=""),
) -> Response:
    """Webhook do Google Drive — recebe notificações de mudanças."""
    if settings.webhook_token and x_goog_channel_token != settings.webhook_token:
        raise HTTPException(status_code=403, detail="token inválido")

    if x_goog_resource_state == "sync":
        # Ping de verificação inicial — nada a processar
        return Response(status_code=200)

    # Processa mudanças em background
    background_tasks.add_task(_processar_mudancas)
    return Response(status_code=202)


async def _processar_mudancas() -> None:
    """Processa mudanças detectadas pelo webhook."""
    if app_state.page_token is None:
        log.warning("Webhook chegou sem page_token inicial — ignorando.")
        return

    try:
        folder_ids = [
            app_state.pastas_monitoradas[i] for i in range(1, 5)
            if app_state.pastas_monitoradas[i]
        ]
        novos, proximo_token = await asyncio.to_thread(
            drive_client.list_new_files_since_token, app_state.page_token, folder_ids
        )
        app_state.page_token = proximo_token

        if not novos:
            log.debug("Webhook recebido, nenhum PDF novo nas pastas monitoradas.")
            return

        log.info("Encontrados %d arquivos novos", len(novos))
        total_processados = 0
        total_renomeados = 0

        for arquivo in novos:
            file_id = arquivo["id"]
            file_name = arquivo["name"]
            folder_id = arquivo["folder_id"]

            # GLOBAL LOCK: pular se já está sendo processado por outra task
            if file_id in app_state.processando_ids:
                log.debug("Arquivo %s já está sendo processado (paralelo), pulando", file_name)
                continue

            # Marcar como sendo processado
            app_state.processando_ids.add(file_id)
            try:
                # Determinar pasta número (1-4)
                folder_num = None
                for num, fid in app_state.pastas_monitoradas.items():
                    if fid == folder_id:
                        folder_num = num
                        break

                if folder_num is None:
                    log.warning("Pasta desconhecida: %s", folder_id)
                    continue

                total_processados += 1
                if processar_arquivo(file_id, file_name, folder_num):
                    total_renomeados += 1
            finally:
                # Sempre remove do lock
                app_state.processando_ids.discard(file_id)

        # Notificar APENAS se renomeou algo com SUCESSO (não falhas com ??)
        # E APENAS se foi de verdade (estado_mgr marcou como sucesso, não notificado)
        if total_renomeados > 0:
            log.info("Notificando sucesso: %d renomeados de %d", total_renomeados, total_processados)
            await asyncio.to_thread(
                notificar_sucesso_ciclo, total_processados, total_renomeados
            )

    except Exception:
        log.exception("Erro ao processar mudanças")
        await asyncio.to_thread(notificar_erro_autenticacao)
