"""Testes do hook que avisa o worker do dashboard de gastos.

Não tocam a rede nem o Drive: o POST no worker e o envio ao Telegram viram
espiões, e o cache de arquivos processados vai para uma pasta temporária.

Como rodar (não precisa de PDF nem de credencial):

    python app/tests/test_hook_gastos.py

Também roda sob pytest, se preferir:

    pytest app/tests/test_hook_gastos.py
"""
from __future__ import annotations

import io
import json
import logging
import os
import sys
import tempfile
import traceback
import urllib.error
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Precisa vir ANTES de importar app.state: o STATE_DIR é lido no import.
os.environ["STATE_DIR"] = tempfile.mkdtemp(prefix="pfo-teste-")

from app import gastos as G  # noqa: E402
from app import main as M  # noqa: E402
from app import notifier, state  # noqa: E402
from app.parser import PdfProtegido, Resultado  # noqa: E402

URL = "https://gastos.teste"
SECRET = "segredo-de-teste"
CONFIG_BASE = G.settings


# ============================================================================
# Andaimes
# ============================================================================


class _Resposta:
    """Resposta HTTP de mentira, no formato que o urlopen devolve."""

    def __init__(self, status: int, corpo: bytes = b"") -> None:
        self.status = status
        self.corpo = corpo

    def getcode(self) -> int:
        return self.status

    def read(self) -> bytes:
        return self.corpo

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class Espiao:
    """Substitui o POST no worker: guarda as requisições e escolhe a resposta."""

    def __init__(self, status: int = 200, erro: Exception | None = None) -> None:
        self.status = status
        self.erro = erro
        self.requisicoes: list = []

    def __call__(self, requisicao):
        self.requisicoes.append(requisicao)
        if self.erro:
            raise self.erro
        return _Resposta(self.status)

    @property
    def chamadas(self) -> int:
        return len(self.requisicoes)

    def cabecalhos(self, i: int = 0) -> dict:
        return {k.lower(): v for k, v in self.requisicoes[i].header_items()}

    def corpo(self, i: int = 0) -> dict:
        return json.loads(self.requisicoes[i].data.decode("utf-8"))


def _zerar_cache() -> None:
    """Cache limpo em disco e em memória, como um serviço que acabou de subir."""
    state._gerenciador = None
    if state.STATE_FILE.exists():
        state.STATE_FILE.unlink()


def _zerar_estado_da_varredura() -> None:
    """Flags do processo que um teste não pode herdar do anterior (a ordem
    muda: pytest segue o arquivo, o script segue a ordem alfabética)."""
    M.estado.alarme_subpastas = False
    M.estado.completa_ao_voltar = False
    M.estado.avisos_adiados = None


@contextmanager
def ambiente(
    url: str = URL,
    secret: str = SECRET,
    dry_run: bool = False,
    erro: Exception | None = None,
    status: int = 200,
    **config_extra,
):
    """Cache e estado da varredura zerados, config isolada, POST e Telegram espionados."""
    _zerar_cache()
    _zerar_estado_da_varredura()
    config = replace(
        CONFIG_BASE,
        gastos_worker_url=url,
        gastos_process_secret=secret,
        dry_run=dry_run,
        **config_extra,
    )
    espiao = Espiao(status=status, erro=erro)
    mensagens: list[str] = []

    def _tg(mensagem: str) -> bool:
        mensagens.append(mensagem)
        return True

    try:
        with mock.patch.object(G, "settings", config), mock.patch.object(
            M, "settings", config
        ), mock.patch.object(G, "_abrir_url", espiao), mock.patch.object(
            notifier, "tg_send", _tg
        ):
            yield espiao, mensagens
    finally:
        _zerar_estado_da_varredura()


def _registrar(file_id: str, nome: str, pasta: int, md5: str) -> None:
    """Simula o que o fluxo de rename grava no cache antes de avisar o worker."""
    state.get_state_manager().registrar(
        file_id, nome, pasta, state.SUCESSO, nome_novo=nome, md5=md5
    )


@contextmanager
def drive_de_mentira(nome_novo: str, renomeados: list):
    """Substitui download/rename do Drive e o parser, para exercitar _processar."""

    def _rename(file_id, novo):
        renomeados.append((file_id, novo))

    with mock.patch.object(M.drive, "download_pdf", lambda _: b"%PDF-fake"), (
        mock.patch.object(M, "determinar_nome_novo", lambda *a, **k: Resultado(nome_novo, []))
    ), mock.patch.object(
        M.drive, "nome_sem_colisao", lambda pasta, nome, ignorar_id=None: nome
    ), mock.patch.object(M.drive, "rename_file", _rename):
        yield


@contextmanager
def drive_que_falha(download=None, parse=None, rename=None, nome_novo="2026-09 - BTG.pdf"):
    """Como `drive_de_mentira`, mas cada etapa pode lançar a exceção dada."""

    def _lanca_ou(erro, valor):
        def _f(*_a, **_k):
            if erro:
                raise erro
            return valor

        return _f

    with mock.patch.object(
        M.drive, "download_pdf", _lanca_ou(download, b"%PDF-fake")
    ), mock.patch.object(
        M, "determinar_nome_novo", _lanca_ou(parse, Resultado(nome_novo, []))
    ), mock.patch.object(
        M.drive, "nome_sem_colisao", lambda pasta, nome, ignorar_id=None: nome
    ), mock.patch.object(M.drive, "rename_file", _lanca_ou(rename, None)):
        yield


def _arquivo(file_id: str, nome: str, md5: str) -> M.drive.DriveFile:
    return M.drive.DriveFile(id=file_id, name=nome, mime_type="application/pdf", md5=md5)


@contextmanager
def varredura_completa(pastas: dict[str, list], processados: list):
    """`listar_pdfs` devolve os arquivos dados por pasta; `_processar` só anota."""

    def _processar(file_id, *_a, **_k):
        processados.append(file_id)
        return False

    with mock.patch.object(
        M.drive, "listar_pdfs", lambda pasta_id, incluir_csv=False: pastas.get(pasta_id, [])
    ), mock.patch.object(M, "_processar", _processar):
        yield


def _http_409(job_id: str | None = "job-1") -> urllib.error.HTTPError:
    """O 409 do worker como o urlopen de verdade entrega: uma HTTPError."""
    corpo = json.dumps({"busy": True, "job_id": job_id}).encode() if job_id else b""
    return urllib.error.HTTPError(f"{URL}/process", 409, "Conflict", {}, io.BytesIO(corpo))


