"""FastAPI: recebe o webhook do Google Drive e renomeia os PDFs novos.

Fluxo: alguém sobe um PDF → o Drive chama POST /drive-webhook → listamos as
mudanças desde o último page_token → cada PDF novo dentro das pastas monitoradas
é lido, parseado e renomeado (ou vira aviso no Telegram).

Não há agendamento: só roda quando o Drive avisa (ou no boot, que registra o
canal de notificação).

Detalhe importante: o Drive dispara VÁRIAS notificações por upload. Por isso as
varreduras são serializadas por um lock e agrupadas por um debounce — sem isso
uma dúzia de tarefas concorrentes processa o mesmo arquivo e enche o Telegram.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import traceback
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Response

from . import drive_client as drive
from . import notifier, state
from .config import settings
from .llm_fallback import completar_campos
from .parser import PdfProtegido, determinar_nome_novo, valida_padrão_final
from .state import get_state_manager

logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
log = logging.getLogger("file_organizer.main")

# Renova o canal com folga antes de expirar (o Drive dá ~7 dias).
RENOVAR_COM_ANTECEDENCIA_MS = 12 * 60 * 60 * 1000
INTERVALO_CHECAGEM_CANAL_S = 60 * 60


class Estado:
    """Estado compartilhado do processo."""

    def __init__(self) -> None:
        self.page_token: str | None = None
        self.channel: drive.Channel | None = None
        self.lock = asyncio.Lock()
        self.pendente = False
        self.completa_pendente = False
        self.varreduras = 0
        self.renomeados = 0
        self.ultimo_erro: str | None = None


estado = Estado()


# ============================================================================
# Mapa das pastas monitoradas
# ============================================================================


def _mapa_pastas() -> dict[str, tuple[int, str | None]]:
    """folder_id → (número da pasta, nome do banco).

    A Pasta 04 guarda os extratos em subpastas por banco (BTG, Itau), então o
    banco vem da subpasta — não de heurística no nome do arquivo. A lista é
    relida a cada varredura, então um banco novo passa a funcionar sozinho.
    """
    mapa: dict[str, tuple[int, str | None]] = {}
    for numero, folder_id in (
        (1, settings.drive_folder_01),
        (2, settings.drive_folder_02),
        (3, settings.drive_folder_03),
        (4, settings.drive_folder_04),
    ):
        if folder_id:
            mapa[folder_id] = (numero, None)

    for subpasta in drive.listar_subpastas(settings.drive_folder_04):
        mapa[subpasta.id] = (4, subpasta.name)

    return mapa


# ============================================================================
# Processamento de um arquivo
# ============================================================================


def _processar(
    file_id: str, nome_atual: str, pasta_id: str, numero: int, banco: str | None
) -> bool:
    """Processa um arquivo. Retorna True se renomeou (ou simulou o rename)."""
    gerenciador = get_state_manager()
    simulacao = settings.dry_run

    # Já está no padrão final → nada a fazer (idempotência).
    if valida_padrão_final(numero, nome_atual):
        log.info("Já no padrão final, ignorando: %s", nome_atual)
        gerenciador.registrar(
            file_id, nome_atual, numero, state.SUCESSO, motivo="já no padrão"
        )
        return False

    if numero == 4 and not banco:
        log.warning("Arquivo na raiz da Pasta 04, sem banco: %s", nome_atual)
        notifier.notificar_banco_desconhecido(nome_atual)
        gerenciador.registrar(
            file_id, nome_atual, numero, state.SEM_DADOS, motivo="banco desconhecido"
        )
        return False

    log.info("Processando %s (pasta %d%s)", nome_atual, numero, f"/{banco}" if banco else "")

    try:
        pdf = drive.download_pdf(file_id)
    except Exception as e:
        log.exception("Falha ao baixar %s", nome_atual)
        gerenciador.registrar(file_id, nome_atual, numero, state.ERRO, motivo=str(e)[:200])
        notifier.notificar_erro(f"download de {nome_atual}", str(e))
        return False

    try:
        resultado = determinar_nome_novo(
            numero, banco, nome_atual, pdf, completar=completar_campos
        )
    except PdfProtegido:
        log.warning("PDF protegido por senha: %s", nome_atual)
        notifier.notificar_pdf_protegido(nome_atual, numero)
        gerenciador.registrar(
            file_id, nome_atual, numero, state.PROTEGIDO, motivo="PDF exige senha"
        )
        return False
    except Exception as e:
        log.exception("Falha ao parsear %s", nome_atual)
        gerenciador.registrar(file_id, nome_atual, numero, state.ERRO, motivo=str(e)[:200])
        notifier.notificar_erro(f"parsing de {nome_atual}", str(e))
        return False

    if resultado.nome is None:
        log.warning("Nenhum campo reconhecido em %s", nome_atual)
        notifier.notificar_arquivo_sem_dados(nome_atual, numero)
        gerenciador.registrar(
            file_id, nome_atual, numero, state.SEM_DADOS, motivo="nenhum campo reconhecido"
        )
        return False

    if resultado.nome == nome_atual:
        gerenciador.registrar(file_id, nome_atual, numero, state.SUCESSO, motivo="nome já correto")
        return False

    # Nunca sobrescreve: se o destino existe, ganha sufixo (2), (3)...
    nome_final = drive.nome_sem_colisao(pasta_id, resultado.nome, ignorar_id=file_id)

    if simulacao:
        log.info("[DRY_RUN] Renomearia: %s → %s", nome_atual, nome_final)
    else:
        drive.rename_file(file_id, nome_final)
        log.info("Renomeado: %s → %s", nome_atual, nome_final)

    if resultado.campos_faltando:
        notifier.notificar_campos_faltando(
            nome_atual, nome_final, numero, resultado.campos_faltando, simulacao
        )
        status = state.INCOMPLETO
    else:
        notifier.notificar_renomeado(nome_atual, nome_final, numero, simulacao)
        status = state.SUCESSO

    gerenciador.registrar(
        file_id,
        nome_atual,
        numero,
        status,
        motivo=", ".join(resultado.campos_faltando),
        nome_novo=nome_final,
        dry_run=simulacao,
    )
    return True


# ============================================================================
# Varredura (síncrona — roda numa thread, serializada pelo lock)
# ============================================================================


def _varrer_tudo(mapa: dict[str, tuple[int, str | None]]) -> None:
    """Percorre as pastas inteiras, não só as mudanças recentes.

    Roda no boot: as notificações do Drive só valem a partir do momento em que o
    canal é registrado, então um arquivo que chegou com o serviço fora do ar
    nunca seria visto. Arquivos já no padrão final são descartados antes de
    qualquer download, então a varredura é barata.
    """
    gerenciador = get_state_manager()
    log.info("Varredura completa das pastas monitoradas")

    for pasta_id, (numero, banco) in mapa.items():
        for arquivo in drive.listar_pdfs(pasta_id):
            if valida_padrão_final(numero, arquivo.name):
                continue
            if not gerenciador.precisa_processar(arquivo.id, settings.dry_run):
                continue
            try:
                if _processar(arquivo.id, arquivo.name, pasta_id, numero, banco):
                    estado.renomeados += 1
            except Exception as e:
                estado.ultimo_erro = traceback.format_exc()
                log.exception("Erro inesperado em %s", arquivo.name)
                notifier.notificar_erro(arquivo.name, str(e))


def _varrer(completa: bool = False) -> None:
    """Lê as mudanças desde o último page_token e processa os PDFs novos."""
    gerenciador = get_state_manager()
    mapa = _mapa_pastas()

    if completa:
        _varrer_tudo(mapa)

    novos, proximo_token = drive.listar_mudancas(estado.page_token)
    estado.page_token = proximo_token
    estado.varreduras += 1

    if not novos:
        log.debug("Varredura sem PDFs novos")
        return

    ja_vistos: set[str] = set()
    for arquivo in novos:
        file_id = arquivo["id"]
        if file_id in ja_vistos:
            continue
        ja_vistos.add(file_id)

        # Em qual pasta monitorada esse arquivo está?
        destino = next(
            ((pai, mapa[pai]) for pai in arquivo["parents"] if pai in mapa), None
        )
        if destino is None:
            continue
        pasta_id, (numero, banco) = destino

        if not gerenciador.precisa_processar(file_id, settings.dry_run):
            log.debug("Já processado, ignorando: %s", arquivo["name"])
            continue

        try:
            if _processar(file_id, arquivo["name"], pasta_id, numero, banco):
                estado.renomeados += 1
        except Exception as e:
            estado.ultimo_erro = traceback.format_exc()
            log.exception("Erro inesperado em %s", arquivo["name"])
            notifier.notificar_erro(arquivo["name"], str(e))


async def _disparar_varredura(completa: bool = False) -> None:
    """Agenda uma varredura, agrupando a rajada de notificações do Drive.

    O Drive manda várias notificações por upload. O lock garante uma varredura
    por vez e o debounce junta a rajada; se chegar aviso durante a varredura, o
    laço roda de novo. Todo pedido passa por aqui — inclusive a varredura
    completa do boot — para nunca haver duas varreduras simultâneas.
    """
    if completa:
        estado.completa_pendente = True
    estado.pendente = True
    if estado.lock.locked():
        return  # quem está com o lock vai enxergar o pedido e rodar de novo

    async with estado.lock:
        while estado.pendente:
            await asyncio.sleep(settings.webhook_debounce_seconds)
            # A partir daqui, avisos novos pedem outra rodada.
            estado.pendente = False
            fazer_completa = estado.completa_pendente
            estado.completa_pendente = False
            try:
                await asyncio.to_thread(_varrer, fazer_completa)
            except Exception as e:
                estado.ultimo_erro = traceback.format_exc()
                log.exception("Erro na varredura")
                notifier.notificar_erro("varredura", str(e))


# ============================================================================
# Ciclo de vida
# ============================================================================


async def _renovar_canal_periodicamente() -> None:
    """Renova o canal do Drive antes de expirar."""
    while True:
        await asyncio.sleep(INTERVALO_CHECAGEM_CANAL_S)
        if not estado.channel or not estado.page_token:
            continue
        restante = estado.channel.expiration_ms - int(time.time() * 1000)
        if restante >= RENOVAR_COM_ANTECEDENCIA_MS:
            continue
        log.info("Renovando canal do Drive (expira em %d ms)", restante)
        try:
            await asyncio.to_thread(drive.stop_watch, estado.channel)
        except Exception:
            log.exception("Falha ao encerrar o canal antigo (seguindo mesmo assim)")
        try:
            estado.channel = await asyncio.to_thread(drive.start_watch, estado.page_token)
        except Exception as e:
            log.exception("Falha ao renovar o canal")
            notifier.notificar_erro("renovação do canal", str(e))


@asynccontextmanager
async def lifespan(app: FastAPI):
    problemas = settings.validar()
    if problemas:
        log.error("Configuração incompleta: %s", ", ".join(problemas))
        notifier.notificar_erro_configuracao(problemas)
        yield
        return

    tarefa = None
    try:
        # O page_token é pego ANTES da varredura inicial para não perder nada
        # que chegue enquanto ela roda.
        estado.page_token = await asyncio.to_thread(drive.get_start_page_token)
        estado.channel = await asyncio.to_thread(drive.start_watch, estado.page_token)
        tarefa = asyncio.create_task(_renovar_canal_periodicamente())
        # Recupera o que tenha chegado com o serviço fora do ar.
        asyncio.create_task(_disparar_varredura(completa=True))
        log.info(
            "Organizador no ar — canal registrado, DRY_RUN=%s", settings.dry_run
        )
    except Exception as e:
        log.exception("Falha ao registrar o canal do Drive")
        notifier.notificar_erro("registro do canal do Drive", str(e))

    yield

    if tarefa:
        tarefa.cancel()
    if estado.channel:
        try:
            await asyncio.to_thread(drive.stop_watch, estado.channel)
        except Exception:
            log.warning("Não consegui encerrar o canal no shutdown")


app = FastAPI(title="personal-file-organizer", lifespan=lifespan)


# ============================================================================
# Endpoints
# ============================================================================


@app.get("/")
def health() -> dict:
    return {
        "status": "ok",
        "canal_ativo": estado.channel is not None,
        "dry_run": settings.dry_run,
        "varreduras": estado.varreduras,
        "renomeados": estado.renomeados,
        "arquivos_em_cache": len(get_state_manager().registros),
        "ultimo_erro": estado.ultimo_erro,
    }


@app.post("/drive-webhook")
async def drive_webhook(
    background_tasks: BackgroundTasks,
    x_goog_resource_state: str = Header(default=""),
    x_goog_channel_token: str = Header(default=""),
) -> Response:
    if settings.webhook_token and x_goog_channel_token != settings.webhook_token:
        raise HTTPException(status_code=403, detail="token inválido")

    # Ping de verificação do canal — nada a processar.
    if x_goog_resource_state == "sync":
        return Response(status_code=200)

    if estado.page_token is None:
        log.warning("Webhook recebido antes do page_token inicial — ignorando")
        return Response(status_code=503)

    background_tasks.add_task(_disparar_varredura)
    return Response(status_code=202)


@app.post("/varrer")
async def varrer_manualmente(
    background_tasks: BackgroundTasks,
    completa: bool = True,
    x_token: str = Header(default=""),
) -> dict:
    """Dispara uma varredura na mão (útil depois de trocar o DRY_RUN).

    Protegido pelo mesmo WEBHOOK_TOKEN:
        curl -X POST https://SEU-HOST/varrer -H "x-token: SEU_TOKEN"
    """
    if not settings.webhook_token or x_token != settings.webhook_token:
        raise HTTPException(status_code=403, detail="token inválido")
    if estado.page_token is None:
        raise HTTPException(status_code=503, detail="serviço ainda não inicializou")

    background_tasks.add_task(_disparar_varredura, completa)
    return {"agendado": True, "completa": completa}
