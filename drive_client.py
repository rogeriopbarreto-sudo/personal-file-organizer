"""Acesso ao Google Drive via service account: listagem, download e rename de PDFs.

Padrão reaproveitado de `03_Passagens Aereas/watcher/app/drive.py`.
A pasta monitorada precisa estar compartilhada como **Editor** com o e-mail do
service account — o rename exige permissão de escrita.
"""
from __future__ import annotations

import io
import json
import logging
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
class DriveFile:
    """Representa um arquivo no Drive."""

    id: str
    name: str
    mime_type: str
    created_time: str


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
