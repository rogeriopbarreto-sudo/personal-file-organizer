"""Parsing determinístico de PDFs: extrai campos, valida padrões, gera nomes novos.

Regras fixas por pasta, conforme instrucoes-claude-code-organizador-drive.md.
"""
from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime

log = logging.getLogger("file_organizer.parser")


def extrair_texto_pdf(pdf_bytes: bytes, primeira_pagina_so: bool = False) -> str:
    """Extrai texto do PDF via pdftotext.

    Se primeira_pagina_so=True, extrai só a primeira página.
    Retorna string vazia se falhar.
    """
    try:
        # Usar pdftotext com -layout para preservar estrutura
        flags = ["-layout"]
        if primeira_pagina_so:
            flags.extend(["-f", "1", "-l", "1"])

        result = subprocess.run(
            ["pdftotext"] + flags + ["-", "-"],
            input=pdf_bytes,
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode != 0:
            log.warning("pdftotext retornou código %d: %s", result.returncode, result.stderr)
            return ""

        return result.stdout

    except FileNotFoundError:
        log.error("pdftotext não encontrado — instalar poppler-utils")
        return ""
    except subprocess.TimeoutExpired:
        log.error("pdftotext timeout (PDF muito grande?)")
        return ""
    except Exception as e:
        log.error("Erro ao extrair texto: %s", e)
        return ""


def br_float(s: str) -> float | None:
    """Converte string em formato BR (1.234,56) para float, ou None se inválido."""
    if not s:
        return None
    try:
        # Remove pontos (separador de milhar) e troca vírgula por ponto (decimal)
        s = s.strip().replace(".", "").replace(",", ".")
        return float(s)
    except ValueError:
        return None


# ============================================================================
# Pasta 01: BTG Notas de Corretagem
# ============================================================================


@dataclass
class NotaBTG:
    data: str | None  # MM-DD
    ativo: str | None
    tipo_op: str | None  # Compra/Venda/Juros
    valor: str | None  # R$X.XXX,XX


def parse_pasta_01_btg_notas(pdf_bytes: bytes) -> NotaBTG:
    """Extrai campos de Nota de Corretagem BTG."""
    texto = extrair_texto_pdf(pdf_bytes)
    if not texto:
        return NotaBTG(None, None, None, None)

    nota = NotaBTG(None, None, None, None)

    # Data da operação: procurar por linha com padrão "Data da Operação: DD/MM/YYYY" ou similar
    for linha in texto.split("\n"):
        # Flexível: procura "operação" com números
        if "operação" in linha.lower() or "data" in linha.lower():
            # Procura DD/MM/YYYY
            match = re.search(r"(\d{2})/(\d{2})/(\d{4})", linha)
            if match:
                # Extrai mês e dia
                nota.data = f"{match.group(2)}-{match.group(1)}"
                break

    # Tipo de operação: buscar "Compra", "Venda" ou "Juros"
    for linha in texto.split("\n"):
        if re.search(r"\b(Compra|Venda|Juros)\b", linha):
            match = re.search(r"\b(Compra|Venda|Juros)\b", linha)
            if match:
                nota.tipo_op = match.group(1)
                break

    # Ativo: procurar por código/nome do papel (complexo, heurístico)
    # Padrão: linha com maiúsculas, dígitos, números
    for linha in texto.split("\n"):
        linha = linha.strip()
        # Ativo pode ser algo como "NTN-B1", "PEJA11", "CDCA-24G02736845"
        if re.search(r"[A-Z0-9]{2,}", linha) and len(linha) < 30:
            # Tenta achar entre aspas ou parênteses, ou isolado
            match = re.search(r"([A-Z][A-Z0-9-]{2,})", linha)
            if match:
                nota.ativo = match.group(1)
                break

    # Valor: buscar valor em formato R$ X.XXX,XX ou similar
    for linha in texto.split("\n"):
        match = re.search(r"R\$\s*([\d.]+,\d{2})", linha)
        if match:
            nota.valor = f"R${match.group(1)}"
            break

    return nota


def nome_pasta_01(nota: NotaBTG, filename: str) -> str | None:
    """Gera nome final para Pasta 01, ou None se faltam campos."""
    # Usar ?? para campos faltantes
    data = nota.data or "??"
    ativo = nota.ativo or "??"
    tipo = nota.tipo_op or "??"
    valor = nota.valor or "R$??"

    # Se NENHUM campo reconhecido, retornar None (não renomear)
    if all(x == "??" or x == "R$??" for x in [data, ativo, tipo, valor]):
        return None

    nome = f"{data} - {ativo} - {tipo} - {valor}.pdf"
    return nome


# ============================================================================
# Pasta 02: Relatório de Performance
# ============================================================================


@dataclass
class RelatorioPerformance:
    data_geracao: str | None  # yy-mm-dd
    periodo_inicio: str | None  # yy-mm
    periodo_fim: str | None  # yy-mm


def parse_pasta_02_performance(pdf_bytes: bytes) -> RelatorioPerformance:
    """Extrai campos de Relatório de Performance (primeira página)."""
    texto = extrair_texto_pdf(pdf_bytes, primeira_pagina_so=True)
    if not texto:
        return RelatorioPerformance(None, None, None)

    rel = RelatorioPerformance(None, None, None)

    # Data de geração: "Gerado em DD/MM/YYYY"
    for linha in texto.split("\n"):
        match = re.search(r"Gerado em (\d{2})/(\d{2})/(\d{4})", linha)
        if match:
            # yy-mm-dd
            dia, mes, ano = match.group(2), match.group(1), match.group(3)
            yy = ano[-2:]
            rel.data_geracao = f"{yy}-{mes}-{dia}"
            break

    # Período: "Período de DD/MM/YYYY a DD/MM/YYYY" ou "PerÃ­odo de ..."
    for linha in texto.split("\n"):
        # Flexível a acentos
        if "período" in linha.lower() or "periodo" in linha.lower():
            # Procurar padrão de datas
            matches = re.findall(r"(\d{2})/(\d{2})/(\d{4})", linha)
            if len(matches) >= 2:
                # Primeira data = início, segunda = fim
                dia1, mes1, ano1 = matches[0]
                dia2, mes2, ano2 = matches[1]
                yy1, yy2 = ano1[-2:], ano2[-2:]
                rel.periodo_inicio = f"{yy1}-{mes1}"
                rel.periodo_fim = f"{yy2}-{mes2}"
                break

    return rel


def nome_pasta_02(rel: RelatorioPerformance, filename: str) -> str | None:
    """Gera nome final para Pasta 02, ou None se faltam campos."""
    data = rel.data_geracao or "??"
    inicio = rel.periodo_inicio or "??"
    fim = rel.periodo_fim or "??"

    if all(x == "??" for x in [data, inicio, fim]):
        return None

    nome = f"{data} - Performance - {inicio} - {fim}.pdf"
    return nome


# ============================================================================
# Pasta 03: Extrato Investimentos
# ============================================================================


@dataclass
class ExtratoInvestimentos:
    ano_mes: str | None  # yyyy-mm


def parse_pasta_03_extrato(pdf_bytes: bytes) -> ExtratoInvestimentos:
    """Extrai período do Extrato Investimentos (primeira página)."""
    texto = extrair_texto_pdf(pdf_bytes, primeira_pagina_so=True)
    if not texto:
        return ExtratoInvestimentos(None)

    est = ExtratoInvestimentos(None)

    # Período: "Período de DD/MM/YY a DD/MM/YY" ou "PerÃ­odo de ..."
    for linha in texto.split("\n"):
        if "período" in linha.lower() or "periodo" in linha.lower():
            matches = re.findall(r"(\d{2})/(\d{2})/(\d{2})", linha)
            if matches:
                # Última data (fim do mês)
                _, mes, ano = matches[-1]
                # Converter YY para YYYY (assumir 20XX)
                yyyy = f"20{ano}"
                est.ano_mes = f"{yyyy}-{mes}"
                break

    return est


def nome_pasta_03(est: ExtratoInvestimentos, filename: str) -> str | None:
    """Gera nome final para Pasta 03, ou None se faltam campos."""
    if not est.ano_mes:
        return None
    return f"{est.ano_mes}.pdf"


# ============================================================================
# Pasta 04: BTG Extratos Banking (subpastas por banco)
# ============================================================================


@dataclass
class ExtratoBank:
    ano_mes: str | None  # yyyy-mm para fatura, None para multi-mês
    periodo_inicio: str | None  # yy-mm para multi-mês
    periodo_fim: str | None  # yy-mm para multi-mês
    eh_fatura_mensal: bool  # True se é fatura de 1 mês, False se é extrato multi-mês


def parse_pasta_04_banking_btg(pdf_bytes: bytes) -> ExtratoBank:
    """Parser para BTG (fatura bancária, vencimento em DD/MM)."""
    texto = extrair_texto_pdf(pdf_bytes, primeira_pagina_so=True)
    if not texto:
        return ExtratoBank(None, None, None, True)

    bank = ExtratoBank(None, None, None, True)

    # Data de vencimento: "Vencimento: DD/MM" (sem ano)
    vencimento_dia, vencimento_mes = None, None
    for linha in texto.split("\n"):
        match = re.search(r"Vencimento:\s*(\d{2})/(\d{2})", linha)
        if match:
            vencimento_dia, vencimento_mes = match.group(1), match.group(2)
            break

    # Mês/ano da fatura: "fatura de <Mês por extenso> de <Ano>"
    fatura_mes, fatura_ano = None, None
    meses_pt = {
        "janeiro": "01", "fevereiro": "02", "março": "03", "março": "03",
        "abril": "04", "maio": "05", "junho": "06", "julho": "07",
        "agosto": "08", "setembro": "09", "outubro": "10", "novembro": "11", "dezembro": "12"
    }
    for linha in texto.split("\n"):
        match = re.search(r"fatura de (\w+) de (\d{4})", linha)
        if match:
            mes_nome = match.group(1).lower()
            fatura_ano = match.group(2)
            fatura_mes = meses_pt.get(mes_nome)
            break

    # Se temos vencimento + ano da fatura, montar a data
    if vencimento_mes and fatura_ano:
        bank.ano_mes = f"{fatura_ano}-{vencimento_mes}"
        bank.eh_fatura_mensal = True

    return bank


def parse_pasta_04_banking_itau(pdf_bytes: bytes) -> ExtratoBank:
    """Parser para Itaú (data de vencimento completa: DD/MM/YYYY)."""
    texto = extrair_texto_pdf(pdf_bytes, primeira_pagina_so=True)
    if not texto:
        return ExtratoBank(None, None, None, True)

    bank = ExtratoBank(None, None, None, True)

    # Data de vencimento: "Vencimento: DD/MM/YYYY"
    for linha in texto.split("\n"):
        match = re.search(r"Vencimento:\s*(\d{2})/(\d{2})/(\d{4})", linha)
        if match:
            _, mes, ano = match.group(1), match.group(2), match.group(3)
            bank.ano_mes = f"{ano}-{mes}"
            bank.eh_fatura_mensal = True
            break

    return bank


def nome_pasta_04(bank: ExtratoBank, nome_banco: str, filename: str) -> str | None:
    """Gera nome final para Pasta 04 (detecta fatura mensal vs. extrato multi-mês)."""
    # Se parece extrato (nome contém "extrato", ou múltiplos períodos)
    if "extrato" in filename.lower() and bank.periodo_inicio and bank.periodo_fim:
        # Multi-mês
        return f"{bank.periodo_inicio} - {bank.periodo_fim} - Banking.pdf"

    # Fatura mensal
    if bank.ano_mes and bank.eh_fatura_mensal:
        return f"{bank.ano_mes} - {nome_banco}.pdf"

    return None


# ============================================================================
# Orquestração: match folder + parse
# ============================================================================


def determinar_nome_novo(
    folder_num: int, nome_banco: str | None, nome_arquivo: str, pdf_bytes: bytes
) -> str | None:
    """Determina nome novo para arquivo conforme a pasta.

    Retorna None se não conseguir reconhecer (não renomear nesse caso).
    folder_num: 1, 2, 3 ou 4.
    nome_banco: para pasta 4 (ex: "BTG", "Itau"), None pra outras.
    """
    if folder_num == 1:
        nota = parse_pasta_01_btg_notas(pdf_bytes)
        return nome_pasta_01(nota, nome_arquivo)

    elif folder_num == 2:
        rel = parse_pasta_02_performance(pdf_bytes)
        return nome_pasta_02(rel, nome_arquivo)

    elif folder_num == 3:
        est = parse_pasta_03_extrato(pdf_bytes)
        return nome_pasta_03(est, nome_arquivo)

    elif folder_num == 4:
        # Tentar parser específico do banco
        if nome_banco == "BTG":
            bank = parse_pasta_04_banking_btg(pdf_bytes)
        elif nome_banco == "Itau":
            bank = parse_pasta_04_banking_itau(pdf_bytes)
        else:
            # Banco novo: retornar None (será tratado no fallback LLM ou notificação)
            log.warning("Banco '%s' sem adapter — passará pro fallback LLM", nome_banco)
            return None

        return nome_pasta_04(bank, nome_banco or "Unknown", nome_arquivo)

    return None


def valida_padrão_final(folder_num: int, nome: str) -> bool:
    """Verifica se nome já bate com padrão final (idempotência)."""
    if folder_num == 1:
        return bool(re.match(r"^\d{2}-\d{2} - .+ - (Compra|Venda|Juros) - R\$[\d.]+,\d{2}\.pdf$", nome))
    elif folder_num == 2:
        return bool(re.match(r"^\d{2}-\d{2}-\d{2} - Performance - \d{2}-\d{2} - \d{2}-\d{2}\.pdf$", nome))
    elif folder_num == 3:
        return bool(re.match(r"^\d{4}-\d{2}\.pdf$", nome))
    elif folder_num == 4:
        mensal = bool(re.match(r"^\d{4}-\d{2} - .+\.pdf$", nome))
        multi_mes = bool(re.match(r"^\d{2}-\d{2} - \d{2}-\d{2} - Banking\.pdf$", nome))
        return mensal or multi_mes
    return False
