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

# Marca usada quando o Drive não devolveu o md5 do arquivo. Existe para que um
# md5 vazio nunca seja confundido com "ainda não notificado".
SEM_MD5 = "sem-md5"


def chave_gastos(md5: str) -> str:
    """Chave de idempotência do aviso ao worker de gastos (file_id + md5)."""
    return md5 or SEM_MD5


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
    # md5 do conteúdo no Drive na última vez que o arquivo foi visto.
    md5: str = ""
    # md5 do conteúdo quando o worker de gastos foi avisado. Se o arquivo mudar,
    # o md5 muda e o worker é avisado de novo — nunca duas vezes pelo mesmo.
    gastos_notificado_md5: str = ""


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
        md5: str = "",
    ) -> None:
        # O aviso ao worker de gastos sobrevive a um reprocessamento: só perde
        # a validade quando o conteúdo do arquivo (md5) muda.
        anterior = self.registros.get(file_id)
        self.registros[file_id] = RegistroArquivo(
            file_id=file_id,
            file_name=file_name,
            folder_num=folder_num,
            status=status,
            data_processamento=datetime.now(timezone.utc).isoformat(),
            motivo=motivo,
            nome_novo=nome_novo,
            dry_run=dry_run,
            md5=md5 or (anterior.md5 if anterior else ""),
            gastos_notificado_md5=anterior.gastos_notificado_md5 if anterior else "",
        )
        self._salvar()

    def ja_notificou_gastos(self, file_id: str, md5: str) -> bool:
        """Diz se o worker de gastos já foi avisado desse arquivo nesse md5."""
        registro = self.registros.get(file_id)
        return registro is not None and registro.gastos_notificado_md5 == chave_gastos(md5)

    def marcar_gastos_notificado(self, file_id: str, md5: str) -> None:
        """Grava que o worker de gastos foi avisado (sobrevive a restart)."""
        registro = self.registros.get(file_id)
        if registro is None:
            log.warning("Sem registro para %s — aviso ao worker não foi marcado", file_id)
            return
        registro.gastos_notificado_md5 = chave_gastos(md5)
        if md5 and not registro.md5:
            registro.md5 = md5
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