TERMINOU_EM = "2026-09-16T12:00:00+00:00"


class WorkerFalso:
    """Worker com um job por vez: POST responde conforme a fila, GET /jobs/{id}
    devolve os status em ordem (e 'done' quando a lista acaba). Job em
    `queued`/`running` vem sem `finished_at`; os demais, com."""

    def __init__(self, posts: list, status_jobs: list[str] | None = None) -> None:
        # Cada item: um status HTTP (int) ou uma função que devolve a exceção.
        # O último item se repete para sempre.
        self.posts = list(posts)
        self.status_jobs = list(status_jobs or [])
        self.requisicoes: list = []

    def __call__(self, requisicao):
        self.requisicoes.append(requisicao)
        if requisicao.get_method() == "GET":
            status = self.status_jobs.pop(0) if self.status_jobs else "done"
            terminou = None if status in ("queued", "running") else TERMINOU_EM
            corpo = {"status": status, "finished_at": terminou}
            return _Resposta(200, json.dumps(corpo).encode())
        item = self.posts.pop(0) if len(self.posts) > 1 else self.posts[0]
        if callable(item):
            raise item()
        return _Resposta(item, b'{"job_id": "novo", "status": "queued"}')

    def metodo(self, metodo: str) -> list:
        return [r for r in self.requisicoes if r.get_method() == metodo]


class WorkerComJob(WorkerFalso):
    """Um job segurando o worker, no tempo do relógio falso, como o worker
    real faz: grava `status` em `status_em` segundos, manda o próprio Telegram
    de falha, grava `finished_at` em `termina_em` e só então solta o pipeline
    — o POST leva 409 até lá. `get_quebrado` faz o GET /jobs/{id} falhar."""

    def __init__(
        self, status_em: float = 10, termina_em: float = 20, get_quebrado: bool = False
    ) -> None:
        super().__init__(posts=[202])
        self.status_em = status_em
        self.termina_em = termina_em
        self.get_quebrado = get_quebrado
        self.inicio: float | None = None

    def __call__(self, requisicao):
        self.requisicoes.append(requisicao)
        if self.inicio is None:
            self.inicio = G._agora()
        decorrido = G._agora() - self.inicio
        if requisicao.get_method() == "GET":
            if self.get_quebrado:
                raise urllib.error.URLError("sem rota até o worker")
            corpo = {
                "status": "error" if decorrido >= self.status_em else "running",
                "finished_at": TERMINOU_EM if decorrido >= self.termina_em else None,
            }
            return _Resposta(200, json.dumps(corpo).encode())
        if decorrido < self.termina_em:
            raise _http_409("job-1")
        return _Resposta(202, b'{"job_id": "novo", "status": "queued"}')


@contextmanager
def relogio_falso():
    """Sono instantâneo: o relógio do módulo avança o que o código dormiria."""
    agora = [1000.0]
    dormidas: list[float] = []

    def _dormir(segundos):
        dormidas.append(segundos)
        agora[0] += segundos

    with mock.patch.object(G, "_agora", lambda: agora[0]), mock.patch.object(
        G, "_dormir", _dormir
    ):
        yield dormidas


# ============================================================================
# Testes — unidade (app/gastos.py)
# ============================================================================


def test_dispara_para_subpasta_da_pasta_04():
    """Extrato numa subpasta de banco vira POST no worker, com o payload certo."""
    with ambiente() as (espiao, _):
        _registrar("id-1", "2026-09 - Bradesco.pdf", 4, "md5-a")
        avisou = G.avisar_worker(
            4, "Bradesco", "id-1", "2026-09 - Bradesco.pdf", "pasta-bradesco", "md5-a"
        )

    assert avisou is True
    assert espiao.chamadas == 1
    assert espiao.requisicoes[0].full_url == f"{URL}/process"
    assert espiao.requisicoes[0].get_method() == "POST"
    assert espiao.cabecalhos()["x-process-secret"] == SECRET
    assert espiao.corpo() == {
        "file_id": "id-1",
        "name": "2026-09 - Bradesco.pdf",
        "bank_folder": "Bradesco",
        "parent_folder_id": "pasta-bradesco",
    }


def test_banco_novo_funciona_sozinho():
    """O banco é o nome da subpasta — subpasta nova não precisa de código novo."""
    for banco in ("BTG", "Itau", "Bradesco", "Nubank"):
        with ambiente() as (espiao, _):
            _registrar("id-x", f"2026-09 - {banco}.pdf", 4, "md5-x")
            G.avisar_worker(
                4, banco, "id-x", f"2026-09 - {banco}.pdf", "pasta-x", "md5-x"
            )
        assert espiao.chamadas == 1, banco
        assert espiao.corpo()["bank_folder"] == banco


def test_nao_dispara_para_pastas_01_a_03():
    """Notas, performance e extrato de investimento não interessam ao dashboard."""
    for pasta, nome in (
        (1, "09-01 - PEJA11 - Compra - R$1.000,00.pdf"),
        (2, "26-09-01 - Performance - 26-01 - 26-08.pdf"),
        (3, "2026-09.pdf"),
    ):
        with ambiente() as (espiao, mensagens):
            _registrar("id-p", nome, pasta, "md5-p")
            avisou = G.avisar_worker(pasta, None, "id-p", nome, "pasta-p", "md5-p")
        assert avisou is False, pasta
        assert espiao.chamadas == 0, pasta
        assert mensagens == [], pasta


def test_nao_dispara_na_raiz_da_pasta_04():
    """Arquivo solto na raiz da Pasta 04 não tem banco — nada a avisar."""
    with ambiente() as (espiao, _):
        _registrar("id-r", "Fatura_MASTERCARD_1.pdf", 4, "md5-r")
        avisou = G.avisar_worker(
            4, None, "id-r", "Fatura_MASTERCARD_1.pdf", "pasta-04", "md5-r"
        )
    assert avisou is False
    assert espiao.chamadas == 0


def test_nao_avisa_duas_vezes():
    """Mesmo file_id + md5 = um POST só, por mais que a varredura repita."""
    with ambiente() as (espiao, _):
        _registrar("id-2", "2026-09 - BTG.pdf", 4, "md5-b")
        primeiro = G.avisar_worker(
            4, "BTG", "id-2", "2026-09 - BTG.pdf", "pasta-btg", "md5-b"
        )
        segundo = G.avisar_worker(
            4, "BTG", "id-2", "2026-09 - BTG.pdf", "pasta-btg", "md5-b"
        )
        terceiro = G.avisar_worker(
            4, "BTG", "id-2", "2026-09 - BTG.pdf", "pasta-btg", "md5-b"
        )

    assert (primeiro, segundo, terceiro) == (True, False, False)
    assert espiao.chamadas == 1


