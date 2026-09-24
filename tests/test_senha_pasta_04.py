"""Testes da senha por banco na Pasta 04 (fatura Itaú/BTG protegida).

Cobre: o nome da env (`env_senha_pdf`), a leitura da senha (`senha_pdf`), a
segunda tentativa com senha em `determinar_nome_novo` e o repasse da senha
em `main._processar`. Um teste final usa pdftotext + pypdf de verdade e é
pulado se algum dos dois faltar. Roda sob pytest:

    pytest app/tests/test_senha_pasta_04.py
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Precisa vir ANTES de importar app.main/app.state: o STATE_DIR é lido no import.
os.environ.setdefault("STATE_DIR", tempfile.mkdtemp(prefix="pfo-teste-senha-"))

from app import config as C  # noqa: E402
from app import main as M  # noqa: E402
from app import parser as P  # noqa: E402

FATURA = "Vencimento: 01/10/2026\nTotal desta fatura 8.014,03\n"


def test_env_senha_pdf_segue_convencao_do_inbox():
    assert C.env_senha_pdf("Itau") == "ITAU_PDF_PASSWORD"
    assert C.env_senha_pdf("Itaú") == "ITAU_PDF_PASSWORD"
    assert C.env_senha_pdf("BTG") == "BTG_PDF_PASSWORD"
    assert C.env_senha_pdf("NuBank") == "NUBANK_PDF_PASSWORD"


def test_senha_pdf_le_env_e_ignora_vazia(monkeypatch):
    monkeypatch.setenv("ITAU_PDF_PASSWORD", " 12345 ")
    monkeypatch.setenv("BTG_PDF_PASSWORD", "")
    assert C.senha_pdf("Itau") == "12345"
    assert C.senha_pdf("BTG") is None
    assert C.senha_pdf(None) is None


def _extrator(senha_certa: str):
    """Imita o pdftotext: sem a senha certa, levanta PdfProtegido."""
    chamadas = []

    def extrair(pdf_bytes, primeira_pagina_so=False, senha=None):
        chamadas.append(senha)
        if senha != senha_certa:
            raise P.PdfProtegido("Incorrect password")
        return FATURA

    return extrair, chamadas


def test_pdf_com_senha_e_renomeado_com_a_senha_do_banco(monkeypatch):
    extrair, chamadas = _extrator("12345")
    monkeypatch.setattr(P, "extrair_texto_pdf", extrair)
    r = P.determinar_nome_novo(4, "Itau", "Fatura_MASTERCARD.pdf", b"%PDF", senha="12345")
    assert r.nome == "2026-10 - Itau.pdf"
    assert chamadas == [None, "12345"]  # tenta sem senha primeiro


def test_pdf_com_senha_sem_env_continua_protegido(monkeypatch):
    extrair, chamadas = _extrator("12345")
    monkeypatch.setattr(P, "extrair_texto_pdf", extrair)
    with pytest.raises(P.PdfProtegido):
        P.determinar_nome_novo(4, "Itau", "Fatura_MASTERCARD.pdf", b"%PDF")
    assert chamadas == [None]


def test_pdf_com_senha_errada_continua_protegido(monkeypatch):
    extrair, _ = _extrator("12345")
    monkeypatch.setattr(P, "extrair_texto_pdf", extrair)
    with pytest.raises(P.PdfProtegido):
        P.determinar_nome_novo(4, "Itau", "Fatura_MASTERCARD.pdf", b"%PDF", senha="errada")


def test_pdf_sem_senha_nao_usa_a_senha(monkeypatch):
    chamadas = []

    def extrair(pdf_bytes, primeira_pagina_so=False, senha=None):
        chamadas.append(senha)
        return FATURA

    monkeypatch.setattr(P, "extrair_texto_pdf", extrair)
    r = P.determinar_nome_novo(4, "Itau", "fatura.pdf", b"%PDF", senha="12345")
    assert r.nome == "2026-10 - Itau.pdf"
    assert chamadas == [None]


def test_processar_passa_a_senha_so_na_pasta_04(monkeypatch):
    monkeypatch.setenv("ITAU_PDF_PASSWORD", "12345")
    vistos = []

    def falso(numero, banco, nome, pdf, completar=None, mime_type=None, senha=None):
        vistos.append((numero, senha))
        return P.Resultado(None, ["ano_mes"])

    with mock.patch.object(M, "determinar_nome_novo", falso), \
         mock.patch.object(M.drive, "download_pdf", return_value=b"%PDF"), \
         mock.patch.object(M, "_avisar_gastos"), \
         mock.patch.object(M.notifier, "notificar_nao_reconhecido", create=True), \
         mock.patch.object(M.notifier, "enviar", create=True):
        M._processar("id-s1", "Fatura_X.pdf", "pasta-itau", 4, "Itau", "md5-s1")
        M._processar("id-s2", "nota.pdf", "pasta-01", 1, None, "md5-s2")

    assert vistos == [(4, "12345"), (1, None)]


def _pdf_com_senha(senha: str) -> bytes:
    pypdf = pytest.importorskip("pypdf")
    try:
        from reportlab.pdfgen import canvas  # noqa: F401
    except ImportError:
        canvas = None
    escritor = pypdf.PdfWriter()
    if canvas is None:
        pytest.skip("reportlab ausente — sem como gerar PDF com texto")
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, "Vencimento: 01/10/2026")
    c.save()
    escritor.append(pypdf.PdfReader(io.BytesIO(buf.getvalue())))
    escritor.encrypt(user_password=senha, algorithm="RC4-128")
    saida = io.BytesIO()
    escritor.write(saida)
    return saida.getvalue()


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="pdftotext ausente")
def test_pdftotext_de_verdade_abre_com_a_senha():
    pdf = _pdf_com_senha("12345")
    with pytest.raises(P.PdfProtegido):
        P.extrair_texto_pdf(pdf, primeira_pagina_so=True)
    assert "01/10/2026" in P.extrair_texto_pdf(pdf, primeira_pagina_so=True, senha="12345")
    r = P.determinar_nome_novo(4, "Itau", "Fatura.pdf", pdf, senha="12345")
    assert r.nome == "2026-10 - Itau.pdf"
