"""Testes do suporte a CSV na Pasta 04 (extrato de conta corrente, ex.: Bradesco).

Cobre: `parse_extrato_csv`, o nome final com extensão `.csv` via
`determinar_nome_novo`/`nome_pasta_04`, a idempotência do padrão final, o
filtro de mimeType em `drive_client.py` (CSV só entra na Pasta 04) e o
descarte de CSV fora da Pasta 04 dentro de `main._varrer`.

Não toca rede nem credencial — `_service()` do Drive é substituído por um
mock. Roda sob pytest:

    pytest app/tests/test_csv_pasta_04.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Precisa vir ANTES de importar app.main/app.state: o STATE_DIR é lido no import.
os.environ.setdefault("STATE_DIR", tempfile.mkdtemp(prefix="pfo-teste-csv-"))

from app import drive_client as D  # noqa: E402
from app import llm_fallback as L  # noqa: E402
from app import main as M  # noqa: E402
from app import parser as P  # noqa: E402

# ============================================================================
# CSVs sintéticos (formato do internet banking do Bradesco)
# ============================================================================

CSV_MULTIMES = (
    "Extrato de: Ag: 1234 | Conta: 123456-7;;;;;\r\n"
    "Data;Histórico;Docto.;Crédito (R$);Débito (R$);Saldo (R$)\r\n"
    "31/12/2025;COD. LANC. 0;0;0,00; ;521,40\r\n"
    "02/01/2026;GASTOS CARTAO DE CREDITO;9081726; ;354,60;166,80\r\n"
    ";;;;;\r\n"
    "Filtro de resultados - Movimentação entre:  01/01/2026 e 30/06/2026;;;;;\r\n"
    ";;;;;\r\n"
    "Os dados acima tem como base 05/09/2026 às 22:01 e estão sujeitos a "
    "alterações.;;;;;\r\n"
).encode("utf-8-sig")

CSV_UM_MES = (
    CSV_MULTIMES.decode("utf-8-sig")
    .replace("01/01/2026 e 30/06/2026", "01/07/2026 e 31/07/2026")
    .encode("utf-8-sig")
)

CSV_SEM_PERIODO = (
    "Extrato de: Ag: 1234 | Conta: 123456-7;;;;;\r\n"
    "Data;Histórico;Docto.;Crédito (R$);Débito (R$);Saldo (R$)\r\n"
    "31/12/2025;COD. LANC. 0;0;0,00; ;521,40\r\n"
).encode("utf-8-sig")


# ============================================================================
# parser.py — parse_extrato_csv / _decode_csv
# ============================================================================


def test_parse_extrato_csv_multimes():
    bank = P.parse_extrato_csv(P._decode_csv(CSV_MULTIMES))
    assert (bank.periodo_inicio, bank.periodo_fim) == ("26-01", "26-06")


def test_parse_extrato_csv_um_mes_ainda_e_periodo():
    """Início e fim no mesmo mês continuam virando período, não fatura."""
    bank = P.parse_extrato_csv(P._decode_csv(CSV_UM_MES))
    assert (bank.periodo_inicio, bank.periodo_fim) == ("26-07", "26-07")
    assert bank.ano_mes is None


def test_parse_extrato_csv_sem_linha_de_filtro():
    bank = P.parse_extrato_csv(P._decode_csv(CSV_SEM_PERIODO))
    assert bank.periodo_inicio is None
    assert bank.periodo_fim is None


def test_decode_csv_cai_para_cp1252():
    """Bytes que não são UTF-8 válido (nem com BOM) caem para cp1252."""
    bruto = "Histórico;café;;;;\r\n".encode("cp1252")
    assert P._decode_csv(bruto) == "Histórico;café;;;;\r\n"


# ============================================================================
# parser.py — determinar_nome_novo / nome_pasta_04 / valida_padrão_final
# ============================================================================


def test_determinar_nome_novo_csv_multimes():
    resultado = P.determinar_nome_novo(
        4, "Bradesco", "uuid-qualquer", CSV_MULTIMES, mime_type="text/csv"
    )
    assert resultado.nome == "26-01 - 26-06 - Banking.csv"
    assert resultado.campos_faltando == []


def test_determinar_nome_novo_csv_um_mes():
    """Mime type alternativo (application/csv) também é reconhecido."""
    resultado = P.determinar_nome_novo(
        4, "Bradesco", "uuid-qualquer", CSV_UM_MES, mime_type="application/csv"
    )
    assert resultado.nome == "26-07 - 26-07 - Banking.csv"
    assert resultado.campos_faltando == []


def test_determinar_nome_novo_csv_sem_dados():
    """Sem a linha de filtro, cai no mesmo caminho de 'sem dados' do PDF."""
    resultado = P.determinar_nome_novo(
        4, "Bradesco", "uuid-qualquer", CSV_SEM_PERIODO, mime_type="text/csv"
    )
    assert resultado.nome is None
    assert resultado.campos_faltando == ["ano_mes"]


def test_determinar_nome_novo_csv_sem_periodo_nunca_aciona_fallback_llm(monkeypatch):
    """Regressão: CSV sem a linha de período não pode cair no fallback de LLM.

    Antes, a falta de "Filtro de resultados" caía no mesmo fallback
    compartilhado da Pasta 01/02 — que manda o texto inteiro (agência, conta,
    todos os lançamentos) pra Anthropic API e já devolveu, no passado, um
    nome no formato de fatura mensal ("2026-03 - Bradesco.csv") que
    `valida_padrão_final` aceitava sem checar se fazia sentido pro CSV
    original. Pra CSV, "sem dados" tem que ser definitivo — mesmo com
    USAR_LLM_FALLBACK ligado (aqui simulado: o `completar_campos` real é
    passado, só que forçado a estourar se for chamado).
    """
    monkeypatch.setattr(
        L,
        "completar_campos",
        mock.Mock(side_effect=AssertionError(
            "fallback de LLM não deveria ser chamado para CSV"
        )),
    )

    resultado = P.determinar_nome_novo(
        4,
        "Bradesco",
        "uuid-qualquer",
        CSV_SEM_PERIODO,
        completar=L.completar_campos,
        mime_type="text/csv",
    )

    assert resultado.nome is None
    assert resultado.campos_faltando == ["ano_mes"]
    assert resultado.usou_llm is False
    L.completar_campos.assert_not_called()


def test_valida_padrao_final_aceita_csv_e_continua_aceitando_pdf():
    assert P.valida_padrão_final(4, "26-01 - 26-06 - Banking.csv") is True
    assert P.valida_padrão_final(4, "2026-09 - Bradesco.csv") is True
    assert P.valida_padrão_final(4, "26-01 - 26-06 - Banking (2).csv") is True
    # Regressão: os nomes em PDF continuam batendo.
    assert P.valida_padrão_final(4, "26-01 - 26-06 - Banking.pdf") is True
    assert P.valida_padrão_final(4, "2026-09 - Bradesco.pdf") is True
    # Nem toda extensão vale.
    assert P.valida_padrão_final(4, "26-01 - 26-06 - Banking.xlsx") is False


# ============================================================================
# drive_client.py — filtro de mimeType (CSV só entra na Pasta 04)
# ============================================================================


def test_listar_pdfs_so_inclui_csv_quando_pedido():
    fake = mock.MagicMock()
    fake.files.return_value.list.return_value.execute.return_value = {"files": []}

    with mock.patch.object(D, "_service", return_value=fake):
        D.listar_pdfs("pasta-01")
        D.listar_pdfs("pasta-04-bradesco", incluir_csv=True)

    chamadas = fake.files.return_value.list.call_args_list
    q_padrao = chamadas[0].kwargs["q"]
    q_com_csv = chamadas[1].kwargs["q"]

    assert "application/pdf" in q_padrao
    assert "text/csv" not in q_padrao and "application/csv" not in q_padrao
    assert "text/plain" not in q_padrao and "application/vnd.ms-excel" not in q_padrao
    assert "application/pdf" in q_com_csv
    assert "text/csv" in q_com_csv and "application/csv" in q_com_csv
    # Carimbo alternativo de navegador/Excel num .csv também entra, só na Pasta 04.
    assert "text/plain" in q_com_csv and "application/vnd.ms-excel" in q_com_csv


def test_listar_mudancas_aceita_pdf_e_csv_mas_descarta_outros_mimetypes():
    fake = mock.MagicMock()
    fake.changes.return_value.list.return_value.execute.return_value = {
        "changes": [
            {
                "file": {
                    "id": "1", "name": "a.pdf", "parents": ["p"],
                    "mimeType": "application/pdf", "md5Checksum": "m1",
                }
            },
            {
                "file": {
                    "id": "2", "name": "b.csv", "parents": ["p"],
                    "mimeType": "text/csv", "md5Checksum": "m2",
                }
            },
            {
                "file": {
                    "id": "3", "name": "c.csv", "parents": ["p"],
                    "mimeType": "application/csv", "md5Checksum": "m3",
                }
            },
            {
                "file": {
                    "id": "4", "name": "d.docx", "parents": ["p"],
                    "mimeType": "application/vnd.google-apps.document",
                    "md5Checksum": "m4",
                }
            },
            {
                "file": {
                    "id": "5", "name": "e.csv", "parents": ["p"],
                    "mimeType": "text/plain", "md5Checksum": "m5",
                }
            },
            {
                "file": {
                    "id": "6", "name": "f.csv", "parents": ["p"],
                    "mimeType": "application/vnd.ms-excel", "md5Checksum": "m6",
                }
            },
        ],
        "newStartPageToken": "token-novo",
    }

    with mock.patch.object(D, "_service", return_value=fake):
        novos, token = D.listar_mudancas("token-velho")

    assert token == "token-novo"
    mimes = {a["id"]: a["mime_type"] for a in novos}
    assert mimes == {
        "1": "application/pdf",
        "2": "text/csv",
        "3": "application/csv",
        "5": "text/plain",
        "6": "application/vnd.ms-excel",
    }


# ============================================================================
# main.py — _varrer descarta CSV fora da Pasta 04 antes de processar
# ============================================================================


def test_varrer_ignora_csv_fora_da_pasta_04():
    """CSV numa pasta que não é a 04 nunca chega a `_processar`; PDF sim."""
    mapa_fixo = {"pasta-01": (1, None), "pasta-04-bradesco": (4, "Bradesco")}
    novos = [
        {
            "id": "csv-fora", "name": "uuid-fora", "parents": ["pasta-01"],
            "md5": "md5-fora", "mime_type": "text/csv",
        },
        {
            "id": "pdf-fora", "name": "nota.pdf", "parents": ["pasta-01"],
            "md5": "md5-pdf", "mime_type": "application/pdf",
        },
        {
            "id": "csv-dentro", "name": "uuid-dentro", "parents": ["pasta-04-bradesco"],
            "md5": "md5-dentro", "mime_type": "text/csv",
        },
    ]
    chamados: list = []

    def _fake_processar(file_id, nome, pasta_id, numero, banco, md5="", mime_type=D.MIME_PDF):
        chamados.append((file_id, numero, mime_type))
        return False

    with mock.patch.object(M, "_mapa_pastas", lambda: mapa_fixo), mock.patch.object(
        M.drive, "listar_mudancas", lambda token: (novos, "token-novo")
    ), mock.patch.object(M, "_processar", _fake_processar):
        M.estado.page_token = "token-velho"
        M._varrer(completa=False)

    # O CSV da Pasta 01 foi descartado antes de _processar; o PDF e o CSV da
    # Pasta 04 passaram.
    assert [c[0] for c in chamados] == ["pdf-fora", "csv-dentro"]
    assert chamados[1] == ("csv-dentro", 4, "text/csv")


def main() -> int:
    import logging
    import traceback

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