def test_conteudo_novo_avisa_de_novo():
    """Arquivo trocado (md5 diferente) precisa chegar de novo ao dashboard."""
    with ambiente() as (espiao, _):
        _registrar("id-3", "2026-09 - BTG.pdf", 4, "md5-velho")
        G.avisar_worker(4, "BTG", "id-3", "2026-09 - BTG.pdf", "p", "md5-velho")
        # Mesmo arquivo, conteúdo novo: o registro é regravado com o md5 novo.
        _registrar("id-3", "2026-09 - BTG.pdf", 4, "md5-novo")
        G.avisar_worker(4, "BTG", "id-3", "2026-09 - BTG.pdf", "p", "md5-novo")

    assert espiao.chamadas == 2


def test_restart_nao_re_notifica():
    """O 'já avisei' está no cache em disco, então sobrevive a um restart."""
    with ambiente() as (espiao, _):
        _registrar("id-4", "2026-09 - Itau.pdf", 4, "md5-c")
        G.avisar_worker(4, "Itau", "id-4", "2026-09 - Itau.pdf", "p", "md5-c")
        assert espiao.chamadas == 1

        # Serviço reiniciou: cache em memória some, o JSON continua lá.
        state._gerenciador = None
        assert state.get_state_manager().ja_notificou_gastos("id-4", "md5-c") is True

        G.avisar_worker(4, "Itau", "id-4", "2026-09 - Itau.pdf", "p", "md5-c")

    assert espiao.chamadas == 1


def test_worker_fora_do_ar_avisa_no_telegram():
    """Worker fora do ar: log + Telegram, e nada de marcar como avisado."""
    erro = urllib.error.URLError("conexão recusada")
    with ambiente(erro=erro) as (espiao, mensagens):
        _registrar("id-5", "2026-09 - BTG.pdf", 4, "md5-d")
        avisou = G.avisar_worker(
            4, "BTG", "id-5", "2026-09 - BTG.pdf", "pasta-btg", "md5-d"
        )
        # Não marcou: na próxima varredura ele tenta de novo.
        pendente = not state.get_state_manager().ja_notificou_gastos("id-5", "md5-d")

    assert avisou is False
    assert espiao.chamadas == 1
    assert pendente is True
    assert any("worker de gastos não respondeu" in m for m in mensagens), mensagens


def test_status_de_erro_conta_como_falha():
    """HTTP 500 não pode ser lido como sucesso."""
    with ambiente(status=500) as (espiao, mensagens):
        _registrar("id-6", "2026-09 - BTG.pdf", 4, "md5-e")
        avisou = G.avisar_worker(4, "BTG", "id-6", "2026-09 - BTG.pdf", "p", "md5-e")
        pendente = not state.get_state_manager().ja_notificou_gastos("id-6", "md5-e")

    assert avisou is False
    assert pendente is True
    assert any("worker de gastos não respondeu" in m for m in mensagens)


def test_env_ausente_e_no_op():
    """Sem GASTOS_PROCESS_SECRET o hook não existe: nem POST, nem Telegram."""
    with ambiente(secret="") as (espiao, mensagens):
        habilitado = G.habilitado()
        _registrar("id-7", "2026-09 - BTG.pdf", 4, "md5-f")
        avisou = G.avisar_worker(4, "BTG", "id-7", "2026-09 - BTG.pdf", "p", "md5-f")

    assert habilitado is False
    assert avisou is False
    assert espiao.chamadas == 0
    assert mensagens == []


def test_url_vazia_tambem_desliga():
    with ambiente(url="") as (espiao, _):
        _registrar("id-8", "2026-09 - BTG.pdf", 4, "md5-g")
        avisou = G.avisar_worker(4, "BTG", "id-8", "2026-09 - BTG.pdf", "p", "md5-g")
    assert avisou is False
    assert espiao.chamadas == 0


def test_dry_run_nao_avisa():
    """Em simulação nada foi renomeado — o worker não tem o que reprocessar."""
    with ambiente(dry_run=True) as (espiao, _):
        _registrar("id-9", "2026-09 - BTG.pdf", 4, "md5-h")
        avisou = G.avisar_worker(
            4, "BTG", "id-9", "2026-09 - BTG.pdf", "p", "md5-h", simulacao=True
        )
    assert avisou is False
    assert espiao.chamadas == 0


# ============================================================================
# Testes — integração com o fluxo de rename (app/main.py)
# ============================================================================


def test_rename_na_pasta_04_dispara_o_hook():
    """Fluxo completo: PDF novo numa subpasta de banco é renomeado E avisado."""
    renomeados: list = []
    with ambiente() as (espiao, mensagens):
        with drive_de_mentira("2026-09 - Bradesco.pdf", renomeados):
            renomeou = M._processar(
                "id-10",
                "Fatura_MASTERCARD_100471538438_01-09-2026.pdf",
                "pasta-bradesco",
                4,
                "Bradesco",
                "md5-i",
            )

    assert renomeou is True
    assert renomeados == [("id-10", "2026-09 - Bradesco.pdf")]
    assert espiao.chamadas == 1
    # O worker recebe o nome NOVO, não o antigo.
    assert espiao.corpo()["name"] == "2026-09 - Bradesco.pdf"
    assert espiao.corpo()["bank_folder"] == "Bradesco"


def test_rename_na_pasta_01_nao_dispara_o_hook():
    renomeados: list = []
    with ambiente() as (espiao, _):
        with drive_de_mentira("09-01 - PEJA11 - Compra - R$1.000,00.pdf", renomeados):
            renomeou = M._processar(
                "id-11", "nota-qualquer.pdf", "pasta-01", 1, None, "md5-j"
            )

    assert renomeou is True
    assert len(renomeados) == 1
    assert espiao.chamadas == 0


