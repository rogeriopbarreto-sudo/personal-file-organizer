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
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Response

from . import drive_client as drive
from . import gastos, notifier, state
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


@dataclass
class AvisoGastos:
    """Um aviso ao worker de gastos esperando o fim dos renames da varredura."""

    file_id: str
    nome: str
    pasta_id: str
    banco: str
    md5: str
    # Chamado com o desfecho (o worker conhece o arquivo nesse md5?).
    depois: Callable[[bool], None] | None = None


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
        # Alarme de "Pasta 04 sem subpastas de banco" já enviado: o webhook
        # dispara varreduras o tempo todo e o Telegram não pode repetir.
        self.alarme_subpastas = False
        # Enquanto a listagem das subpastas falha, mudanças nelas são puladas
        # e o page_token avança. Quando a listagem voltar, uma varredura
        # completa recupera o que passou.
        self.completa_ao_voltar = False
        # Avisos ao worker de gastos juntados durante uma varredura e enviados
        # no fim dela, ainda com o lock: a espera por um 409 não atrasa os
        # renames da mesma varredura, mas segura a próxima (ver `_varrer`).
        # None fora de uma varredura: o aviso sai na hora.
        self.avisos_adiados: list[AvisoGastos] | None = None


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

    if not settings.drive_folder_04:
        return mapa

    # Sem subpasta de banco nenhum extrato chega ao worker de gastos. Erro de
    # API ou lista vazia (ex.: a service account perdeu o acesso) viram um
    # alarme só, até as subpastas voltarem.
    try:
        subpastas = drive.listar_subpastas(settings.drive_folder_04)
        problema = "" if subpastas else "a listagem voltou vazia"
    except Exception as e:
        subpastas, problema = [], f"erro ao listar: {e}"

    if not problema:
        if estado.alarme_subpastas:
            log.info("Subpastas de banco da Pasta 04 visíveis de novo: %d", len(subpastas))
        estado.alarme_subpastas = False
    else:
        # Mudança numa subpasta invisível agora é pulada, e o page_token
        # avança: sem isso ela se perderia até o próximo deploy.
        estado.completa_ao_voltar = True
        if not estado.alarme_subpastas:
            estado.alarme_subpastas = True
            log.error("Pasta 04 sem subpastas de banco: %s", problema)
            notifier.notificar_pasta_04_sem_subpastas(problema)

    for subpasta in subpastas:
        mapa[subpasta.id] = (4, subpasta.name)

    return mapa


# ============================================================================
# Processamento de um arquivo
# ============================================================================


def _hook_gastos_desligado() -> str:
    """Por que o aviso ao worker de gastos não sai ('' se ele está ligado)."""
    if not gastos.habilitado():
        return "GASTOS_WORKER_URL/GASTOS_PROCESS_SECRET não configurados"
    if settings.dry_run:
        return "DRY_RUN"
    return ""


def _avisar_gastos(
    file_id: str,
    nome: str,
    pasta_id: str,
    numero: int,
    banco: str | None,
    md5: str,
    depois: Callable[[bool], None] | None = None,
) -> None:
    """Avisa o worker do dashboard de gastos (só Pasta 04, nunca quebra o fluxo).

    Precisa de um registro no cache para guardar o "já notificado" — arquivo que
    chegou pronto (nome já no padrão final) pode ainda não ter um.

    Dentro de uma varredura o aviso entra na fila e sai depois dos renames;
    fora dela, sai na hora. `depois` recebe o desfecho.
    """
    if numero != 4 or not banco or not gastos.habilitado():
        return
    try:
        gerenciador = get_state_manager()
        if gerenciador.get(file_id) is None:
            gerenciador.registrar(
                file_id, nome, numero, state.SUCESSO, motivo="já no padrão", md5=md5
            )
    except Exception:
        log.exception("Falha no hook do worker de gastos para %s", nome)
        return
    aviso = AvisoGastos(file_id, nome, pasta_id, banco, md5, depois)
    if estado.avisos_adiados is not None:
        estado.avisos_adiados.append(aviso)
    else:
        _enviar_aviso_gastos(aviso)


