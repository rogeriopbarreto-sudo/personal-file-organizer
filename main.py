"""Loop principal: polling das 4 pastas, parsing, rename, notificação."""
from __future__ import annotations

import logging
import logging.config
import time
import traceback
from pathlib import Path

from .config import settings
from .drive_client import download_pdf, list_files_in_folder, rename_file
from .notifier import (
    notificar_arquivo_sem_dados,
    notificar_banco_novo,
    notificar_duplicata_suspeita,
    notificar_erro_autenticacao,
    notificar_pdf_protegido,
    notificar_sucesso_ciclo,
)
from .parser import (
    determinar_nome_novo,
    parse_pasta_04_banking_btg,
    parse_pasta_04_banking_itau,
    valida_padrão_final,
)
from .state import get_state_manager

# ============================================================================
# Logging
# ============================================================================

LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
)
log = logging.getLogger("file_organizer.main")


# ============================================================================
# Configuração de pastas
# ============================================================================


PASTAS_MONITORADAS = {
    1: {"id": settings.drive_folder_01, "nome": "01_BTG Notas de Corretagem"},
    2: {"id": settings.drive_folder_02, "nome": "02_Relatorio de Performance"},
    3: {"id": settings.drive_folder_03, "nome": "03_Extrato Investimentos"},
    4: {"id": settings.drive_folder_04, "nome": "04_BTG Extratos Banking"},
}

BANCOS_CONHECIDOS = {
    "BTG": parse_pasta_04_banking_btg,
    "Itau": parse_pasta_04_banking_itau,
}


# ============================================================================
# Processamento de um arquivo
# ============================================================================


def processar_arquivo(file_id: str, file_name: str, folder_num: int) -> bool:
    """Processa um arquivo: baixa, parseia, renomeia (ou notifica).

    Retorna True se processado com sucesso (renomeado ou já válido), False se erro/ambíguo.
    """
    state_mgr = get_state_manager()

    # Se já visto, pular
    if state_mgr.ja_visto(file_id):
        log.debug("Arquivo %s já visto, pulando", file_name)
        return True

    # Checar se nome já está no padrão final (idempotência)
    if valida_padrão_final(folder_num, file_name):
        log.info("Arquivo %s já está no padrão final, pulando", file_name)
        state_mgr.registrar_sucesso(file_id, file_name, folder_num)
        return True

    log.info("Processando %s (pasta %d)", file_name, folder_num)

    try:
        # Baixar PDF
        pdf_bytes = download_pdf(file_id)
        if not pdf_bytes:
            log.warning("Falha ao baixar %s", file_name)
            return False

        # Determinar nome novo
        if folder_num == 4:
            # Pasta 04: precisa detectar subpasta/banco
            nome_banco = detectar_banco_pasta_04(file_name)
            if not nome_banco:
                log.warning("Não conseguiu detectar banco para %s", file_name)
                notificar_banco_novo("Desconhecido", file_name)
                state_mgr.registrar_notificado(
                    file_id, file_name, folder_num, "Banco desconhecido"
                )
                return False
        else:
            nome_banco = None

        nome_novo = determinar_nome_novo(folder_num, nome_banco, file_name, pdf_bytes)

        if not nome_novo:
            # Parser não conseguiu extrair nenhum campo
            log.warning("Arquivo %s sem dados reconhecidos", file_name)
            notificar_arquivo_sem_dados(file_name, folder_num)
            state_mgr.registrar_notificado(file_id, file_name, folder_num, "Sem dados")
            return False

        # Checar duplicata (Pasta 03)
        if folder_num == 3:
            resultado = verificar_duplicata_pasta_03(file_id, file_name, nome_novo)
            if resultado == "duplicata":
                return False
            elif resultado == "mesmo_arquivo":
                state_mgr.registrar_sucesso(file_id, file_name, folder_num)
                return True

        # Renomear (se DRY_RUN, só logar)
        if settings.dry_run:
            log.info("[DRY_RUN] Teria renomeado: %s → %s", file_name, nome_novo)
        else:
            rename_file(file_id, nome_novo)
            log.info("Renomeado com sucesso: %s → %s", file_name, nome_novo)

        state_mgr.registrar_sucesso(file_id, file_name, folder_num)
        return True

    except Exception as e:
        log.error("Erro ao processar %s: %s", file_name, traceback.format_exc())
        return False


