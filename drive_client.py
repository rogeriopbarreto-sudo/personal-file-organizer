"""Acesso ao Google Drive via service account: webhook, listagem, download e rename.

Padrão reaproveitado de `03_Passagens Aereas/watcher/app/drive.py`.
As pastas monitoradas precisam estar compartilhadas como **Editor** com o e-mail
do service account — o rename exige permissão de escrita.
"""
from __future__ import annotations

import io
import json
import logging
import re
import uuid
from dataclasses import dataclass

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from .config import settings

log = logging.getLogger("file_organizer.drive")

SCOPES = ["https://www.googleapis.com/auth/drive"]

MIME_PASTA = "application/vnd.google-apps.folder"
MIME_PDF = "application/pdf"
# A Pasta 04 também aceita extrato de conta corrente em CSV (export do
# internet banking, ex.: Bradesco) — só ela; as demais pastas são PDF-only.
# `text/plain` e `application/vnd.ms-excel` entram porque é o que o navegador
# (ou o Excel, se o arquivo passar por lá antes do upload) costuma carimbar
# num `.csv` — o Drive não normaliza pro `text/csv` "correto".
MIMES_CSV = ("text/csv", "application/csv", "text/plain", "application/vnd.ms-excel")


def _service():
    """Constrói o client da Drive API com credenciais do service account."""
    info = json.loads(settings.google_service_account_json)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _escapa(valor: str) -> str:
    """Escapa aspas simples e barras para uso dentro de uma query do Drive."""
    return valor.replace("\\", "\\\\").replace("'", "\\'")


@dataclass
class Channel:
    """Canal de notificação push do Google Drive."""

    id: str
    resource_id: str
    expiration_ms: int


@dataclass
class DriveFile:
    """Arquivo no Drive."""

    id: str
    name: str
    mime_type: str = ""
    created_time: str = ""
    # Muda quando o conteúdo muda — usado como chave de idempotência.
    md5: str = ""


# ============================================================================
# Webhook (push notifications)
# ============================================================================


def get_start_page_token() -> str:
    """Page token inicial para começar a observar mudanças."""
    return _service().changes().getStartPageToken().execute()["startPageToken"]


def start_watch(page_token: str) -> Channel:
    """Registra um canal de notificação para mudanças no Drive."""
    svc = _service()
    channel_id = str(uuid.uuid4())
    body = {
        "id": channel_id,
        "type": "web_hook",
        "address": f"{settings.webhook_base_url.rstrip('/')}/drive-webhook",
        "token": settings.webhook_token,
    }
    resp = svc.changes().watch(pageToken=page_token, body=body).execute()
    log.info("Canal Drive registrado: %s (expira em %s)", channel_id, resp.get("expiration"))
    return Channel(
        id=channel_id,
        resource_id=resp["resourceId"],
        expiration_ms=int(resp.get("expiration", 0)),
    )


def stop_watch(channel: Channel) -> None:
    """Encerra um canal de notificação."""
    _service().channels().stop(
        body={"id": channel.id, "resourceId": channel.resource_id}
    ).execute()
    log.info("Canal %s encerrado", channel.id)


def listar_mudancas(page_token: str) -> tuple[list[dict], str]:
    """Lista os PDFs/CSVs criados/alterados desde `page_token`.

    Retorna (lista de {id, name, parents, md5, mime_type}, próximo page_token).
    O filtro por pasta é feito por quem chama — aqui ainda não se sabe em qual
    pasta o arquivo caiu, então CSV passa (só interessa se cair na Pasta 04;
    quem chama descarta o resto).
    """
    svc = _service()
    novos: list[dict] = []
    token = page_token

    while token:
        resp = (
            svc.changes()
            .list(
                pageToken=token,
                spaces="drive",
                pageSize=200,
                fields=(
                    "nextPageToken,newStartPageToken,"
                    "changes(fileId,removed,"
                    "file(id,name,parents,mimeType,trashed,md5Checksum))"
                ),
            )
            .execute()
        )

        for mudanca in resp.get("changes", []):
            arquivo = mudanca.get("file")
            if not arquivo or mudanca.get("removed") or arquivo.get("trashed"):
                continue
            mime_type = arquivo.get("mimeType")
            if mime_type != MIME_PDF and mime_type not in MIMES_CSV:
                continue
            novos.append(
                {
                    "id": arquivo["id"],
                    "name": arquivo["name"],
                    "parents": arquivo.get("parents") or [],
                    "md5": arquivo.get("md5Checksum") or "",
                    "mime_type": mime_type or "",
                }
            )

        if "nextPageToken" in resp:
            token = resp["nextPageToken"]
        else:
            return novos, resp["newStartPageToken"]

    return novos, page_token


# ============================================================================
# Listagem
# ============================================================================


