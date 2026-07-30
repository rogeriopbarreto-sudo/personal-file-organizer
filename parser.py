"""Parsing determinístico de PDFs: extrai campos, valida padrões, gera nomes novos.

Regras fixas por pasta, conforme instrucoes-claude-code-organizador-drive.md.

Nota sobre a estratégia de extração (Pasta 01):
    As notas do BTG são TABELAS. Com `pdftotext -layout`, o cabeçalho da coluna
    ("Valor Líquido") fica numa linha e o valor correspondente numa linha
    seguinte, na MESMA posição horizontal. Por isso a extração do valor é feita
    por ALINHAMENTO DE COLUNA — procurar "Valor Líquido" por regex simples
    (`valor líquido\\s+N`) nunca funciona.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass, field

log = logging.getLogger("file_organizer.parser")


class PdfProtegido(Exception):
    """O PDF exige senha — não é possível extrair o texto."""


# ============================================================================
# Extração de texto e normalização
# ============================================================================


def _decode(raw: bytes) -> str:
    """Decodifica saída do pdftotext (UTF-8, com fallback pra Latin-1).

    Builds diferentes do poppler usam encodings diferentes por padrão; a saída
    do Docker (poppler-utils) é UTF-8, mas não dá pra assumir.
    """
    for enc in ("utf-8", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def extrair_texto_pdf(pdf_bytes: bytes, primeira_pagina_so: bool = False) -> str:
    """Extrai texto do PDF via pdftotext, preservando o layout das tabelas.

    Escreve o PDF num arquivo temporário em vez de usar stdin: o poppler aceita
    `-` como entrada, mas o Xpdf (usado em algumas máquinas de desenvolvimento)
    não — passar o caminho funciona nos dois.

    Se primeira_pagina_so=True, extrai só a primeira página.
    Retorna string vazia se falhar.
    """
    flags = ["-layout", "-enc", "UTF-8"]
    if primeira_pagina_so:
        flags.extend(["-f", "1", "-l", "1"])

    fd, caminho = tempfile.mkstemp(suffix=".pdf", prefix="pfo_")
    try:
        with os.fdopen(fd, "wb") as tmp:
            tmp.write(pdf_bytes)

        result = subprocess.run(
            ["pdftotext"] + flags + [caminho, "-"],
            capture_output=True,
            timeout=30,
        )
    except FileNotFoundError:
        log.error("pdftotext não encontrado — instalar poppler-utils")
        return ""
    except subprocess.TimeoutExpired:
        log.error("pdftotext timeout (PDF muito grande?)")
        return ""
    except Exception:
        log.exception("Erro inesperado ao rodar pdftotext")
        return ""
    finally:
        try:
            os.unlink(caminho)
        except OSError:
            pass

    if result.returncode != 0:
        erro = _decode(result.stderr).strip()
        if re.search(r"password|senha|encrypt", erro, re.IGNORECASE):
            raise PdfProtegido(erro[:200])
        log.warning("pdftotext retornou código %d: %s", result.returncode, erro[:200])
        return ""

    return _decode(result.stdout)


def _sem_acentos(s: str) -> str:
    """Remove acentos PRESERVANDO o comprimento da string (1 char → 1 char).

    Preservar o comprimento é essencial: os índices são usados pra localizar
    colunas na saída do `pdftotext -layout`.
    """
    saida = []
    for ch in s:
        decomposto = unicodedata.normalize("NFKD", ch)
        saida.append(decomposto[0] if decomposto else ch)
    return "".join(saida)


def _chave(s: str) -> str:
    """Normaliza pra comparação: sem acentos, minúsculo."""
    return _sem_acentos(s).lower()


def _linhas(texto: str) -> list[str]:
    """Divide em linhas removendo o \\r de arquivos com CRLF."""
    return [linha.rstrip("\r") for linha in texto.split("\n")]


def _compacta(s: str) -> str:
    """Remove TODOS os espaços de uma chave normalizada.

    Alguns PDFs do BTG saem com espaçamento entre letras ("Ge rado e m",
    "Va lor"), então a busca por palavra-chave precisa ignorar espaços.
    """
    return re.sub(r"\s+", "", _chave(s))


def _acha_linha(linhas: list[str], *trechos: str, inicio: int = 0) -> int | None:
    """Índice da primeira linha (a partir de `inicio`) que contém algum trecho.

    Comparação sem acentos, case-insensitive e tolerante a espaços dentro das
    palavras.
    """
    alvos = [(_chave(t), _compacta(t)) for t in trechos]
    for i in range(inicio, len(linhas)):
        norm = _chave(linhas[i])
        compacta = _compacta(linhas[i])
        if any(direto in norm or sem_espaco in compacta for direto, sem_espaco in alvos):
            return i
    return None


def _span_rotulo(linha: str, rotulo: str) -> tuple[int, int] | None:
    """Posição (início, fim) de um rótulo na linha, tolerando espaços extras.

    Casa tanto "Valor Líquido" quanto "Va lor  Lí quido".
    """
    padrao = r"\s*".join(re.escape(c) for c in _compacta(rotulo))
    m = re.search(padrao, _chave(linha))
    return (m.start(), m.end()) if m else None


# ============================================================================
# Valores monetários no formato brasileiro
# ============================================================================

# Aceita 1.234,56 / 1.234,5600 / 234,56 — sempre com vírgula decimal.
_RE_VALOR_BR = re.compile(r"\d{1,3}(?:\.\d{3})+,\d{2,}|\d+,\d{2,}")


def br_float(s: str) -> float | None:
    """Converte string em formato BR (1.234,56) para float, ou None se inválido."""
    if not s:
        return None
    try:
        return float(s.strip().replace(".", "").replace(",", "."))
    except ValueError:
        return None


def fmt_valor_br(valor: float) -> str:
    """Formata float como valor BR com 2 casas: 99999.06 → '99.999,06'."""
    return f"{valor:,.2f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def _valor_na_coluna(
    linhas: list[str], idx_cabecalho: int, rotulo: str, ate: int
) -> str | None:
    """Extrai o número alinhado à coluna `rotulo` do cabeçalho da tabela.

    `pdftotext -layout` preserva a posição horizontal, então o valor da coluna
    "Valor Líquido" aparece nas linhas seguintes aproximadamente na mesma faixa
    de colunas do rótulo. Escolhe o número cujo centro está mais próximo do
    centro do rótulo.

    Retorna o valor já formatado com 2 casas (ex.: "99.999,06") ou None.
    """
    span = _span_rotulo(linhas[idx_cabecalho], rotulo)
    if span is None:
        return None

    centro_rotulo = (span[0] + span[1]) / 2
    # Tolerância generosa: a célula pode estar alinhada à esquerda ou à direita.
    tolerancia = max(span[1] - span[0], 14)

    melhor: tuple[float, float] | None = None  # (distância, valor)
    for i in range(idx_cabecalho + 1, min(ate, len(linhas))):
        for m in _RE_VALOR_BR.finditer(linhas[i]):
            centro_token = (m.start() + m.end()) / 2
            distancia = abs(centro_token - centro_rotulo)
            if distancia > tolerancia:
                continue
            valor = br_float(m.group())
            if valor is None:
                continue
            if melhor is None or distancia < melhor[0]:
                melhor = (distancia, valor)

    if melhor is None:
        return None
    return fmt_valor_br(melhor[1])


# ============================================================================
# Pasta 01: BTG Notas de Corretagem
# ============================================================================


@dataclass
class NotaBTG:
    data: str | None = None  # MM-DD
    ativo: str | None = None
    tipo_op: str | None = None  # Compra/Venda/Juros
    valor: str | None = None  # X.XXX,XX (sem o "R$")


# Linha do título: "DEB - ENEVB0", "CDCA - 24G02736845", "NTN-B1 - NTN-B1  BACEN-..."
_RE_TITULO = re.compile(
    r"^\s*([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*)\s+-\s+([A-Z0-9][A-Z0-9-]*)(?=\s{2,}|\s*$)"
)

_TIPOS_OPERACAO = ("Compra", "Venda", "Juros")
_RE_TIPO = re.compile(r"^\s*(Compra|Venda|Juros)\b", re.IGNORECASE)


def _monta_ativo(prefixo: str, codigo: str) -> str:
    """Decide entre `CODIGO` e `PREFIXO-CODIGO`.

    Regra derivada dos nomes já validados na pasta:
      DEB - ENEVB0        → ENEVB0            (código começa com letra)
      CDB - CDB326EQ2A4   → CDB326EQ2A4       (código começa com letra)
      NTN-B1 - NTN-B1     → NTN-B1            (código começa com letra)
      CDCA - 24G02736845  → CDCA-24G02736845  (código começa com dígito)
      CRI - 22L2288690    → CRI-22L2288690    (código começa com dígito)
    """
    return codigo if codigo[:1].isalpha() else f"{prefixo}-{codigo}"


def parse_pasta_01(texto: str) -> NotaBTG:
    """Extrai campos de Nota de Negociação BTG (Tesouro Direto / Títulos Privados / CDB)."""
    if not texto.strip():
        log.warning("Pasta 01: pdftotext devolveu texto vazio")
        return NotaBTG()

    linhas = _linhas(texto)
    nota = NotaBTG()

    # --- 1. Data da Operação: DD/MM/YYYY → MM-DD ---------------------------
    idx_data = _acha_linha(linhas, "data da operacao")
    if idx_data is not None:
        m = re.search(r"(\d{2})/(\d{2})/(\d{4})", linhas[idx_data])
        if m:
            nota.data = f"{m.group(2)}-{m.group(1)}"

    # --- Delimita os dois blocos da nota -----------------------------------
    # "Características dos Títulos" / "do Produto" / "dos Títulos Públicos"
    ini_titulos = _acha_linha(linhas, "caracteristicas do")
    ini_operacao = _acha_linha(linhas, "caracteristicas da opera")
    fim_operacao = None
    if ini_operacao is not None:
        fim_operacao = _acha_linha(
            linhas, "forma de liquida", "observacoes", "importante", inicio=ini_operacao + 1
        )
    if fim_operacao is None:
        fim_operacao = len(linhas)

    # --- 2. Ativo (dentro do bloco de títulos) -----------------------------
    fim_titulos = ini_operacao if ini_operacao is not None else len(linhas)
    if ini_titulos is not None:
        for i in range(ini_titulos, min(fim_titulos, len(linhas))):
            m = _RE_TITULO.match(linhas[i])
            if m:
                nota.ativo = _monta_ativo(m.group(1), m.group(2))
                break

    # --- 3. Tipo de operação (dentro do bloco da operação) -----------------
    if ini_operacao is not None:
        for i in range(ini_operacao + 1, min(fim_operacao, len(linhas))):
            m = _RE_TIPO.match(linhas[i])
            if m:
                nota.tipo_op = m.group(1).capitalize()
                break

    # --- 4. Valor Líquido (alinhamento de coluna) --------------------------
    if ini_operacao is not None:
        idx_cab = _acha_linha(
            linhas, "valor liquido", inicio=ini_operacao
        )
        if idx_cab is not None and idx_cab < fim_operacao:
            nota.valor = _valor_na_coluna(linhas, idx_cab, "Valor Liquido", fim_operacao)

    if nota.valor is None:
        log.debug("Pasta 01: valor não encontrado por coluna")

    log.info(
        "Pasta 01 → data=%s ativo=%s tipo=%s valor=%s",
        nota.data, nota.ativo, nota.tipo_op, nota.valor,
    )
    return nota


def nome_pasta_01(nota: NotaBTG) -> tuple[str | None, list[str]]:
    """Gera nome final da Pasta 01 e a lista de campos não reconhecidos."""
    faltando = [
        rotulo
        for rotulo, valor in (
            ("data", nota.data),
            ("ativo", nota.ativo),
            ("tipo", nota.tipo_op),
            ("valor", nota.valor),
        )
        if not valor
    ]
    # Nenhum campo reconhecido → não renomear (regra explícita).
    if len(faltando) == 4:
        return None, faltando

    data = nota.data or "??"
    ativo = nota.ativo or "??"
    tipo = nota.tipo_op or "??"
    valor = f"R${nota.valor}" if nota.valor else "R$??"
    return f"{data} - {ativo} - {tipo} - {valor}.pdf", faltando


# ============================================================================
# Pasta 02: Relatório de Performance
# ============================================================================


@dataclass
class RelatorioPerformance:
    data_geracao: str | None = None  # yy-mm-dd
    periodo_inicio: str | None = None  # yy-mm
    periodo_fim: str | None = None  # yy-mm


def parse_pasta_02(texto: str) -> RelatorioPerformance:
    """Extrai campos de Relatório de Performance (primeira página)."""
    if not texto.strip():
        return RelatorioPerformance()

    linhas = _linhas(texto)
    rel = RelatorioPerformance()

    for linha in linhas:
        compacta = _compacta(linha)
        # "Gerado em" sai como "Ge rado e m" em alguns relatórios.
        if rel.data_geracao is None and "geradoem" in compacta:
            m = re.search(r"(\d{2})/(\d{2})/(\d{4})", linha)
            if m:
                rel.data_geracao = f"{m.group(3)[-2:]}-{m.group(2)}-{m.group(1)}"
        if rel.periodo_inicio is None and "periodo" in compacta:
            datas = re.findall(r"(\d{2})/(\d{2})/(\d{4})", linha)
            if len(datas) >= 2:
                d1, d2 = datas[0], datas[-1]
                rel.periodo_inicio = f"{d1[2][-2:]}-{d1[1]}"
                rel.periodo_fim = f"{d2[2][-2:]}-{d2[1]}"

    log.info(
        "Pasta 02 → geracao=%s periodo=%s..%s",
        rel.data_geracao, rel.periodo_inicio, rel.periodo_fim,
    )
    return rel


def nome_pasta_02(rel: RelatorioPerformance) -> tuple[str | None, list[str]]:
    faltando = [
        rotulo
        for rotulo, valor in (
            ("data_geracao", rel.data_geracao),
            ("periodo_inicio", rel.periodo_inicio),
            ("periodo_fim", rel.periodo_fim),
        )
        if not valor
    ]
    if len(faltando) == 3:
        return None, faltando

    data = rel.data_geracao or "??"
    inicio = rel.periodo_inicio or "??"
    fim = rel.periodo_fim or "??"
    return f"{data} - Performance - {inicio} - {fim}.pdf", faltando


# ============================================================================
# Pasta 03: Extrato Investimentos
# ============================================================================


@dataclass
class ExtratoInvestimentos:
    ano_mes: str | None = None  # yyyy-mm


def parse_pasta_03(texto: str) -> ExtratoInvestimentos:
    """Extrai o período do Extrato de Investimentos (primeira página)."""
    if not texto.strip():
        return ExtratoInvestimentos()

    est = ExtratoInvestimentos()
    for linha in _linhas(texto):
        if "periodo" not in _chave(linha):
            continue
        # Aceita DD/MM/YYYY e DD/MM/YY — usa a última data (fim do período).
        datas = re.findall(r"(\d{2})/(\d{2})/(\d{2,4})", linha)
        if datas:
            _, mes, ano = datas[-1]
            ano_completo = ano if len(ano) == 4 else f"20{ano}"
            est.ano_mes = f"{ano_completo}-{mes}"
            break

    log.info("Pasta 03 → ano_mes=%s", est.ano_mes)
    return est


def nome_pasta_03(est: ExtratoInvestimentos) -> tuple[str | None, list[str]]:
    if not est.ano_mes:
        return None, ["ano_mes"]
    return f"{est.ano_mes}.pdf", []


# ============================================================================
# Pasta 04: BTG Extratos Banking (subpastas por banco)
# ============================================================================


_MESES_PT = {
    "janeiro": "01", "fevereiro": "02", "marco": "03", "abril": "04",
    "maio": "05", "junho": "06", "julho": "07", "agosto": "08",
    "setembro": "09", "outubro": "10", "novembro": "11", "dezembro": "12",
}


@dataclass
class ExtratoBank:
    ano_mes: str | None = None  # yyyy-mm (fatura de um mês)
    periodo_inicio: str | None = None  # yy-mm (extrato multi-mês)
    periodo_fim: str | None = None  # yy-mm


def parse_banking(texto: str) -> ExtratoBank:
    """Parser comum de extrato/fatura bancária.

    Cobre os formatos vistos em ambos os bancos:
      - "Vencimento: DD/MM/YYYY"          → ano-mês direto
      - "Vencimento: DD/MM" + "fatura de <mês> de <ano>"
      - "Período de DD/MM/YYYY a DD/MM/YYYY" → extrato multi-mês
    """
    if not texto.strip():
        return ExtratoBank()

    bank = ExtratoBank()
    linhas = _linhas(texto)

    venc_mes = venc_ano = None
    fatura_mes = fatura_ano = None

    for linha in linhas:
        norm = _chave(linha)

        if venc_mes is None and "vencimento" in norm:
            m = re.search(r"(\d{2})/(\d{2})/(\d{4})", linha)
            if m:
                venc_mes, venc_ano = m.group(2), m.group(3)
            else:
                m = re.search(r"(\d{2})/(\d{2})(?!/)", linha)
                if m:
                    venc_mes = m.group(2)

        if fatura_mes is None:
            m = re.search(r"fatura de (\w+) de (\d{4})", norm)
            if m:
                fatura_mes = _MESES_PT.get(m.group(1))
                fatura_ano = m.group(2)

        if bank.periodo_inicio is None and "periodo" in norm:
            datas = re.findall(r"(\d{2})/(\d{2})/(\d{2,4})", linha)
            if len(datas) >= 2:
                d1, d2 = datas[0], datas[-1]
                a1 = d1[2] if len(d1[2]) == 2 else d1[2][-2:]
                a2 = d2[2] if len(d2[2]) == 2 else d2[2][-2:]
                if (a1, d1[1]) != (a2, d2[1]):  # só é multi-mês se mudar de mês
                    bank.periodo_inicio = f"{a1}-{d1[1]}"
                    bank.periodo_fim = f"{a2}-{d2[1]}"

    if venc_mes and venc_ano:
        bank.ano_mes = f"{venc_ano}-{venc_mes}"
    elif venc_mes and fatura_ano:
        bank.ano_mes = f"{fatura_ano}-{venc_mes}"
    elif fatura_mes and fatura_ano:
        bank.ano_mes = f"{fatura_ano}-{fatura_mes}"

    log.info(
        "Pasta 04 → ano_mes=%s periodo=%s..%s",
        bank.ano_mes, bank.periodo_inicio, bank.periodo_fim,
    )
    return bank


def nome_pasta_04(bank: ExtratoBank, nome_banco: str) -> tuple[str | None, list[str]]:
    """Nome final da Pasta 04: fatura mensal ou extrato multi-mês."""
    if bank.ano_mes:
        return f"{bank.ano_mes} - {nome_banco}.pdf", []
    if bank.periodo_inicio and bank.periodo_fim:
        return f"{bank.periodo_inicio} - {bank.periodo_fim} - Banking.pdf", []
    return None, ["ano_mes"]


# ============================================================================
# Orquestração
# ============================================================================


@dataclass
class Resultado:
    """Resultado do parsing de um arquivo."""

    nome: str | None = None
    campos_faltando: list[str] = field(default_factory=list)
    usou_llm: bool = False

    @property
    def completo(self) -> bool:
        return self.nome is not None and not self.campos_faltando


# Nome do campo (usado pelo LLM) → atributo do dataclass de cada pasta.
_CAMPOS_POR_PASTA: dict[int, dict[str, str]] = {
    1: {"data": "data", "ativo": "ativo", "tipo": "tipo_op", "valor": "valor"},
    2: {
        "data_geracao": "data_geracao",
        "periodo_inicio": "periodo_inicio",
        "periodo_fim": "periodo_fim",
    },
    3: {"ano_mes": "ano_mes"},
    4: {"ano_mes": "ano_mes"},
}


def determinar_nome_novo(
    folder_num: int,
    nome_banco: str | None,
    nome_arquivo: str,
    pdf_bytes: bytes,
    completar=None,
) -> Resultado:
    """Determina o nome novo do arquivo conforme a pasta de origem.

    `nome` é None quando nenhum campo foi reconhecido (não renomear).

    `completar` é um callback opcional `(texto, campos_faltando) -> dict` usado
    como último recurso quando a extração determinística não fecha. Fica como
    parâmetro (e não import) para o parser continuar puro e testável offline.
    """
    if folder_num not in _CAMPOS_POR_PASTA:
        return Resultado(None, ["pasta_desconhecida"])

    # Pasta 01 precisa do documento inteiro; as demais só da primeira página.
    texto = extrair_texto_pdf(pdf_bytes, primeira_pagina_so=(folder_num != 1))

    def monta(dados) -> tuple[str | None, list[str]]:
        if folder_num == 1:
            return nome_pasta_01(dados)
        if folder_num == 2:
            return nome_pasta_02(dados)
        if folder_num == 3:
            return nome_pasta_03(dados)
        return nome_pasta_04(dados, nome_banco or "??")

    if folder_num == 1:
        dados = parse_pasta_01(texto)
    elif folder_num == 2:
        dados = parse_pasta_02(texto)
    elif folder_num == 3:
        dados = parse_pasta_03(texto)
    else:
        dados = parse_banking(texto)

    nome, faltando = monta(dados)
    usou_llm = False

    if faltando and completar is not None:
        preenchidos = completar(texto, faltando) or {}
        mapa = _CAMPOS_POR_PASTA[folder_num]
        for campo, valor in preenchidos.items():
            atributo = mapa.get(campo)
            if atributo and not getattr(dados, atributo, None):
                setattr(dados, atributo, valor)
                usou_llm = True
        if usou_llm:
            nome, faltando = monta(dados)

    return Resultado(nome, faltando, usou_llm)


# Padrões finais por pasta — aceitam "??" (campo não reconhecido) e o sufixo
# de colisão " (2)", " (3)"...
_SUFIXO = r"(?: \(\d+\))?"
_PADROES_FINAIS = {
    1: re.compile(
        r"^(?:\d{2}-\d{2}|\?\?) - .+ - (?:Compra|Venda|Juros|\?\?) - "
        r"R\$(?:\d{1,3}(?:\.\d{3})*,\d{2}|\?\?)" + _SUFIXO + r"\.pdf$"
    ),
    2: re.compile(
        r"^(?:\d{2}-\d{2}-\d{2}|\?\?) - Performance - (?:\d{2}-\d{2}|\?\?) - "
        r"(?:\d{2}-\d{2}|\?\?)" + _SUFIXO + r"\.pdf$"
    ),
    3: re.compile(r"^\d{4}-\d{2}" + _SUFIXO + r"\.pdf$"),
    4: re.compile(
        r"^(?:\d{4}-\d{2} - .+|\d{2}-\d{2} - \d{2}-\d{2} - Banking)" + _SUFIXO + r"\.pdf$"
    ),
}


def valida_padrão_final(folder_num: int, nome: str) -> bool:
    """Verifica se o nome já bate com o padrão final da pasta (idempotência)."""
    padrao = _PADROES_FINAIS.get(folder_num)
    return bool(padrao and padrao.match(nome))
