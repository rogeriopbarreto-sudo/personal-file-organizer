"""Fallback via Anthropic API para campos que o parser não conseguiu extrair."""
from __future__ import annotations

import functools
import json
import logging

import anthropic

from .config import settings

log = logging.getLogger("file_organizer.llm_fallback")


@functools.lru_cache(maxsize=1)
def get_anthropic() -> anthropic.Anthropic:
    """Singleton de cliente Anthropic."""
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def extrair_data_com_llm(texto_pdf: str, pasta_num: int) -> str | None:
    """Tenta extrair data de vencimento/geração com LLM (fallback).

    Retorna data em formato apropriado (MM-DD, yy-mm-dd, yyyy-mm, etc.) ou None.
    """
    if not texto_pdf or not settings.anthropic_api_key:
        return None

    try:
        client = get_anthropic()

        prompt = f"""Extraia a data de vencimento da fatura ou data de geração do documento.

Retorne APENAS um JSON com o campo "data", ou "data": null se não encontrar.
Formato esperado:
- Para datas completas (DD/MM/YYYY): retorne "YYYY-MM-DD"
- Para datas sem ano (DD/MM): retorne "MM-DD" ou "YYYY-MM" se conseguir inferir o ano
- Para períodos (DD/MM/YYYY a DD/MM/YYYY): retorne apenas a data final
Não invente, se não encontrar com certeza, retorne null.

Texto do PDF:
{texto_pdf[:2000]}
"""

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )

        try:
            result = json.loads(response.content[0].text)
            return result.get("data")
        except (json.JSONDecodeError, IndexError, KeyError):
            log.warning("LLM retornou JSON inválido para data")
            return None

    except Exception as e:
        log.error("Erro ao chamar Anthropic para data: %s", e)
        return None


def extrair_campo_com_llm(texto_pdf: str, campo: str) -> str | None:
    """Tenta extrair um campo específico (ex: "ativo", "tipo_operacao", "valor").

    Retorna valor ou None.
    """
    if not texto_pdf or not settings.anthropic_api_key:
        return None

    try:
        client = get_anthropic()

        prompt = f"""Extraia o campo "{campo}" do documento.

Retorne APENAS um JSON com o campo "{campo}", ou "{campo}": null se não encontrar.
Exemplos:
- campo "ativo": retorne "NTN-B1" ou "PEJA11" (sem aspas extras)
- campo "tipo_operacao": retorne "Compra" ou "Venda" ou "Juros"
- campo "valor": retorne "R$99.999,06" ou "99.999,06"

Não invente, extraia do texto ou retorne null.

Texto do PDF:
{texto_pdf[:2000]}
"""

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )

        try:
            result = json.loads(response.content[0].text)
            valor = result.get(campo)
            return valor if valor and isinstance(valor, str) else None
        except (json.JSONDecodeError, IndexError, KeyError):
            log.warning("LLM retornou JSON inválido para campo %s", campo)
            return None

    except Exception as e:
        log.error("Erro ao chamar Anthropic para campo %s: %s", campo, e)
        return None