def _enviar_aviso_gastos(aviso: AvisoGastos) -> bool:
    """Faz o aviso de verdade. True se o worker conhece o arquivo nesse md5."""
    try:
        gastos.avisar_worker(
            4, aviso.banco, aviso.file_id, aviso.nome, aviso.pasta_id, aviso.md5,
            settings.dry_run,
        )
        enviado = get_state_manager().ja_notificou_gastos(aviso.file_id, aviso.md5)
    except Exception:
        log.exception("Falha no hook do worker de gastos para %s", aviso.nome)
        enviado = False
    if aviso.depois:
        try:
            aviso.depois(enviado)
        except Exception:
            log.exception("Falha depois do aviso ao worker de gastos: %s", aviso.nome)
    return enviado


def _enviar_avisos_adiados() -> None:
    """Esvazia a fila da varredura. Mesmo arquivo e md5 só é tentado uma vez."""
    avisos, estado.avisos_adiados = estado.avisos_adiados or [], None
    desfechos: dict[tuple[str, str], bool] = {}
    for aviso in avisos:
        chave = (aviso.file_id, aviso.md5)
        if chave not in desfechos:
            desfechos[chave] = _enviar_aviso_gastos(aviso)
        elif aviso.depois:
            aviso.depois(desfechos[chave])


