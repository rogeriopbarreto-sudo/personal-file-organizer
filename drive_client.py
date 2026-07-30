"""Acesso ao Google Drive via service account: webhook + listagem/download/rename de PDFs.

Padrão reaproveitado de `03_Passagens Aereas/watcher/app/drive.py`.
A pasta monitorada precisa estar compartilhada como **Editor** com o e-mail do
service account — o rename exige permissão de escrita.
"""
from __future__ import annotations

import io
import json
import logging
import uuid
from dataclasses import dataclass

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from .config import settings

log = logging.getLogger("file_organizer.drive")

SCOPES = ["https://www.googleapis.com/auth/drive"]


def _service():
    """Constrói o client de Drive API com credenciais do service account."""
    info = json.loads(settings.google_service_account_json)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


@dataclass
class Channel:
    """Representa um canal de notificação de webhook do Google Drive."""

    id: str
    resource_id: str
    expiration_ms: int


@dataclass
class DriveFile:
    """Representa um arquivo no Drive."""

    id: str
    name: str
    mime_type: str
    created_time: str


def get_start_page_token() -> str:
    """Obtém o page token inicial para começar a assistir mudanças."""
    svc = _service()
    return svc.changes().getStartPageToken().execute()["startPageToken"]


def start_watch(page_token: str) -> Channel:
    """Registra um canal de notificação para mudanças no Drive a partir de page_token."""
    svc = _service()
    channel_id = str(uuid.uuid4())
    body = {
        "id": channel_id,
        "type": "web_hook",
        "address": f"{settings.webhook_base_url}/drive-webhook",
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
    """Para um canal de notificação."""
    svc = _service()
    svc.channels().stop(body={"id": channel.id, "resourceId": channel.resource_id}).execute()
    log.info("Canal %s parado", channel.id)


def list_new_files_since_token(
    page_token: str, folder_ids: list[str]
) -> tuple[list[dict], str]:
    """Lista arquivos novos/alterados nas pastas especificadas desde page_token.

    Retorna (lista de {id, name, folder_id}, novo_page_token).
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
                fields="nextPageToken,newStartPageToken,changes(fileId,removed,file(id,name,parents,mimeType,trashed))",
            )
            .execute()
        )
        for change in resp.get("changes", []):
            f = change.get("file")
            if not f or change.get("removed") or f.get("trashed"):
                continue
            if f.get("mimeType") != "application/pdf":
                continue
            # Verificar se está em uma das pastas monitoradas
            file_parents = f.get("parents") or []
            for folder_id in folder_ids:
                if folder_id in file_parents:
                    novos.append({"id": f["id"], "name": f["name"], "folder_id": folder_id})
                    break
        if "nextPageToken" in resp:
            token = resp["nextPageToken"]
        else:
            return novos, resp["newStartPageToken"]
    return novos, page_token


def list_files_in_folder(folder_id: str) -> list[DriveFile]:
    """Lista todos os arquivos PDF (não deletados, não na lixeira) dentro de uma pasta.

    Retorna lista de DriveFile ordenada por created_time (mais antigos primeiro).
    """
    if not folder_id:
        log.warning("folder_id vazio, pulando listagem")
        return []

    svc = _service()
    files = []
    page_token = None

    try:
        while True:
            resp = (
                svc.files()
                .list(
                    q=f"'{folder_id}' in parents and trashed=false",
                    spaces="drive",
                    fields="nextPageToken,files(id,name,mimeType,createdTime)",
                    pageSize=100,
                    pageToken=page_token,
                )
                .execute()
            )

            for f in resp.get("files", []):
                if f.get("mimeType") == "application/pdf":
                    files.append(
                        DriveFile(
                            id=f["id"],
                            name=f["name"],
                            mime_type=f["mimeType"],
                            created_time=f.get("createdTime", ""),
                        )
                    )

            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    except Exception as e:
        log.error("Erro ao listar pasta %s: %s", folder_id, e)
        return []

    # Ordenar por created_time (mais antigos primeiro)
    files.sort(key=lambda x: x.created_time)
    return files


def download_pdf(file_id: str) -> bytes:
    """Baixa conteúdo do PDF em bytes."""
    svc = _service()
    request = svc.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def rename_file(file_id: str, novo_nome: str) -> None:
    """Renomeia o arquivo no Drive."""
    svc = _service()
    try:
        svc.files().update(fileId=file_id, body={"name": novo_nome}).execute()
        log.info("Arquivo %s renomeado para %s", file_id, novo_nome)
    except Exception as e:
        log.error("Erro ao renomear arquivo %s: %s", file_id, e)
        raise
