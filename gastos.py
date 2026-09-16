"""Hook da Pasta 04: avisa o worker do dashboard de gastos.

Só vale para a **Pasta 04** (extratos e faturas de banco, uma subpasta por
banco). Todo arquivo de subpasta de banco é avisado — renomeado ou não, com
senha, sem dados reconhecidos ou com erro de leitura aqui —, porque o worker
baixa o arquivo sozinho, tem a senha de cada banco e não depende do nome.

Três regras que não mudam:

- **Nunca quebra o rename.** Qualquer falha de rede vira log + aviso no
  Telegram; a exceção não sobe.
- **Nunca avisa duas vezes.** A chave é `file_id + md5`, guardada no mesmo
  cache dos arquivos processados, então restart não re-notifica. Se o conteúdo
  do arquivo mudar, o md5 muda e o worker é avisado de novo — que é o
  comportamento certo: o dashboard precisa do dado novo.
- **Desligado por padrão.** Sem `GASTOS_PROCESS_SECRET` o hook é no-op, com uma
  linha de log no boot.

O worker roda um job por vez e responde **409** (`{"busy": true, "job_id"}`)
enquanto outro job segura o pipeline. Isso não é falha: o aviso espera o job
terminar (`finished_at` em `GET /jobs/{id}`) e tenta de novo, sem Telegram a
cada 409. Só quando estoura o teto de tentativas ou de tempo é que desiste — um
Telegram, e o arquivo segue não avisado até o próximo deploy ou `POST /varrer`.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from . import notifier
from .config import settings
from .state import get_state_manager

log = logging.getLogger("file_organizer.gastos")

CAMINHO = "/process"
CAMINHO_JOBS = "/jobs/"
TIMEOUT_S = 10

# Espera pelo worker ocupado (409). O teto de tempo fica acima dos 600 s que o
# worker dá a um job antes de devolver o pipeline, então um job travado não
# faz o aviso desistir; o de tentativas limita um 409 que nunca acaba.
ESPERA_OCUPADO_MAX_S = 11 * 60
TENTATIVAS_OCUPADO_MAX = 8
INTERVALO_OCUPADO_S = 5
BACKOFF_OCUPADO_MAX_S = 60


class WorkerOcupado(Exception):
    """409: outro job segura o pipeline do worker."""

    def __init__(self, job_id: str | None) -> None:
        super().__init__(f"HTTP 409 (job ativo: {job_id or 'desconhecido'})")
        self.job_id = job_id


def _dormir(segundos: float) -> None:
    """Isolado para o teste não esperar de verdade."""
    time.sleep(segundos)


def _agora() -> float:
    """Isolado para o teste controlar o relógio."""
    return time.monotonic()


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


def _job_id_da_resposta(resposta) -> str | None:
    """Tira o `job_id` do corpo de um 409 — None se não der para ler."""
    try:
        job_id = json.loads(resposta.read().decode("utf-8")).get("job_id")
    except Exception:
        return None
    return str(job_id) if job_id else None


def _postar(payload: dict) -> None:
    """POST no worker. Lança `WorkerOcupado` no 409 e erro se a rede falhar
    ou o status não for 2xx."""
    requisicao = urllib.request.Request(
        _url(),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Process-Secret": settings.gastos_process_secret,
        },
    )
    try:
        with _abrir_url(requisicao) as resposta:
            status = getattr(resposta, "status", None) or resposta.getcode()
            if status == 409:
                raise WorkerOcupado(_job_id_da_resposta(resposta))
            if status >= 300:
                raise RuntimeError(f"HTTP {status}")
    except urllib.error.HTTPError as e:
        # O urlopen de verdade lança HTTPError para 4xx/5xx.
        if e.code == 409:
            raise WorkerOcupado(_job_id_da_resposta(e)) from None
        raise


def _consultar_job(job_id: str) -> dict:
    """`GET /jobs/{id}` no worker. Lança se a consulta falhar (rede, 404)."""
    requisicao = urllib.request.Request(
        settings.gastos_worker_url.rstrip("/")
        + CAMINHO_JOBS
        + urllib.parse.quote(job_id, safe=""),
        method="GET",
        headers={"X-Process-Secret": settings.gastos_process_secret},
    )
    with _abrir_url(requisicao) as resposta:
        return json.loads(resposta.read().decode("utf-8"))


def _esperar_worker_livre(job_id: str | None, tentativa: int, prazo: float) -> None:
    """Espera o job que ocupa o worker terminar, sem passar do prazo.

    Com o id do job, consulta `GET /jobs/{id}` a cada poucos segundos até
    `finished_at` vir preenchido. Não basta o `status`: o worker o grava antes
    de mandar o próprio Telegram de falha e só solta o pipeline depois de
    gravar `finished_at` — um POST nesse meio-tempo leva 409 e gasta tentativa.
    Sem o id, ou se a consulta falhar, backoff simples.
    """
    if job_id:
        while _agora() < prazo:
            _dormir(min(INTERVALO_OCUPADO_S, max(0.0, prazo - _agora())))
            try:
                job = _consultar_job(job_id)
            except Exception:
                break  # não deu para acompanhar: cai no backoff abaixo
            if job.get("finished_at"):
                return
        else:
            return  # prazo acabou; o POST seguinte dá a palavra final
    espera = min(
        INTERVALO_OCUPADO_S * 2 ** (tentativa - 1),
        BACKOFF_OCUPADO_MAX_S,
        max(0.0, prazo - _agora()),
    )
    _dormir(espera)


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
    inicio = _agora()
    prazo = inicio + ESPERA_OCUPADO_MAX_S
    tentativa = 0
    while True:
        tentativa += 1
        try:
            _postar(payload)
            break
        except WorkerOcupado as ocupado:
            if tentativa >= TENTATIVAS_OCUPADO_MAX or _agora() >= prazo:
                log.error(
                    "Worker de gastos ocupado após %d tentativas; %s fica pendente",
                    tentativa,
                    nome,
                )
                try:
                    notifier.notificar_gastos_ocupado(nome, tentativa, _agora() - inicio)
                except Exception:
                    log.exception("Falha ao avisar no Telegram sobre o worker de gastos")
                return False
            log.info(
                "Worker de gastos ocupado (job %s); esperando para avisar %s",
                ocupado.job_id or "?",
                nome,
            )
            _esperar_worker_livre(ocupado.job_id, tentativa, prazo)
        except Exception as e:
            # O organizador já fez a parte dele; o dashboard é que fica atrasado.
            log.error("Worker de gastos não respondeu para %s: %s", nome, e)
            try:
                notifier.notificar_gastos_falhou(nome, str(e))
            except Exception:
                log.exception("Falha ao avisar no Telegram sobre o worker de gastos")
            return False

    gerenciador.marcar_gastos_notificado(file_id, md5)
    log.info("Worker de gastos avisado: %s (%s)", nome, banco)
    return True