def test_worker_fora_do_ar_nao_quebra_o_rename():
    """O rename é a prioridade: worker caído vira aviso, não exceção."""
    renomeados: list = []
    erro = urllib.error.URLError("timeout")
    with ambiente(erro=erro) as (espiao, mensagens):
        with drive_de_mentira("2026-09 - BTG.pdf", renomeados):
            renomeou = M._processar(
                "id-12", "extrato-btg.pdf", "pasta-btg", 4, "BTG", "md5-k"
            )

    assert renomeou is True
    assert renomeados == [("id-12", "2026-09 - BTG.pdf")]
    assert espiao.chamadas == 1
    assert any("worker de gastos não respondeu" in m for m in mensagens), mensagens
    # A notificação normal de rename continua saindo.
    assert any("2026-09 - BTG.pdf" in m and "→" in m for m in mensagens)


def test_arquivo_ja_no_padrao_final_avisa_uma_vez():
    """Arquivo que já chegou com o nome certo é novidade para o dashboard."""
    with ambiente() as (espiao, _):
        primeiro = M._processar(
            "id-13", "2026-08 - Itau.pdf", "pasta-itau", 4, "Itau", "md5-l"
        )
        # Varredura seguinte: mesmo arquivo, mesmo md5.
        M._processar("id-13", "2026-08 - Itau.pdf", "pasta-itau", 4, "Itau", "md5-l")

    assert primeiro is False  # não renomeou nada
    assert espiao.chamadas == 1


# ============================================================================
# Testes — Pasta 04: todo arquivo chega ao worker, renomeado ou não
# ============================================================================


def test_pdf_com_senha_na_subpasta_btg_avisa_o_worker():
    """PDF com senha fica com o nome original e vai ao worker, que tem a senha."""
    with ambiente() as (espiao, mensagens):
        with drive_que_falha(parse=PdfProtegido("Incorrect password")):
            renomeou = M._processar(
                "id-20", "extrato_btg_092026.pdf", "pasta-btg", 4, "BTG", "md5-20"
            )
        registro = state.get_state_manager().get("id-20")

    assert renomeou is False
    assert registro.status == state.PROTEGIDO
    assert espiao.chamadas == 1
    assert espiao.corpo() == {
        "file_id": "id-20",
        "name": "extrato_btg_092026.pdf",
        "bank_folder": "BTG",
        "parent_folder_id": "pasta-btg",
    }
    protegido = [m for m in mensagens if "protegido por senha" in m]
    assert len(protegido) == 1, mensagens
    assert "ficou com o nome original" in protegido[0]
    assert "senha configurada no servidor" in protegido[0]
    assert "Remova a senha" not in protegido[0]


def test_pdf_com_senha_e_worker_fora_do_ar_nao_diz_que_enviou():
    """O Telegram do PDF com senha não afirma um envio que falhou."""
    with ambiente(erro=urllib.error.URLError("recusada")) as (espiao, mensagens):
        with drive_que_falha(parse=PdfProtegido("Incorrect password")):
            M._processar("id-25", "extrato.pdf", "pasta-btg", 4, "BTG", "md5-25")

    assert espiao.chamadas == 1
    protegido = [m for m in mensagens if "protegido por senha" in m]
    assert len(protegido) == 1, mensagens
    assert "Não chegou ao dashboard" in protegido[0]
    assert "senha configurada no servidor" not in protegido[0]
    assert "próxima varredura completa" not in protegido[0]
    assert any("worker de gastos não respondeu" in m for m in mensagens)


def test_pdf_com_senha_com_hook_desligado_diz_que_esta_desligado():
    """Sem secret ou com DRY_RUN nada é tentado: o Telegram não fala em
    'não chegou', diz que o aviso está desligado."""
    for caso, config in (("sem secret", {"secret": ""}), ("DRY_RUN", {"dry_run": True})):
        with ambiente(**config) as (espiao, mensagens):
            with drive_que_falha(parse=PdfProtegido("Incorrect password")):
                M._processar("id-27", "extrato.pdf", "pasta-btg", 4, "BTG", "md5-27")

        assert espiao.chamadas == 0, caso
        protegido = [m for m in mensagens if "protegido por senha" in m]
        assert len(protegido) == 1, (caso, mensagens)
        assert "está desligado" in protegido[0], caso
        assert "Não chegou" not in protegido[0], caso
        assert "Enviado" not in protegido[0], caso


def test_pdf_com_senha_na_pasta_01_nao_avisa_o_worker():
    """Pastas 01–03 seguem como antes: pede para remover a senha, sem worker."""
    with ambiente() as (espiao, mensagens):
        with drive_que_falha(parse=PdfProtegido("Incorrect password")):
            M._processar("id-21", "nota.pdf", "pasta-01", 1, None, "md5-21")
        registro = state.get_state_manager().get("id-21")

    assert espiao.chamadas == 0
    assert registro.status == state.PROTEGIDO
    assert any("Remova a senha" in m for m in mensagens), mensagens


def test_erro_de_download_na_pasta_04_avisa_o_worker():
    """O worker baixa o arquivo sozinho — uma falha de download aqui não o impede."""
    with ambiente() as (espiao, _):
        with drive_que_falha(download=RuntimeError("HttpError 500")):
            M._processar("id-22", "fatura.pdf", "pasta-itau", 4, "Itau", "md5-22")
        registro = state.get_state_manager().get("id-22")

    assert registro.status == state.ERRO
    assert espiao.chamadas == 1
    assert espiao.corpo()["bank_folder"] == "Itau"


def test_erro_de_leitura_na_pasta_04_avisa_o_worker():
    with ambiente() as (espiao, mensagens):
        with drive_que_falha(parse=ValueError("layout inesperado")):
            M._processar("id-23", "fatura.pdf", "pasta-bradesco", 4, "Bradesco", "md5-23")
        registro = state.get_state_manager().get("id-23")

    assert registro.status == state.ERRO
    assert espiao.chamadas == 1
    assert espiao.corpo()["name"] == "fatura.pdf"
    # O aviso de erro de parsing continua saindo.
    assert any("parsing de fatura.pdf" in m for m in mensagens), mensagens


