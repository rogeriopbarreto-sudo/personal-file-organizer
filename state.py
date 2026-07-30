"""Cache persistente de arquivos já processados, notificados ou ignorados.

Evita re-notificar o mesmo arquivo toda vez que o loop roda.
Usa JSON em volume persistente (Coolify).
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("file_organizer.state")

# Diretório de estado (será montado como volume no Coolify)
STATE_DIR = Path(os.environ.get("STATE_DIR", "/app/state"))
STATE_FILE = STATE_DIR / "state.json"


@dataclass
class ArchivoState:
    """Registro de um arquivo já visto."""

    file_id: str
    file_name: str
    folder_num: int  # 1, 2, 3 ou 4
    status: str  # "success", "notificado", "ignorado", "protegido", "duplicata"
    data_processamento: str  # ISO 8601
    motivo: str = ""  # Se status != "success", motivo da notificação


class StateManager:
    """Gerencia cache persistente."""

    def __init__(self):
        self.files: dict[str, ArchivoState] = {}
        self._load()

    def _ensure_dir(self) -> None:
        """Cria diretório de estado se não existir."""
        STATE_DIR.mkdir(parents=True, exist_ok=True)

    def _load(self) -> None:
        """Carrega estado do arquivo JSON."""
        self._ensure_dir()
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, "r") as f:
                    data = json.load(f)
                    self.files = {
                        file_id: ArchivoState(**item)
                        for file_id, item in data.get("files", {}).items()
                    }
                log.info("Estado carregado: %d arquivos em cache", len(self.files))
            except Exception as e:
                log.error("Erro ao carregar state.json: %s", e)
                self.files = {}
        else:
            log.info("Primeiro run: state.json não existe ainda")
            self.files = {}

    def _save(self) -> None:
        """Salva estado no arquivo JSON."""
        self._ensure_dir()
        try:
            data = {
                "files": {
                    file_id: asdict(state)
                    for file_id, state in self.files.items()
                }
            }
            with open(STATE_FILE, "w") as f:
                json.dump(data, f, indent=2)
            log.debug("State salvo")
        except Exception as e:
            log.error("Erro ao salvar state.json: %s", e)

    def registrar_sucesso(self, file_id: str, file_name: str, folder_num: int) -> None:
        """Marca arquivo como processado com sucesso (renomeado)."""
        self.files[file_id] = ArchivoState(
            file_id=file_id,
            file_name=file_name,
            folder_num=folder_num,
            status="success",
            data_processamento=datetime.now(timezone.utc).isoformat(),
            motivo="",
        )
        self._save()

    def registrar_notificado(
        self,
        file_id: str,
        file_name: str,
        folder_num: int,
        motivo: str,
    ) -> None:
        """Marca arquivo como notificado (não processado, mas já avisado)."""
        self.files[file_id] = ArchivoState(
            file_id=file_id,
            file_name=file_name,
            folder_num=folder_num,
            status="notificado",
            data_processamento=datetime.now(timezone.utc).isoformat(),
            motivo=motivo,
        )
        self._save()

    def registrar_protegido(self, file_id: str, file_name: str, folder_num: int) -> None:
        """Marca arquivo como protegido por senha."""
        self.files[file_id] = ArchivoState(
            file_id=file_id,
            file_name=file_name,
            folder_num=folder_num,
            status="protegido",
            data_processamento=datetime.now(timezone.utc).isoformat(),
            motivo="PDF protegido por senha",
        )
        self._save()

    def registrar_duplicata(
        self,
        file_id: str,
        file_name: str,
        folder_num: int,
        nome_existente: str,
    ) -> None:
        """Marca arquivo como duplicata suspeita."""
        self.files[file_id] = ArchivoState(
            file_id=file_id,
            file_name=file_name,
            folder_num=folder_num,
            status="duplicata",
            data_processamento=datetime.now(timezone.utc).isoformat(),
            motivo=f"Possível duplicata de {nome_existente}",
        )
        self._save()

    def ja_visto(self, file_id: str) -> bool:
        """Retorna True se arquivo já foi processado (em qualquer status)."""
        return file_id in self.files

    def ja_notificado(self, file_id: str) -> bool:
        """Retorna True se arquivo já foi notificado (não vai notificar de novo)."""
        if file_id not in self.files:
            return False
        return self.files[file_id].status == "notificado"

    def get(self, file_id: str) -> ArchivoState | None:
        """Retorna registro do arquivo, ou None se não existe."""
        return self.files.get(file_id)


# Singleton global
state_manager: StateManager | None = None


def get_state_manager() -> StateManager:
    """Retorna a instância global do StateManager."""
    global state_manager
    if state_manager is None:
        state_manager = StateManager()
    return state_manager
