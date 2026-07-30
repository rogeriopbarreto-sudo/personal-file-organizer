"""Regressão do parser contra PDFs reais já renomeados corretamente.

A ideia: o nome atual dos arquivos nas pastas do Drive (sincronizadas localmente)
É a verdade. Se o parser não reproduz exatamente o nome que já está lá, ele
regrediu.

Como rodar (precisa da pasta do Drive sincronizada e do pdftotext no PATH):

    python app/tests/test_regressao_parser.py

Para apontar para outro lugar:

    PFO_PASTA_RAIZ="D:/..." python app/tests/test_regressao_parser.py
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
CASOS = [
    (1, "01_BTG Notas de Corretagem", None),
    (2, "02_Relatorio de Performance", None),
    (3, "03_Extrato Investimentos", None),
    (4, "04_BTG Extratos Banking/BTG", "BTG"),
    (4, "04_BTG Extratos Banking/Itau", "Itau"),
]


def main() -> int:
    logging.disable(logging.CRITICAL)
    if not RAIZ.is_dir():
        print(f"Pasta não encontrada: {RAIZ}")
        return 2

    ok = falhas = pulados = 0
    for numero, relativo, banco in CASOS:
        pasta = RAIZ / relativo
        if not pasta.is_dir():
            print(f"!! ausente: {pasta}")
            continue
        print(f"\n== Pasta {numero}: {relativo}")
        for arquivo in sorted(pasta.glob("*.pdf")):
            nome = arquivo.name
            # Só valida o que já está no padrão final — esse é o ground truth.
            if not P.valida_padrão_final(numero, nome):
                pulados += 1
                continue
            try:
                obtido = P.determinar_nome_novo(
                    numero, banco, nome, arquivo.read_bytes()
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