def _processar(
    file_id: str,
    nome_atual: str,
    pasta_id: str,
    numero: int,
    banco: str | None,
    md5: str = "",
    mime_type: str = drive.MIME_PDF,
) -> bool:
    """Processa um arquivo. Retorna True se renomeou (ou simulou o rename).

    Na Pasta 04 (subpasta de banco) o worker de gastos é avisado em TODA saída:
    renomeado, sem dados, com senha, erro de download/leitura ou falha no
    rename. O worker baixa o arquivo, tem a senha do banco e não depende do
    nome — o rename daqui não é pré-requisito do aviso. Pastas 01–03 nunca avisam.
    """
    gerenciador = get_state_manager()
    simulacao = settings.dry_run

    # Já está no padrão final → nada a renomear (idempotência). O worker de
    # gastos ainda pode não conhecer o arquivo, então o hook roda mesmo assim.
    if valida_padrão_final(numero, nome_atual):
        log.info("Já no padrão final, ignorando: %s", nome_atual)
        gerenciador.registrar(
            file_id, nome_atual, numero, state.SUCESSO, motivo="já no padrão", md5=md5
        )
        _avisar_gastos(file_id, nome_atual, pasta_id, numero, banco, md5)
        return False

    if numero == 4 and not banco:
        log.warning("Arquivo na raiz da Pasta 04, sem banco: %s", nome_atual)
        notifier.notificar_banco_desconhecido(nome_atual)
        gerenciador.registrar(
            file_id,
            nome_atual,
            numero,
            state.SEM_DADOS,
            motivo=state.MOTIVO_BANCO_DESCONHECIDO,
            md5=md5,
        )
        return False

    log.info("Processando %s (pasta %d%s)", nome_atual, numero, f"/{banco}" if banco else "")

    try:
        pdf = drive.download_pdf(file_id)
    except Exception as e:
        log.exception("Falha ao baixar %s", nome_atual)
        gerenciador.registrar(
            file_id, nome_atual, numero, state.ERRO, motivo=str(e)[:200], md5=md5
        )
        notifier.notificar_erro(f"download de {nome_atual}", str(e))
        _avisar_gastos(file_id, nome_atual, pasta_id, numero, banco, md5)
        return False

    try:
        resultado = determinar_nome_novo(
            numero, banco, nome_atual, pdf, completar=completar_campos, mime_type=mime_type
        )
    except PdfProtegido:
        log.warning("PDF protegido por senha: %s", nome_atual)
        gerenciador.registrar(
            file_id,
            nome_atual,
            numero,
            state.PROTEGIDO,
            motivo="PDF exige senha",
            md5=md5,
        )
        if numero == 4 and banco:
            # Fica com o nome original; quem abre é o worker, com a senha do
            # banco. O Telegram sai com o desfecho real do aviso.
            desligado = _hook_gastos_desligado()
            if desligado:
                notifier.notificar_pdf_protegido_pasta_04(nome_atual, False, desligado)
            _avisar_gastos(
                file_id, nome_atual, pasta_id, numero, banco, md5,
                depois=None if desligado else (
                    lambda enviado: notifier.notificar_pdf_protegido_pasta_04(
                        nome_atual, enviado
                    )
                ),
            )
        else:
            notifier.notificar_pdf_protegido(nome_atual, numero)
        return False
    except Exception as e:
        log.exception("Falha ao parsear %s", nome_atual)
        gerenciador.registrar(
            file_id, nome_atual, numero, state.ERRO, motivo=str(e)[:200], md5=md5
        )
        notifier.notificar_erro(f"parsing de {nome_atual}", str(e))
        _avisar_gastos(file_id, nome_atual, pasta_id, numero, banco, md5)
        return False

    if resultado.nome is None:
        log.warning("Nenhum campo reconhecido em %s", nome_atual)
        notifier.notificar_arquivo_sem_dados(nome_atual, numero)
        gerenciador.registrar(
            file_id,
            nome_atual,
            numero,
            state.SEM_DADOS,
            motivo="nenhum campo reconhecido",
            md5=md5,
        )
        # Mesmo sem conseguir renomear (ex.: CSV sem a linha de período), o
        # worker de gastos ganha o file_id e tenta parsear por conta própria.
        _avisar_gastos(file_id, nome_atual, pasta_id, numero, banco, md5)
        return False

    if resultado.nome == nome_atual:
        gerenciador.registrar(
            file_id, nome_atual, numero, state.SUCESSO, motivo="nome já correto", md5=md5
        )
        _avisar_gastos(file_id, nome_atual, pasta_id, numero, banco, md5)
        return False

    try:
        # Nunca sobrescreve: se o destino existe, ganha sufixo (2), (3)...
        nome_final = drive.nome_sem_colisao(pasta_id, resultado.nome, ignorar_id=file_id)

        if simulacao:
            log.info("[DRY_RUN] Renomearia: %s → %s", nome_atual, nome_final)
        else:
            drive.rename_file(file_id, nome_final)
            log.info("Renomeado: %s → %s", nome_atual, nome_final)
    except Exception as e:
        # O rename falhou, mas o worker de gastos não precisa dele. O registro
        # guarda o "já avisado" e, como ERRO_RENAME, não impede a próxima
        # varredura de tentar o rename de novo. A exceção segue para quem
        # chamou (log, Telegram e /health, como antes).
        if numero == 4 and banco:
            gerenciador.registrar(
                file_id,
                nome_atual,
                numero,
                state.ERRO_RENAME,
                motivo=f"rename: {e}"[:200],
                md5=md5,
            )
            _avisar_gastos(file_id, nome_atual, pasta_id, numero, banco, md5)
        raise

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
        md5=md5,
    )
    # Extrato da Pasta 04 renomeado: o dashboard de gastos precisa saber.
    _avisar_gastos(file_id, nome_final, pasta_id, numero, banco, md5)
    return True


# ============================================================================
# Varredura (síncrona — roda numa thread, serializada pelo lock)
# ============================================================================


