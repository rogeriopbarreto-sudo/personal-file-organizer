"""Cache persistente dos arquivos já processados, notificados ou ignorados.

Evita reprocessar e re-notificar o mesmo arquivo a cada webhook.
Guardado em JSON num volume persistente (Coolify).
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("file_organizer.state")

STATE_DIR = Path(os.environ.get("STATE_DIR", "/app/state"))
STATE_FILE = STATE_DIR / "state.json"

# Status possíveis
SUCESSO = "sucesso"
INCOMPLETO = "incompleto"  # renomeado, mas com campos "??"
SEM_DADOS = "sem_dados"
PROTEGIDO = "protegido"
ERRO = "erro"


@dataclass
class RegistroArquivo:
    """Registro de um arquivo já visto."""

    file_id: str
    file_name: str
    folder_num: int
    status: str
    data_processamento: str
    motivo: str = ""
    nome_novo: str = ""
    # Processado apenas em simulação: quando o DRY_RUN for desligado, o arquivo
    # precisa ser processado de verdade.
    dry_run: bool = False


class StateManager:
    """Cache persistente em JSON."""

    def __init__(self) -> None:
        self.registros: dict[str, RegistroArquivo] = {}
        self._carregar()

    # -- persistência ----------------------------------------------------

    def _carregar(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if not STATE_FILE.exists():
            log.info("Primeiro run: state.json ainda não existe")
            return
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                dados = json.load(f)
            conhecidos = RegistroArquivo.__dataclass_fields__.keys()
            self.registros = {
                file_id: RegistroArquivo(
                    **{k: v for k, v in item.items() if k in conhecidos}
                )
                for file_id, item in dados.get("files", {}).items()
            }
            log.info("Estado carregado: %d arquivos em cache", len(self.registros))
        except Exception:
            log.exception("Erro ao carregar state.json — começando vazio")
            self.registros = {}

    def _salvar(self) -> None:
        """Grava de forma atômica (tmp + replace) para não corromper em queda."""
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        temporario = STATE_FILE.with_suffix(".json.tmp")
        try:
            with open(temporario, "w", encoding="utf-8") as f:
                json.dump(
                    {"files": {k: asdict(v) for k, v in self.registros.items()}},
                    f,
                    indent=2,
                    ensure_ascii=False,
                )
            os.replace(temporario, STATE_FILE)
        except Exception:
            log.exception("Erro ao salvar state.json")

    # -- API -------------------------------------------------------------

    def registrar(
        self,
        file_id: str,
        file_name: str,
        folder_num: int,
        status: str,
        motivo: str = "",
        nome_novo: str = "",
        dry_run: bool = False,
    ) -> None:
        self.registros[file_id] = RegistroArquivo(
            file_id=file_id,
            file_name=file_name,
            folder_num=folder_num,
            status=status,
            data_processamento=datetime.now(timezone.utc).isoformat(),
            motivo=motivo,
            nome_novo=nome_novo,
            dry_run=dry_run,
        )
        self._salvar()

    def precisa_processar(self, file_id: str, dry_run_atual: bool) -> bool:
        """Diz se o arquivo ainda precisa ser processado.

        Um registro feito em modo simulação não conta como processado quando o
        serviço passa a rodar de verdade — senão nada seria renomeado ao sair
        do DRY_RUN.
        """
        registro = self.registros.get(file_id)
        if registro is None:
            return True
        if registro.dry_run and not dry_run_atual:
            return True
        return False

    def get(self, file_id: str) -> RegistroArquivo | None:
        return self.registros.get(file_id)


_gerenciador: StateManager | None = None


def get_state_manager() -> StateManager:
    """Instância global do StateManager."""
    global _gerenciador
    if _gerenciador is None:
        _gerenciador = StateManager()
    return _gerenciador