def test_rename_que_falha_na_pasta_04_avisa_e_tenta_de_novo_na_varredura_seguinte():
    """Um 503 passageiro no rename: o worker é avisado com o nome original, e a
    varredura seguinte renomeia de verdade — sem avisar o worker de novo."""
    arquivo = _arquivo("id-24", "Fatura_NU.pdf", "md5-24")
    mapa = {"pasta-nubank": (4, "NuBank")}
    renomeados: list = []
    with ambiente() as (espiao, mensagens):
        with mock.patch.object(
            M.drive, "listar_pdfs", lambda pasta_id, incluir_csv=False: [arquivo]
        ):
            with drive_que_falha(rename=RuntimeError("HttpError 503 backendError")):
                M._varrer_tudo(mapa)
            gerenciador = state.get_state_manager()
            status_apos_falha = gerenciador.get("id-24").status
            posts_apos_falha = espiao.chamadas
            nome_avisado = espiao.corpo()["name"]

            with drive_de_mentira("2026-09 - NuBank.pdf", renomeados):
                M._varrer_tudo(mapa)
            registro = gerenciador.get("id-24")

    # Varredura seguinte: rename feito, registro final, nenhum POST novo.
    assert renomeados == [("id-24", "2026-09 - NuBank.pdf")]
    assert status_apos_falha == state.ERRO_RENAME
    assert posts_apos_falha == 1
    assert nome_avisado == "Fatura_NU.pdf"  # o rename não tinha acontecido
    # O erro continua indo ao Telegram, como antes.
    assert any("503" in m for m in mensagens), mensagens
    assert registro.status == state.SUCESSO
    assert registro.gastos_notificado_md5 == "md5-24"
    assert espiao.chamadas == 1


def test_excecao_no_rename_na_pasta_01_segue_como_antes():
    """Pastas 01–03: nem aviso nem registro — a varredura completa tenta de novo."""
    levantou = False
    with ambiente() as (espiao, _):
        with drive_que_falha(rename=RuntimeError("insufficientFilePermissions")):
            try:
                M._processar("id-26", "nota.pdf", "pasta-01", 1, None, "md5-26")
            except RuntimeError:
                levantou = True
        registro = state.get_state_manager().get("id-26")

    assert levantou is True
    assert espiao.chamadas == 0
    assert registro is None


def test_varredura_completa_avisa_protegido_e_sem_dados_ja_registrados():
    """PROTEGIDO e SEM_DADOS de antes, com nome fora do padrão, chegam ao worker
    uma vez só: a segunda varredura não reenvia o mesmo md5. Pasta 01 nunca."""
    protegido = _arquivo("id-30", "extrato_btg_senha.pdf", "md5-30")
    sem_dados = _arquivo("id-31", "3f9c1e2a-sem-nome", "md5-31")
    nota = _arquivo("id-32", "nota-com-senha.pdf", "md5-32")
    processados: list = []
    with ambiente() as (espiao, mensagens):
        gerenciador = state.get_state_manager()
        gerenciador.registrar(
            "id-30", protegido.name, 4, state.PROTEGIDO, motivo="PDF exige senha", md5="md5-30"
        )
        gerenciador.registrar(
            "id-31", sem_dados.name, 4, state.SEM_DADOS, motivo="nenhum campo", md5="md5-31"
        )
        gerenciador.registrar(
            "id-32", nota.name, 1, state.PROTEGIDO, motivo="PDF exige senha", md5="md5-32"
        )
        mapa = {"pasta-01": (1, None), "pasta-btg": (4, "BTG")}
        pastas = {"pasta-01": [nota], "pasta-btg": [protegido, sem_dados]}
        with varredura_completa(pastas, processados):
            M._varrer_tudo(mapa)
            M._varrer_tudo(mapa)

    assert processados == []  # já registrados: nada de baixar e parsear de novo
    assert espiao.chamadas == 2
    assert sorted(espiao.corpo(i)["file_id"] for i in range(2)) == ["id-30", "id-31"]
    assert mensagens == []


def test_arquivo_da_raiz_movido_para_btg_e_renomeado_e_avisado_com_bank_folder_btg():
    """Registrado como banco desconhecido na raiz; depois aparece dentro de BTG
    e é processado de verdade: renomeado com o banco e avisado."""
    nome = "Fatura_BTG_setembro.pdf"
    renomeados: list = []
    with ambiente() as (espiao, _):
        # 1) Caiu na raiz da Pasta 04: sem banco, sem aviso.
        M._processar("id-40", nome, "pasta-04", 4, None, "md5-40")
        gerenciador = state.get_state_manager()
        chamadas_na_raiz = espiao.chamadas
        motivo = gerenciador.get("id-40").motivo
        # Enquanto está na raiz, não há o que reprocessar.
        reprocessa_na_raiz = gerenciador.precisa_processar("id-40", False)
        # 2) Foi movido para a subpasta BTG e a varredura completa o encontra lá.
        pastas = {"pasta-btg": [_arquivo("id-40", nome, "md5-40")]}
        with mock.patch.object(
            M.drive, "listar_pdfs", lambda pasta_id, incluir_csv=False: pastas.get(pasta_id, [])
        ), drive_de_mentira("2026-09 - BTG.pdf", renomeados):
            M._varrer_tudo({"pasta-04": (4, None), "pasta-btg": (4, "BTG")})
        registro = gerenciador.get("id-40")

    assert chamadas_na_raiz == 0
    assert motivo == state.MOTIVO_BANCO_DESCONHECIDO
    assert reprocessa_na_raiz is False
    assert renomeados == [("id-40", "2026-09 - BTG.pdf")]
    assert registro.status == state.SUCESSO
    assert espiao.chamadas == 1
    assert espiao.corpo()["bank_folder"] == "BTG"
    assert espiao.corpo()["parent_folder_id"] == "pasta-btg"
    assert espiao.corpo()["name"] == "2026-09 - BTG.pdf"


def test_varredura_incremental_avisa_arquivo_ja_registrado():
    """O webhook também avisa o que já estava no cache e nunca chegou ao worker."""
    novos = [
        {
            "id": "id-41", "name": "Fatura_BTG.pdf", "parents": ["pasta-btg"],
            "md5": "md5-41", "mime_type": "application/pdf",
        }
    ]
    processados: list = []
    with ambiente() as (espiao, _):
        state.get_state_manager().registrar(
            "id-41", "Fatura_BTG.pdf", 4, state.PROTEGIDO,
            motivo="PDF exige senha", md5="md5-41",
        )
        with mock.patch.object(
            M, "_mapa_pastas", lambda: {"pasta-btg": (4, "BTG")}
        ), mock.patch.object(
            M.drive, "listar_mudancas", lambda token: (novos, "token-novo")
        ), mock.patch.object(
            M, "_processar", lambda file_id, *a, **k: processados.append(file_id)
        ):
            M.estado.page_token = "token-velho"
            M._varrer(completa=False)
            M._varrer(completa=False)  # rajada do Drive: mesmo arquivo de novo

    assert processados == []
    assert espiao.chamadas == 1
    assert espiao.corpo()["bank_folder"] == "BTG"


