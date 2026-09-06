"""Regressão do parser contra PDFs reais já renomeados corretamente.

A ideia: o nome atual dos arquivos nas pastas do Drive (sincronizadas localmente)
É a verdade. Se o parser não reproduz exatamente o nome que já está lá, ele
regrediu.

Como rodar (precisa da pasta do Drive sincronizada e do pdftotext no PATH):

    python app/tests/test_regressao_parser.py

Para apontar para outro lugar:

    PFO_PASTA_RAIZ="D:/..." python app/tests/test_regressao_parser.py

Além do `main()` acima (que exige a pasta do Drive), este arquivo também tem
testes de unidade de `parse_banking` com trechos sintéticos — esses rodam sob
`pytest` em qualquer máquina, sem precisar de PDF nem de `PFO_PASTA_RAIZ`.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import parser as P  # noqa: E402

RAIZ = Path(
    os.environ.get(
        "PFO_PASTA_RAIZ",
        Path.home() / "My Drive" / "03_Documentos" / "05_Comprovantes Op Fiannceira",
    )
)

# (número da pasta, caminho relativo, nome do banco)
CASOS_FIXOS = [
    (1, "01_BTG Notas de Corretagem", None),
    (2, "02_Relatorio de Performance", None),
    (3, "03_Extrato Investimentos", None),
]

PASTA_04 = "04_BTG Extratos Banking"


def casos() -> list[tuple[int, str, str | None]]:
    """Casos a testar, com as subpastas de banco descobertas — não listadas.

    O serviço faz o mesmo em runtime (`drive.listar_subpastas`) e tira o banco
    do nome da subpasta, então um banco novo (Bradesco, Nubank) entra no teste
    sozinho, sem mexer aqui.
    """
    lista = list(CASOS_FIXOS)
    raiz_04 = RAIZ / PASTA_04
    if raiz_04.is_dir():
        for sub in sorted(p for p in raiz_04.iterdir() if p.is_dir()):
            lista.append((4, f"{PASTA_04}/{sub.name}", sub.name))
    return lista


# ============================================================================
# Testes — unidade (parse_banking), com trechos sintéticos
# ============================================================================
#
# Não usam PDF nem a pasta do Drive — rodam em qualquer máquina, sem
# PFO_PASTA_RAIZ. Cobrem os formatos de vencimento por banco, incluídos aqui
# porque test_regressao_parser.py é o arquivo que já reúne os casos da
# Pasta 04.


def test_parse_banking_nubank_data_por_extenso_abreviada():
    """Nubank: "Data de vencimento: 03 AGO 2026" — sem "/", mês abreviado.

    O trecho inclui, mais abaixo, a mesma frase de declaração legal real
    ("...até a data de vencimento da fatura de dezembro de 2025") que faz o
    fallback `fatura de <mês> de <ano>` casar com prosa não relacionada. O
    vencimento explícito tem que ganhar dessa decoy.
    """
    texto = """
    NUBANK

    Olá, Fulano.
    Esta é a sua fatura de
    agosto, no valor de
    R$ 1.234,56

    Data de vencimento: 03 AGO 2026
    Limite total do cartão de crédito: R$ 10.000,00

    Ao autorizar o pagamento parcial, você reconhece que os encargos
    incidem até a data de vencimento da fatura de dezembro de 2025.
    """
    bank = P.parse_banking(texto)
    assert bank.ano_mes == "2026-08"


def test_parse_banking_bradesco_rotulo_e_data_em_linhas_separadas():
    """Bradesco: rótulo " Vencimento" numa linha, a data na linha seguinte."""
    texto = """
    BANCO BRADESCO S.A.

                                                       Total da fatura  Vencimento
     Vencimento
    01/09/2026

    Valor total da fatura: R$ 987,65
    """
    bank = P.parse_banking(texto)
    assert bank.ano_mes == "2026-09"


def test_parse_banking_vencimento_completo_ganha_do_fallback_fatura():
    """Formato já coberto (BTG/Itaú): "Vencimento: DD/MM/YYYY" na mesma linha."""
    texto = """
    Vencimento: 05/08/2026
    ...
    conforme consta da fatura de julho de 2025, este débito é anterior.
    """
    bank = P.parse_banking(texto)
    assert bank.ano_mes == "2026-08"


def test_parse_banking_vencimento_sem_ano_usa_fatura():
    """Formato já coberto (BTG/Itaú): "Vencimento: DD/MM" + "fatura de <mês> de <ano>"."""
    texto = """
    Vencimento: 05/08
    ...
    Esta é a fatura de agosto de 2026.
    """
    bank = P.parse_banking(texto)
    assert bank.ano_mes == "2026-08"


def test_parse_banking_periodo_multimes():
    """Formato já coberto: extrato "Período de DD/MM/YYYY a DD/MM/YYYY"."""
    texto = """
    Período de 01/07/2026 a 31/08/2026
    """
    bank = P.parse_banking(texto)
    assert (bank.periodo_inicio, bank.periodo_fim) == ("26-07", "26-08")


def main() -> int:
    logging.disable(logging.CRITICAL)
    if not RAIZ.is_dir():
        print(f"Pasta não encontrada: {RAIZ}")
        return 2

    ok = falhas = pulados = 0
    for numero, relativo, banco in casos():
        pasta = RAIZ / relativo
        if not pasta.is_dir():
            print(f"!! ausente: {pasta}")
            continue
        print(f"\n== Pasta {numero}: {relativo}")
        arquivos = sorted(pasta.glob("*.pdf")) + sorted(pasta.glob("*.csv"))
        for arquivo in arquivos:
            nome = arquivo.name
            # Extensão → mimeType: só a Pasta 04 tem CSV (extrato de conta
            # corrente); o resto é sempre PDF. `determinar_nome_novo` decide
            # o parser certo (CSV puro vs. pdftotext) a partir desse mime.
            mime_type = "text/csv" if arquivo.suffix.lower() == ".csv" else P.MIME_PDF
            # Só valida o que já está no padrão final — esse é o ground truth.
            if not P.valida_padrão_final(numero, nome):
                pulados += 1
                continue
            try:
                obtido = P.determinar_nome_novo(
                    numero, banco, nome, arquivo.read_bytes(), mime_type=mime_type
                ).nome
            except P.PdfProtegido:
                pulados += 1
                print(f"  SENHA {nome}")
                continue
            if obtido == nome:
                ok += 1
            else:
                falhas += 1
                print(f"  FALHA {nome}\n        obtido: {obtido}")

    print(f"\n{ok} OK / {falhas} FALHA / {pulados} pulados")
    return 1 if falhas else 0


if __name__ == "__main__":
    raise SystemExit(main())