def _varrer_tudo(mapa: dict[str, tuple[int, str | None]]) -> bool:
    """Percorre as pastas inteiras, não só as mudanças recentes.

    Roda no boot: as notificações do Drive só valem a partir do momento em que o
    canal é registrado, então um arquivo que chegou com o serviço fora do ar
    nunca seria visto. Arquivos já no padrão final ou já registrados são
    descartados antes de qualquer download, então a varredura é barata —
    passam só pelo hook do worker de gastos, que é um teste em memória quando
    o aviso já foi dado.

    Retorna True só se TODAS as pastas foram listadas. Uma pasta que falha é
    pulada (log) e as outras seguem.
    """
    gerenciador = get_state_manager()
    log.info("Varredura completa das pastas monitoradas")
    listou_tudo = True

    for pasta_id, (numero, banco) in mapa.items():
        try:
            # Extrato de conta corrente em CSV só é aceito na Pasta 04.
            arquivos = drive.listar_pdfs(pasta_id, incluir_csv=(numero == 4))
        except Exception:
            listou_tudo = False
            continue  # o erro já foi logado; as outras pastas seguem
        for arquivo in arquivos:
            if valida_padrão_final(numero, arquivo.name) or not (
                gerenciador.precisa_processar(
                    arquivo.id, settings.dry_run, em_subpasta_de_banco=(numero == 4 and bool(banco))
                )
            ):
                # Nada a renomear — mas o worker de gastos pode ainda não
                # conhecer o arquivo: primeiro boot com o hook, PROTEGIDO ou
                # SEM_DADOS de antes, aviso que falhou, arquivo movido da raiz
                # para a subpasta (o banco é a subpasta ATUAL). Idempotente por
                # file_id + md5; no-op fora das subpastas da Pasta 04. Arquivo
                # que ainda vai a `_processar` é avisado lá, depois do rename.
                _avisar_gastos(
                    arquivo.id, arquivo.name, pasta_id, numero, banco, arquivo.md5
                )
                continue
            try:
                if _processar(
                    arquivo.id,
                    arquivo.name,
                    pasta_id,
                    numero,
                    banco,
                    arquivo.md5,
                    arquivo.mime_type,
                ):
                    estado.renomeados += 1
            except Exception as e:
                estado.ultimo_erro = traceback.format_exc()
                log.exception("Erro inesperado em %s", arquivo.name)
                notifier.notificar_erro(arquivo.name, str(e))

    return listou_tudo


def _varrer(completa: bool = False) -> None:
    """Lê as mudanças desde o último page_token e processa os PDFs novos.

    Os avisos ao worker de gastos são juntados durante a varredura e enviados
    no fim dela, ainda com o lock preso. O que isso garante: a espera por um
    worker ocupado (409) não atrasa os renames DESTA varredura, que saem todos
    antes do primeiro aviso. O que não garante: um arquivo que chega enquanto a
    fila espera só é processado na varredura seguinte, depois que ela esvaziar.
    Se a varredura quebrar, a fila já montada é enviada mesmo assim (finally).
    """
    estado.avisos_adiados = []
    try:
        _varrer_renomeando(completa)
    finally:
        _enviar_avisos_adiados()


def _varrer_renomeando(completa: bool) -> None:
    gerenciador = get_state_manager()
    mapa = _mapa_pastas()

    # A listagem das subpastas da Pasta 04 falhou numa varredura anterior e
    # voltou agora: o que mudou nelas nesse meio-tempo foi pulado.
    recuperacao = estado.completa_ao_voltar and not estado.alarme_subpastas
    if recuperacao:
        log.info("Subpastas da Pasta 04 de volta — varredura completa de recuperação")
        completa = True

    if completa:
        # A pendência só se desliga quando a varredura viu todas as pastas; se
        # alguma não listou (inclusive no boot), a próxima notificação refaz.
        if not _varrer_tudo(mapa):
            estado.completa_ao_voltar = True
        elif recuperacao:
            estado.completa_ao_voltar = False

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

        # CSV só interessa na Pasta 04 — em qualquer outra é descartado aqui
        # (o filtro de `listar_mudancas` é global, sem saber a pasta ainda).
        mime_type = arquivo.get("mime_type", drive.MIME_PDF)
        if numero != 4 and mime_type != drive.MIME_PDF:
            continue

        if not gerenciador.precisa_processar(
            file_id, settings.dry_run, em_subpasta_de_banco=(numero == 4 and bool(banco))
        ):
            log.debug("Já processado, ignorando: %s", arquivo["name"])
            # Mesmo motivo da varredura completa: o worker pode não ter
            # recebido (PROTEGIDO, SEM_DADOS, aviso que falhou, arquivo que
            # veio da raiz, conteúdo novo). No-op fora da Pasta 04.
            _avisar_gastos(
                file_id, arquivo["name"], pasta_id, numero, banco, arquivo.get("md5", "")
            )
            continue

        try:
            if _processar(
                file_id,
                arquivo["name"],
                pasta_id,
                numero,
                banco,
                arquivo.get("md5", ""),
                mime_type,
            ):
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
    gastos.log_configuracao()
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