def listar_subpastas(folder_id: str) -> list[DriveFile]:
    """Subpastas diretas de uma pasta (usado para descobrir os bancos da Pasta 04)."""
    if not folder_id:
        return []
    try:
        resp = (
            _service()
            .files()
            .list(
                q=f"'{_escapa(folder_id)}' in parents and trashed=false "
                f"and mimeType='{MIME_PASTA}'",
                spaces="drive",
                fields="files(id,name)",
                pageSize=100,
            )
            .execute()
        )
    except Exception:
        log.exception("Falha ao listar subpastas de %s", folder_id)
        return []
    return [DriveFile(id=f["id"], name=f["name"]) for f in resp.get("files", [])]


def listar_pdfs(folder_id: str, incluir_csv: bool = False) -> list[DriveFile]:
    """Todos os PDFs (e, com `incluir_csv`, os CSVs) de uma pasta, mais antigos primeiro.

    `incluir_csv` é ligado só para a Pasta 04 (extrato de conta corrente em
    CSV) — as demais pastas continuam PDF-only.
    """
    if not folder_id:
        return []

    if incluir_csv:
        clausula_mime = (
            "(" + " or ".join(f"mimeType='{m}'" for m in (MIME_PDF,) + MIMES_CSV) + ")"
        )
    else:
        clausula_mime = f"mimeType='{MIME_PDF}'"

    svc = _service()
    arquivos: list[DriveFile] = []
    page_token = None
    try:
        while True:
            resp = (
                svc.files()
                .list(
                    q=f"'{_escapa(folder_id)}' in parents and trashed=false "
                    f"and {clausula_mime}",
                    spaces="drive",
                    fields=(
                        "nextPageToken,"
                        "files(id,name,mimeType,createdTime,md5Checksum)"
                    ),
                    pageSize=200,
                    pageToken=page_token,
                )
                .execute()
            )
            for f in resp.get("files", []):
                arquivos.append(
                    DriveFile(
                        id=f["id"],
                        name=f["name"],
                        mime_type=f.get("mimeType", ""),
                        created_time=f.get("createdTime", ""),
                        md5=f.get("md5Checksum", "") or "",
                    )
                )
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    except Exception:
        log.exception("Falha ao listar PDFs de %s", folder_id)
        return []

    arquivos.sort(key=lambda a: a.created_time)
    return arquivos


def nome_existe(folder_id: str, nome: str, ignorar_id: str | None = None) -> bool:
    """Diz se já existe um arquivo com esse nome exato na pasta."""
    try:
        resp = (
            _service()
            .files()
            .list(
                q=f"'{_escapa(folder_id)}' in parents and trashed=false "
                f"and name='{_escapa(nome)}'",
                spaces="drive",
                fields="files(id)",
                pageSize=10,
            )
            .execute()
        )
    except Exception:
        log.exception("Falha ao checar existência de '%s'", nome)
        # Assume que existe: é mais seguro gerar um sufixo do que sobrescrever.
        return True
    return any(f["id"] != ignorar_id for f in resp.get("files", []))


def nome_sem_colisao(
    folder_id: str, nome_desejado: str, ignorar_id: str | None = None, limite: int = 50
) -> str:
    """Devolve o nome desejado ou, se já existir, com sufixo ' (2)', ' (3)'...

    Nunca sobrescreve um arquivo existente — regra inegociável do projeto.
    """
    if not nome_existe(folder_id, nome_desejado, ignorar_id):
        return nome_desejado

    base, _, extensao = nome_desejado.rpartition(".")
    if not base:  # nome sem extensão
        base, extensao = nome_desejado, ""
    sufixo_ext = f".{extensao}" if extensao else ""

    # Se o nome já vier com "(N)", começa a contagem a partir dele.
    m = re.match(r"^(.*) \((\d+)\)$", base)
    if m:
        base, inicio = m.group(1), int(m.group(2)) + 1
    else:
        inicio = 2

    for n in range(inicio, inicio + limite):
        candidato = f"{base} ({n}){sufixo_ext}"
        if not nome_existe(folder_id, candidato, ignorar_id):
            log.info("Nome '%s' já existe — usando '%s'", nome_desejado, candidato)
            return candidato

    raise RuntimeError(f"Não achei nome livre para '{nome_desejado}' após {limite} tentativas")


# ============================================================================
# Download e rename
# ============================================================================


def download_pdf(file_id: str) -> bytes:
    """Baixa o conteúdo do arquivo (PDF ou, na Pasta 04, CSV) como bytes."""
    request = _service().files().get_media(fileId=file_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    concluido = False
    while not concluido:
        _, concluido = downloader.next_chunk()
    return buffer.getvalue()


def rename_file(file_id: str, novo_nome: str) -> None:
    """Renomeia o arquivo no Drive."""
    _service().files().update(fileId=file_id, body={"name": novo_nome}).execute()
    log.info("Arquivo %s renomeado para %s", file_id, novo_nome)