# ============================================================================
# Testes — worker ocupado (409)
# ============================================================================


def test_409_seguido_de_202_marca_como_avisado_sem_telegram():
    """Worker ocupado: espera o job ativo terminar e avisa, sem Telegram."""
    worker = WorkerFalso([lambda: _http_409("job-1"), 202], ["running", "done"])
    with ambiente() as (_, mensagens), relogio_falso(), mock.patch.object(
        G, "_abrir_url", worker
    ):
        _registrar("id-50", "2026-09 - BTG.pdf", 4, "md5-50")
        avisou = G.avisar_worker(
            4, "BTG", "id-50", "2026-09 - BTG.pdf", "pasta-btg", "md5-50"
        )
        avisado = state.get_state_manager().ja_notificou_gastos("id-50", "md5-50")

    assert avisou is True
    assert avisado is True
    assert mensagens == []
    assert len(worker.metodo("POST")) == 2
    # Esperou consultando o job no worker, não às cegas.
    consultas = worker.metodo("GET")
    assert [r.full_url for r in consultas] == [f"{URL}/jobs/job-1"] * 2
    cabecalhos = {k.lower(): v for k, v in consultas[0].header_items()}
    assert cabecalhos["x-process-secret"] == SECRET


def test_409_sem_job_id_usa_backoff_e_depois_avisa():
    worker = WorkerFalso([lambda: _http_409(None), lambda: _http_409(None), 202])
    with ambiente() as (_, mensagens), relogio_falso() as dormidas, mock.patch.object(
        G, "_abrir_url", worker
    ):
        _registrar("id-52", "2026-09 - BTG.pdf", 4, "md5-52")
        avisou = G.avisar_worker(4, "BTG", "id-52", "2026-09 - BTG.pdf", "p", "md5-52")

    assert avisou is True
    assert mensagens == []
    assert worker.metodo("GET") == []
    assert dormidas == [G.INTERVALO_OCUPADO_S, G.INTERVALO_OCUPADO_S * 2]


def test_409_sem_fim_desiste_com_um_telegram_so():
    """Teto de tentativas (o 409 volta a cada job que termina) e de tempo (o job
    nunca termina): um Telegram por desistência, e o arquivo segue pendente."""
    casos = {
        "tentativas": WorkerFalso([lambda: _http_409("job-x")]),
        "tempo": WorkerFalso([lambda: _http_409("job-preso")], ["running"] * 1000),
    }
    for caso, worker in casos.items():
        with ambiente() as (_, mensagens), relogio_falso() as dormidas, (
            mock.patch.object(G, "_abrir_url", worker)
        ):
            _registrar("id-51", "2026-09 - Itau.pdf", 4, "md5-51")
            avisou = G.avisar_worker(4, "Itau", "id-51", "2026-09 - Itau.pdf", "p", "md5-51")
            pendente = not state.get_state_manager().ja_notificou_gastos("id-51", "md5-51")

        assert avisou is False, caso
        assert pendente is True, caso
        assert len(mensagens) == 1, (caso, mensagens)
        assert "worker de gastos ocupado" in mensagens[0], caso
        assert sum(dormidas) <= G.ESPERA_OCUPADO_MAX_S, caso
        posts = len(worker.metodo("POST"))
        if caso == "tentativas":
            assert posts == G.TENTATIVAS_OCUPADO_MAX
        else:
            assert posts == 2  # desistiu pelo tempo, não pelas tentativas


def test_409_espera_finished_at_e_nao_so_o_status():
    """O worker grava `status` (t=10), manda o Telegram de falha e só grava
    `finished_at` e solta o pipeline em t=20. Um POST no meio leva 409 e gasta
    tentativa; esperar `finished_at` acerta de primeira."""
    worker = WorkerComJob(status_em=10, termina_em=20)
    with ambiente() as (_, mensagens), relogio_falso(), mock.patch.object(
        G, "_abrir_url", worker
    ):
        _registrar("id-53", "2026-09 - BTG.pdf", 4, "md5-53")
        avisou = G.avisar_worker(4, "BTG", "id-53", "2026-09 - BTG.pdf", "p", "md5-53")

    assert avisou is True
    assert mensagens == []
    # Um 409 e o 202 depois de finished_at — nenhum POST entre status e finished_at.
    assert len(worker.metodo("POST")) == 2


def test_409_com_consulta_do_job_quebrada_cai_no_backoff():
    """GET /jobs/{id} falhando não pode virar POST a cada 5 s (8 POSTs e
    desistência em 35 s): cai no backoff e aguenta um job de 100 s."""
    worker = WorkerComJob(status_em=90, termina_em=100, get_quebrado=True)
    with ambiente() as (_, mensagens), relogio_falso() as dormidas, mock.patch.object(
        G, "_abrir_url", worker
    ):
        _registrar("id-54", "2026-09 - BTG.pdf", 4, "md5-54")
        avisou = G.avisar_worker(4, "BTG", "id-54", "2026-09 - BTG.pdf", "p", "md5-54")
        avisado = state.get_state_manager().ja_notificou_gastos("id-54", "md5-54")

    assert avisou is True
    assert avisado is True
    assert mensagens == []
    assert len(worker.metodo("POST")) < G.TENTATIVAS_OCUPADO_MAX
    # Esperas de backoff crescentes entre as consultas que falharam.
    backoffs = dormidas[1::2]
    assert backoffs == sorted(backoffs) and backoffs[-1] > G.INTERVALO_OCUPADO_S, dormidas


# ============================================================================
# Testes — alarme de Pasta 04 sem subpastas de banco
# ============================================================================


def test_subpastas_vazias_geram_um_alarme_so():
    subpastas_btg = [M.drive.DriveFile(id="pasta-btg", name="BTG")]
    with ambiente(drive_folder_04="pasta-04") as (_, mensagens):
        with mock.patch.object(M.drive, "listar_subpastas", lambda _id: []):
            for _ in range(5):  # rajada de webhooks
                mapa = M._mapa_pastas()
        alarmes_na_rajada = len(mensagens)
        # Voltaram: nenhum Telegram, e o alarme rearma para a próxima queda.
        with mock.patch.object(M.drive, "listar_subpastas", lambda _id: subpastas_btg):
            mapa_de_volta = M._mapa_pastas()
        with mock.patch.object(M.drive, "listar_subpastas", lambda _id: []):
            M._mapa_pastas()

    assert alarmes_na_rajada == 1, mensagens
    assert "nenhuma subpasta de banco" in mensagens[0]
    assert mapa == {"pasta-04": (4, None)}
    assert mapa_de_volta["pasta-btg"] == (4, "BTG")
    assert len(mensagens) == 2


