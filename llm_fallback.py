"""Fallback via Anthropic API para os campos que o parser determinístico não extraiu.

Último recurso: só roda quando o regex/coluna falha. Tudo que volta do modelo é
validado por formato antes de virar nome de arquivo — o LLM não pode inventar.
"""
from __future__ import annotations

import functools
import json
import logging
import re

from .config import settings

log = logging.getLogger("file_organizer.llm_fallback")

# Como cada campo precisa voltar, e como validá-lo.
_INSTRUCOES = {
    "data": 'data da operação no formato "MM-DD" (mês-dia, sem ano)',
    "ativo": 'código do ativo/título, ex.: "NTN-B1", "PEJA11", "CDCA-24G02736845"',
    "tipo": '"Compra", "Venda" ou "Juros"',
    "valor": 'valor líquido no formato brasileiro, ex.: "99.999,06"',
    "data_geracao": 'data de geração no formato "AA-MM-DD"',
    "periodo_inicio": 'início do período no formato "AA-MM"',
    "periodo_fim": 'fim do período no formato "AA-MM"',
    "ano_mes": 'mês de referência no formato "AAAA-MM"',
}

_VALIDADORES = {
    "data": re.compile(r"^\d{2}-\d{2}$"),
    "ativo": re.compile(r"^[A-Z0-9][A-Z0-9-]{1,30}$"),
    "tipo": re.compile(r"^(Compra|Venda|Juros)$"),
    "valor": re.compile(r"^\d{1,3}(?:\.\d{3})*,\d{2}$"),
    "data_geracao": re.compile(r"^\d{2}-\d{2}-\d{2}$"),
    "periodo_inicio": re.compile(r"^\d{2}-\d{2}$"),
    "periodo_fim": re.compile(r"^\d{2}-\d{2}$"),
    "ano_mes": re.compile(r"^\d{4}-\d{2}$"),
}


@functools.lru_cache(maxsize=1)
def _cliente():
    import anthropic

    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def _extrai_json(texto: str) -> dict:
    """Lê o JSON da resposta, tolerando cercas de markdown."""
    texto = texto.strip()
    if texto.startswith("```"):
        texto = re.sub(r"^```[a-zA-Z]*\n?|```$", "", texto).strip()
    inicio, fim = texto.find("{"), texto.rfind("}")
    if inicio < 0 or fim <= inicio:
        return {}
    try:
        dados = json.loads(texto[inicio : fim + 1])
        return dados if isinstance(dados, dict) else {}
    except json.JSONDecodeError:
        return {}


def completar_campos(texto_pdf: str, faltando: list[str]) -> dict[str, str]:
    """Tenta preencher os campos que faltaram, validando o formato de cada um.

    Devolve só os campos que voltaram com formato válido. Falha silenciosa:
    qualquer erro resulta em dicionário vazio.
    """
    if not settings.usar_llm_fallback or not settings.anthropic_api_key:
        return {}
    if not texto_pdf.strip() or not faltando:
        return {}

    pedidos = [c for c in faltando if c in _INSTRUCOES]
    if not pedidos:
        return {}

    lista = "\n".join(f'- "{campo}": {_INSTRUCOES[campo]}' for campo in pedidos)
    prompt = (
        "Extraia os campos abaixo do documento financeiro. Responda APENAS com um "
        "objeto JSON, sem texto em volta.\n\n"
        f"Campos:\n{lista}\n\n"
        "Use null em qualquer campo que você não encontrar explicitamente no "
        "documento. Nunca invente ou estime valores.\n\n"
        f"Documento:\n{texto_pdf[:6000]}"
    )

    try:
        resposta = _cliente().messages.create(
            model=settings.anthropic_model,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        bruto = "".join(
            bloco.text for bloco in resposta.content if getattr(bloco, "type", "") == "text"
        )
    except Exception as e:
        log.error("Falha ao chamar a Anthropic: %s", e)
        return {}

    dados = _extrai_json(bruto)
    resultado: dict[str, str] = {}
    for campo in pedidos:
        valor = dados.get(campo)
        if not isinstance(valor, str):
            continue
        valor = valor.strip()
        validador = _VALIDADORES.get(campo)
        if validador and validador.match(valor):
            resultado[campo] = valor
        elif valor:
            log.warning("LLM devolveu '%s' inválido para o campo %s", valor, campo)

    if resultado:
        log.info("LLM completou: %s", ", ".join(resultado))
    return resultado
