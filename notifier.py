"""Notificações via Telegram (stdlib, sem dependências extras)."""
from __future__ import annotations

import html
import logging
import time
import urllib.parse
import urllib.request

from .config import settings

log = logging.getLogger("file_organizer.notifier")

# Anti-flood: não repete a mesma mensagem dentro dessa janela.
JANELA_ANTI_REPETICAO_S = 60
_ultimas: dict[str, float] = {}


def _esc(texto: str) -> str:
    """Escapa para parse_mode=HTML (nomes de arquivo podem ter & < >)."""
    return html.escape(texto, quote=False)


def tg_send(mensagem: str) -> bool:
    """Envia mensagem via Telegram. Nunca lança exceção."""
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        log.warning("Credenciais do Telegram ausentes — mensagem não enviada")
        return False

    agora = time.monotonic()
    ultimo = _ultimas.get(mensagem)
    if ultimo is not None and agora - ultimo < JANELA_ANTI_REPETICAO_S:
        log.debug("Mensagem idêntica suprimida (anti-flood)")
        return True
    _ultimas[mensagem] = agora
    # Não deixa o dicionário crescer sem limite.
    if len(_ultimas) > 500:
        for chave, quando in list(_ultimas.items()):
            if agora - quando > JANELA_ANTI_REPETICAO_S:
                _ultimas.pop(chave, None)

    if len(mensagem) > 4000:
        mensagem = mensagem[:3996] + "..."

    dados = urllib.parse.urlencode(
        {
            "chat_id": settings.telegram_chat_id,
            "text": mensagem,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode()

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    try:
        requisicao = urllib.request.Request(url, data=dados, method="POST")
        with urllib.request.urlopen(requisicao, timeout=15) as resposta:
            if resposta.status == 200:
                log.info("Mensagem Telegram enviada")
                return True
            log.warning("Telegram retornou status %d", resposta.status)
            return False
    except Exception as e:
        log.error("Erro ao enviar Telegram: %s", e)
        return False


# ============================================================================
# Mensagens por evento
# ============================================================================


def notificar_renomeado(nome_antigo: str, nome_novo: str, pasta: int, simulacao: bool) -> None:
    """Rename concluído (ou simulado, em DRY_RUN)."""
    prefixo = "🧪 <b>[simulação]</b>" if simulacao else "✅"
    tg_send(
        f"{prefixo} Pasta {pasta}\n"
        f"<code>{_esc(nome_antigo)}</code>\n"
        f"→ <code>{_esc(nome_novo)}</code>"
    )


def notificar_campos_faltando(
    nome_antigo: str, nome_novo: str, pasta: int, faltando: list[str], simulacao: bool
) -> None:
    """Renomeado, mas com campos não reconhecidos (viraram '??')."""
    prefixo = "🧪 <b>[simulação]</b>" if simulacao else "⚠️"
    tg_send(
        f"{prefixo} Pasta {pasta} — campos não reconhecidos: "
        f"<b>{_esc(', '.join(faltando))}</b>\n"
        f"<code>{_esc(nome_antigo)}</code>\n"
        f"→ <code>{_esc(nome_novo)}</code>\n\n"
        f"Confira no Drive e ajuste o nome se precisar."
    )


def notificar_arquivo_sem_dados(nome_arquivo: str, pasta: int) -> None:
    """Nenhum campo reconhecido — arquivo mantido como está."""
    tg_send(
        f"❌ Pasta {pasta} — nenhum dado reconhecido, arquivo <b>não</b> renomeado:\n"
        f"<code>{_esc(nome_arquivo)}</code>"
    )


def notificar_pdf_protegido(nome_arquivo: str, pasta: int) -> None:
    """PDF exige senha."""
    tg_send(
        f"🔒 Pasta {pasta} — PDF protegido por senha, não deu para ler:\n"
        f"<code>{_esc(nome_arquivo)}</code>\n\n"
        f"Remova a senha e suba de novo."
    )


def notificar_pdf_protegido_pasta_04(
    nome_arquivo: str, enviado: bool, hook_desligado: str = ""
) -> None:
    """PDF com senha numa subpasta de banco: fica com o nome original.

    Quem abre é o worker de gastos, que tem a senha de cada banco no servidor —
    não há o que o Roger fazer, a não ser que o envio tenha falhado.
    `hook_desligado` diz por que o aviso nem foi tentado (sem secret, DRY_RUN).
    """
    if hook_desligado:
        desfecho = (
            f"O aviso ao dashboard de gastos está desligado ({hook_desligado}); "
            f"o arquivo não foi enviado."
        )
    elif enviado:
        desfecho = (
            "Enviado ao dashboard de gastos, que abre com a senha configurada "
            "no servidor."
        )
    else:
        desfecho = (
            "Não chegou ao dashboard de gastos; o organizador tenta de novo no "
            "próximo deploy ou com POST /varrer."
        )
    tg_send(
        f"🔒 Pasta 4 — PDF protegido por senha, ficou com o nome original:\n"
        f"<code>{_esc(nome_arquivo)}</code>\n\n"
        f"{desfecho}"
    )


def notificar_banco_desconhecido(nome_arquivo: str) -> None:
    """Arquivo na raiz da Pasta 04, fora de uma subpasta de banco."""
    tg_send(
        f"🏦 Pasta 4 — não consegui identificar o banco de:\n"
        f"<code>{_esc(nome_arquivo)}</code>\n\n"
        f"Mova o arquivo para a subpasta do banco (ex.: <b>BTG</b> ou <b>Itau</b>)."
    )


def notificar_gastos_falhou(nome_arquivo: str, detalhe: str) -> None:
    """O worker do dashboard de gastos não respondeu — o organizador seguiu."""
    tg_send(
        f"📉 Pasta 4 — <b>worker de gastos não respondeu</b> para:\n"
        f"<code>{_esc(nome_arquivo)}</code>\n"
        f"<code>{_esc(detalhe[:200])}</code>\n\n"
        f"O organizador seguiu normalmente; só o dashboard pode estar atrasado. "
        f"Tenta de novo no próximo deploy ou com POST /varrer."
    )


def notificar_gastos_ocupado(nome_arquivo: str, tentativas: int, segundos: float) -> None:
    """O worker ficou ocupado (409) além do teto de tentativas ou de tempo."""
    duracao = f"{segundos / 60:.0f} min" if segundos >= 60 else f"{segundos:.0f} s"
    tg_send(
        f"📉 Pasta 4 — <b>worker de gastos ocupado</b> "
        f"({tentativas} tentativas em {duracao}); desisti de avisar sobre:\n"
        f"<code>{_esc(nome_arquivo)}</code>\n\n"
        f"O arquivo fica pendente até o próximo deploy ou um POST /varrer."
    )


def notificar_pasta_04_sem_subpastas(detalhe: str) -> None:
    """A Pasta 04 não devolveu subpasta de banco — nenhum extrato chega ao dashboard."""
    tg_send(
        f"🚨 Pasta 4 — <b>nenhuma subpasta de banco visível</b>:\n"
        f"<code>{_esc(detalhe[:300])}</code>\n\n"
        f"Sem elas nenhum extrato chega ao dashboard de gastos. Confira se a "
        f"Pasta 04 continua compartilhada como Editor com a service account."
    )


def notificar_erro(contexto: str, detalhe: str) -> None:
    """Erro inesperado durante o processamento."""
    tg_send(f"🚨 Erro em <b>{_esc(contexto)}</b>:\n<code>{_esc(detalhe[:500])}</code>")


def notificar_erro_configuracao(problemas: list[str]) -> None:
    """Configuração incompleta no startup."""
    tg_send(
        "🚨 Organizador subiu com configuração incompleta:\n"
        f"<code>{_esc(', '.join(problemas))}</code>"
    )