def test_erro_ao_listar_subpastas_gera_um_alarme_so():
    def _falha(_id):
        raise RuntimeError("HttpError 404: File not found: pasta-04")

    with ambiente(drive_folder_04="pasta-04") as (_, mensagens):
        with mock.patch.object(M.drive, "listar_subpastas", _falha):
            for _ in range(5):
                mapa = M._mapa_pastas()

    assert len(mensagens) == 1, mensagens
    assert "File not found" in mensagens[0]
    assert mapa == {"pasta-04": (4, None)}


def test_listar_subpastas_nao_engole_erro_da_api():
    """Antes o erro virava lista vazia em silêncio; agora sobe para virar alarme."""
    fake = mock.MagicMock()
    fake.files.return_value.list.return_value.execute.side_effect = RuntimeError("403")
    levantou = False
    with mock.patch.object(M.drive, "_service", return_value=fake):
        try:
            M.drive.listar_subpastas("pasta-04")
        except RuntimeError:
            levantou = True
    assert levantou is True


def test_listagem_de_subpastas_falha_numa_notificacao_e_volta_na_seguinte():
    """A 1ª notificação traz um extrato novo em Itau, mas a listagem das
    subpastas falha: o arquivo é pulado e o page_token avança. Na 2ª a listagem
    volta e o arquivo tem que ser processado e avisado mesmo assim."""
    extrato = {
        "id": "id-60", "name": "Fatura_Itau.pdf", "parents": ["pasta-itau"],
        "md5": "md5-60", "mime_type": "application/pdf",
    }
    listagens: list = [RuntimeError("HttpError 503"), [M.drive.DriveFile("pasta-itau", "Itau")]]
    mudancas: list = [[extrato], []]  # a 2ª notificação não traz o arquivo de novo

    def _subpastas(_id):
        resposta = listagens.pop(0) if len(listagens) > 1 else listagens[0]
        if isinstance(resposta, Exception):
            raise resposta
        return resposta

    pdfs = {"pasta-itau": [_arquivo("id-60", "Fatura_Itau.pdf", "md5-60")]}
    renomeados: list = []
    with ambiente(drive_folder_04="pasta-04") as (espiao, mensagens):
        with mock.patch.object(M.drive, "listar_subpastas", _subpastas), mock.patch.object(
            M.drive, "listar_mudancas", lambda token: (mudancas.pop(0), token + "+")
        ), mock.patch.object(
            M.drive, "listar_pdfs", lambda pasta_id, incluir_csv=False: pdfs.get(pasta_id, [])
        ), drive_de_mentira("2026-09 - Itau.pdf", renomeados):
            M.estado.page_token = "t"
            M._varrer(completa=False)  # 1ª notificação: listagem quebrada
            apos_a_primeira = (list(renomeados), espiao.chamadas)
            M._varrer(completa=False)  # 2ª notificação: listagem de volta

    assert apos_a_primeira == ([], 0)
    assert renomeados == [("id-60", "2026-09 - Itau.pdf")]
    assert espiao.chamadas == 1
    assert espiao.corpo()["bank_folder"] == "Itau"
    assert sum("nenhuma subpasta de banco" in m for m in mensagens) == 1, mensagens


class _DriveListagemFalsa:
    """`_service()` falso só para `files().list()`: cada pasta tem uma fila de
    respostas (lista de arquivos ou exceção); a última se repete. Assim o
    `listar_pdfs` de verdade roda, com o tratamento de erro dele."""

    def __init__(self, respostas: dict[str, list]) -> None:
        self.respostas = respostas
        self._pasta = ""

    def files(self):
        return self

    def list(self, q: str, **_):
        self._pasta = q.split("'")[1]
        return self

    def execute(self):
        fila = self.respostas.get(self._pasta, [[]])
        resposta = fila.pop(0) if len(fila) > 1 else fila[0]
        if isinstance(resposta, Exception):
            raise resposta
        return {
            "files": [
                {"id": a.id, "name": a.name, "mimeType": a.mime_type, "md5Checksum": a.md5}
                for a in resposta
            ]
        }


def test_recuperacao_so_desliga_a_pendencia_se_todas_as_pastas_listaram():
    """O caso do revisor: a listagem das subpastas falha na 1ª notificação e
    volta na 2ª, mas na varredura de recuperação o `listar_pdfs` do Itau falha.
    A pendência continua ligada, a Pasta 01 segue normal, e na 3ª notificação o
    extrato do Itau é renomeado e avisado."""
    extrato = _arquivo("id-60", "Fatura_Itau.pdf", "md5-60")
    nota = _arquivo("id-61", "nota.pdf", "md5-61")
    listagens: list = [RuntimeError("HttpError 503"), [M.drive.DriveFile("pasta-itau", "Itau")]]
    drive_falso = _DriveListagemFalsa({
        "pasta-01": [[nota]],
        "pasta-04": [[]],
        # 2ª notificação: erro da API; 3ª: o extrato aparece.
        "pasta-itau": [RuntimeError("HttpError 500 listando Itau"), [extrato]],
    })
    mudancas: list = [
        [{"id": "id-60", "name": extrato.name, "parents": ["pasta-itau"],
          "md5": "md5-60", "mime_type": "application/pdf"}],
        [],
        [],
    ]
    nomes = {4: "2026-09 - Itau.pdf", 1: "09-01 - PEJA11 - Compra - R$1.000,00.pdf"}
    renomeados: list = []

    def _subpastas(_id):
        resposta = listagens.pop(0) if len(listagens) > 1 else listagens[0]
        if isinstance(resposta, Exception):
            raise resposta
        return resposta

    def _rename(file_id, novo):
        renomeados.append((file_id, novo))

    with ambiente(drive_folder_01="pasta-01", drive_folder_04="pasta-04") as (espiao, _):
        with mock.patch.object(M.drive, "listar_subpastas", _subpastas), mock.patch.object(
            M.drive, "_service", lambda: drive_falso
        ), mock.patch.object(
            M.drive, "listar_mudancas", lambda token: (mudancas.pop(0), token + "+")
        ), mock.patch.object(M.drive, "download_pdf", lambda _id: b"%PDF-fake"), (
            mock.patch.object(
                M, "determinar_nome_novo",
                lambda numero, *_a, **_k: Resultado(nomes[numero], []),
            )
        ), mock.patch.object(
            M.drive, "nome_sem_colisao", lambda pasta, nome, ignorar_id=None: nome
        ), mock.patch.object(M.drive, "rename_file", _rename):
            M.estado.page_token = "t"
            M._varrer(completa=False)  # 1ª: subpastas não listam
            M._varrer(completa=False)  # 2ª: subpastas voltam, Itau não lista
            apos_a_segunda = (list(renomeados), espiao.chamadas, M.estado.completa_ao_voltar)
            M._varrer(completa=False)  # 3ª: tudo lista
            pendencia_no_fim = M.estado.completa_ao_voltar

    # Na 2ª a Pasta 01 foi renomeada mesmo com o Itau quebrado; o extrato não
    # sumiu: a pendência ficou ligada.
    assert apos_a_segunda == ([("id-61", nomes[1])], 0, True)
    assert renomeados == [("id-61", nomes[1]), ("id-60", "2026-09 - Itau.pdf")]
    assert espiao.chamadas == 1
    assert espiao.corpo()["file_id"] == "id-60"
    assert espiao.corpo()["bank_folder"] == "Itau"
    assert pendencia_no_fim is False


