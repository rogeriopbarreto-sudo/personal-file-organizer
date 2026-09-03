"""Hook pós-rename: avisa o worker do dashboard de gastos.

Só vale para a **Pasta 04** (extratos e faturas de banco, uma subpasta por
banco). Depois que um arquivo dessa pasta fica com o nome final, o worker é
avisado para reprocessar o PDF e atualizar o dashboard.

Três regras que não mudam:

- **Nunca quebra o rename.** Qualquer falha de rede vira log + aviso no
  Telegram; a exceção não sobe.
- **Nunca avisa duas vezes.** A chave é `file_id + md5`, guardada no mesmo
  cache dos arquivos processados, então restart não re-notifica. Se o conteúdo
  do arquivo mudar, o md5 muda e o worker é avisado de novo — que é o
  comportamento certo: o dashboard precisa do dado novo.
- **Desligado por padrão.** Sem `GASTOS_PROCESS_SECRET` o hook é no-op, com uma
  linha de log no boot.
"""
from __future__ import annotations

import json
import logging
import urllib.request

from . import notifier
from .config import settings
from .state import get_state_manager

log = logging.getLogger("file_organizer.gastos")

CAMINHO = "/process"
TIMEOUT_S = 10


def habilitado() -> bool:
    """O hook só roda com URL e secret configurados."""
    return bool(settings.gastos_worker_url and settings.gastos_process_secret)


def log_configuracao() -> None:
    """Uma linha no boot dizendo se o hook está ligado."""
    if habilitado():
        log.info("Hook do worker de gastos ativo: %s", _url())
    else:
        log.info(
            "Hook do worker de gastos desligado "
            "(GASTOS_WORKER_URL/GASTOS_PROCESS_SECRET não configurados)"
        )


def _url() -> str:
    return settings.gastos_worker_url.rstrip("/") + CAMINHO


def _abrir_url(requisicao: urllib.request.Request):
    """Isolado para o teste substituir sem mexer no urllib global."""
    return urllib.request.urlopen(requisicao, timeout=TIMEOUT_S)


def _postar(payload: dict) -> None:
    """POST no worker. Lança se a rede falhar ou o status não for 2xx."""
    requisicao = urllib.request.Request(
        _url(),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Process-Secret": settings.gastos_process_secret,
        },
    )
    with _abrir_url(requisicao) as resposta:
        status = getattr(resposta, "status", None) or resposta.getcode()
        if status >= 300:
            raise RuntimeError(f"HTTP {status}")


def avisar_worker(
    folder_num: int,
    banco: str | None,
    file_id: str,
    nome: str,
    parent_folder_id: str,
    md5: str = "",
    simulacao: bool = False,
) -> bool:
    """Avisa o worker de gastos, se for o caso. Nunca lança exceção.

    Retorna True só quando o POST foi feito e aceito agora.
    """
    # Fora da Pasta 04, ou na raiz dela (sem banco), não há o que avisar.
    if folder_num != 4 or not banco:
        return False
    if not habilitado():
        return False
    if simulacao:
        log.info("[DRY_RUN] Não avisaria o worker de gastos: %s", nome)
        return False

    gerenciador = get_state_manager()
    if gerenciador.ja_notificou_gastos(file_id, md5):
        log.debug("Worker de gastos já avisado sobre %s", nome)
        return False

    payload = {
        "file_id": file_id,
        "name": nome,
        "bank_folder": banco,
        "parent_folder_id": parent_folder_id,
    }
    try:
        _postar(payload)
    except Exception as e:
        # O rename já aconteceu; o dashboard é que fica atrasado.
        log.error("Worker de gastos não respondeu para %s: %s", nome, e)
        try:
            notifier.notificar_gastos_falhou(nome, str(e))
        except Exception:
            log.exception("Falha ao avisar no Telegram sobre o worker de gastos")
        return False

    gerenciador.marcar_gastos_notificado(file_id, md5)
    log.info("Worker de gastos avisado: %s (%s)", nome, banco)
    return True
