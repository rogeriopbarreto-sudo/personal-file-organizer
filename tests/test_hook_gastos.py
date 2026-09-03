"""Testes do hook que avisa o worker do dashboard de gastos.

Não tocam a rede nem o Drive: o POST no worker e o envio ao Telegram viram
espiões, e o cache de arquivos processados vai para uma pasta temporária.

Como rodar (não precisa de PDF nem de credencial):

    python app/tests/test_hook_gastos.py

Também roda sob pytest, se preferir:

    pytest app/tests/test_hook_gastos.py
"""
from __future__ import annotations

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
from app.parser import Resultado  # noqa: E402

URL = "https://gastos.teste"
SECRET = "segredo-de-teste"
CONFIG_BASE = G.settings


# ============================================================================
# Andaimes
# ============================================================================


class _Resposta:
    """Resposta HTTP de mentira, no formato que o urlopen devolve."""

    def __init__(self, status: int) -> None:
        self.status = status

    def getcode(self) -> int:
        return self.status

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


@contextmanager
def ambiente(
    url: str = URL,
    secret: str = SECRET,
    dry_run: bool = False,
    erro: Exception | None = None,
    status: int = 200,
):
    """Cache zerado, config isolada, POST e Telegram espionados."""
    _zerar_cache()
    config = replace(
        CONFIG_BASE,
        gastos_worker_url=url,
        gastos_process_secret=secret,
        dry_run=dry_run,
    )
    espiao = Espiao(status=status, erro=erro)
    mensagens: list[str] = []

    def _tg(mensagem: str) -> bool:
        mensagens.append(mensagem)
        return True

    with mock.patch.object(G, "settings", config), mock.patch.object(
        M, "settings", config
    ), mock.patch.object(G, "_abrir_url", espiao), mock.patch.object(
        notifier, "tg_send", _tg
    ):
        yield espiao, mensagens


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