def test_listar_pdfs_nao_engole_erro_da_api():
    """Erro de API não pode virar "pasta vazia" — a varredura precisa saber."""
    drive_falso = _DriveListagemFalsa({"pasta-01": [RuntimeError("403")]})
    levantou = False
    with mock.patch.object(M.drive, "_service", lambda: drive_falso):
        try:
            M.drive.listar_pdfs("pasta-01")
        except RuntimeError:
            levantou = True
    assert levantou is True


# ============================================================================
# Testes — avisos da Pasta 04 saem depois dos renames da varredura
# ============================================================================


def test_varredura_que_quebra_ainda_envia_a_fila_e_propaga_o_erro():
    """A varredura completa monta a fila e depois `listar_mudancas` quebra: o
    `finally` envia os avisos já juntados e a exceção sobe para quem chamou."""
    pronto = _arquivo("id-70", "2026-09 - BTG.pdf", "md5-70")

    def _mudancas_quebradas(_token):
        raise RuntimeError("HttpError 500 em changes.list")

    levantou = None
    with ambiente() as (espiao, _):
        with mock.patch.object(
            M, "_mapa_pastas", lambda: {"pasta-btg": (4, "BTG")}
        ), mock.patch.object(
            M.drive, "listar_pdfs", lambda pasta_id, incluir_csv=False: [pronto]
        ), mock.patch.object(M.drive, "listar_mudancas", _mudancas_quebradas):
            M.estado.page_token = "t"
            try:
                M._varrer(completa=True)
            except RuntimeError as e:
                levantou = str(e)
        fila_depois = M.estado.avisos_adiados
        avisado = state.get_state_manager().ja_notificou_gastos("id-70", "md5-70")

    assert levantou == "HttpError 500 em changes.list"
    assert espiao.chamadas == 1
    assert espiao.corpo()["file_id"] == "id-70"
    assert avisado is True
    assert fila_depois is None


def test_avisos_da_pasta_04_saem_depois_dos_renames_da_mesma_varredura():
    """Dentro de UMA varredura, todos os renames vêm antes do primeiro POST —
    mesmo com a Pasta 04 listada primeiro. É só isso: a fila roda com o lock
    preso, então um arquivo que chega durante a espera de um 409 fica para a
    varredura seguinte. E a idempotência não muda: a varredura seguinte não
    reenvia."""
    mapa = {"pasta-btg": (4, "BTG"), "pasta-01": (1, None)}  # Pasta 04 primeiro
    pdfs = {
        "pasta-btg": [_arquivo("id-btg", "extrato-btg.pdf", "md5-btg")],
        "pasta-01": [_arquivo("id-01", "nota.pdf", "md5-01")],
    }
    nomes = {4: "2026-09 - BTG.pdf", 1: "09-01 - PEJA11 - Compra - R$1.000,00.pdf"}
    eventos: list = []

    def _rename(file_id, _novo):
        eventos.append(("rename", file_id))

    def _worker(requisicao):
        eventos.append(("aviso", json.loads(requisicao.data)["file_id"]))
        return _Resposta(202)

    with ambiente() as (_, mensagens):
        with mock.patch.object(M, "_mapa_pastas", lambda: mapa), mock.patch.object(
            M.drive, "listar_pdfs", lambda pasta_id, incluir_csv=False: pdfs[pasta_id]
        ), mock.patch.object(
            M.drive, "listar_mudancas", lambda token: ([], token)
        ), mock.patch.object(M.drive, "download_pdf", lambda _id: b"%PDF-fake"), (
            mock.patch.object(
                M, "determinar_nome_novo",
                lambda numero, *_a, **_k: Resultado(nomes[numero], []),
            )
        ), mock.patch.object(
            M.drive, "nome_sem_colisao", lambda pasta, nome, ignorar_id=None: nome
        ), mock.patch.object(M.drive, "rename_file", _rename), mock.patch.object(
            G, "_abrir_url", _worker
        ):
            M.estado.page_token = "t"
            M._varrer(completa=True)
            M._varrer(completa=True)
        fila_depois = getattr(M.estado, "avisos_adiados", None)

    assert eventos == [("rename", "id-btg"), ("rename", "id-01"), ("aviso", "id-btg")]
    assert fila_depois is None  # fora da varredura o aviso volta a sair na hora
    assert not any("não respondeu" in m for m in mensagens)


# ============================================================================
# Execução como script (mesmo estilo do test_regressao_parser.py)
# ============================================================================


def main() -> int:
    logging.disable(logging.CRITICAL)
    testes = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    ok = falhas = 0
    for teste in testes:
        try:
            teste()
        except Exception:
            falhas += 1
            print(f"  FALHA {teste.__name__}")
            print("        " + traceback.format_exc().strip().replace("\n", "\n        "))
        else:
            ok += 1
            print(f"  ok    {teste.__name__}")

    print(f"\n{ok} OK / {falhas} FALHA")
    return 1 if falhas else 0


if __name__ == "__main__":
    raise SystemExit(main())