def detectar_banco_pasta_04(file_name: str) -> str | None:
    """Detecta banco pela subpasta ou nome do arquivo.

    Heurística simples: procura "BTG" ou "Itau" no nome.
    TODO: melhorar pra detectar pela subpasta real no Drive.
    """
    # Por enquanto, heurística no nome
    if "itau" in file_name.lower():
        return "Itau"
    elif "btg" in file_name.lower() or "banco" in file_name.lower():
        return "BTG"
    else:
        return None  # Banco desconhecido


def verificar_duplicata_pasta_03(file_id: str, file_name: str, nome_novo: str) -> str | None:
    """Verifica se arquivo é uma possível duplicata (Pasta 03 apenas).

    Retorna:
    - "ok": prosseguir com rename
    - "duplicata": suspeitade duplicata, não renomear
    - "mesmo_arquivo": arquivo idêntico ao existente
    """
    # TODO: implementar lógica de comparar tamanho/hash com arquivo existente
    # Por enquanto, apenas retornar "ok" (será melhorado depois)
    return "ok"


# ============================================================================
# Ciclo principal
# ============================================================================


def rodar_ciclo() -> None:
    """Executa um ciclo de polling: lista arquivos das 4 pastas, processa cada um."""
    state_mgr = get_state_manager()
    total_processados = 0
    total_renomeados = 0

    log.info("=== INICIANDO CICLO ===")

    # Validar configuração
    faltando = settings.validar()
    if faltando:
        log.error("Configuração incompleta: faltam %s", ", ".join(faltando))
        notificar_erro_autenticacao()
        return

    for folder_num, folder_info in PASTAS_MONITORADAS.items():
        folder_id = folder_info["id"]
        folder_nome = folder_info["nome"]

        if not folder_id:
            log.warning("Pasta %d não configurada (ID vazio)", folder_num)
            continue

        log.info("Listando arquivos da pasta %d (%s)", folder_num, folder_nome)

        try:
            arquivos = list_files_in_folder(folder_id)
            log.info("  Encontrados %d PDFs", len(arquivos))

            for arquivo in arquivos:
                total_processados += 1

                if processar_arquivo(arquivo.id, arquivo.name, folder_num):
                    total_renomeados += 1

        except Exception as e:
            log.error("Erro ao listar pasta %d: %s", folder_num, traceback.format_exc())
            notificar_erro_autenticacao()

    log.info("=== CICLO CONCLUÍDO ===")
    log.info("Total: %d processados, %d renomeados", total_processados, total_renomeados)

    # Notificar sucesso se houver algo para notificar
    if total_renomeados > 0:
        notificar_sucesso_ciclo(total_processados, total_renomeados)


def loop_principal() -> None:
    """Loop infinito: polling a cada poll_interval_seconds."""
    log.info("Iniciando loop principal")
    log.info("Intervalo de polling: %d segundos", settings.poll_interval_seconds)
    log.info("DRY_RUN: %s", settings.dry_run)

    ciclo_count = 0
    while True:
        try:
            ciclo_count += 1
            log.info("--- Ciclo #%d ---", ciclo_count)
            rodar_ciclo()

        except KeyboardInterrupt:
            log.info("Interrompido pelo usuário")
            break

        except Exception as e:
            log.error("Erro não tratado: %s", traceback.format_exc())

        log.info("Dormindo %d segundos...", settings.poll_interval_seconds)
        time.sleep(settings.poll_interval_seconds)


if __name__ == "__main__":
    log.info("Starting Personal File Organizer")
    loop_principal()
