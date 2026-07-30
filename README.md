# Personal File Organizer

Automação de renomeação de PDFs no Google Drive — notas BTG, extratos, faturas de banco. Roda 24/7 numa VPS (Coolify) com polling a cada 5 minutos.

## Setup Local (Teste)

### Pré-requisitos

- Python 3.12+
- `poppler-utils` (para `pdftotext`)
- Variáveis de ambiente (copiar de `.env.example` para `.env`)

### Instalação

```bash
pip install -r requirements.txt
```

### Variáveis de Ambiente

Copiar `.env.example` para `.env` e preencher:

```bash
cp .env.example .env
```

**Variáveis obrigatórias:**
- `GOOGLE_SERVICE_ACCOUNT_JSON` — credencial JSON do service account (completa)
- `DRIVE_FOLDER_01`, `DRIVE_FOLDER_02`, `DRIVE_FOLDER_03`, `DRIVE_FOLDER_04` — IDs das pastas do Drive
- `ANTHROPIC_API_KEY` — chave da Anthropic API
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — credenciais do bot Telegram

### Executar (teste local)

```bash
export DRY_RUN=true  # Testar sem fazer rename real
python -m app       # Roda o loop infinito (Ctrl+C para parar)
```

Com `DRY_RUN=true`, o script loga o que _teria_ feito sem modificar nenhum arquivo.

## Deploy no Coolify

1. Criar um repositório GitHub novo (`personal-file-organizer`).
2. Subir este código pro repo.
3. No Coolify:
   - Resource → Repository → selecionar `personal-file-organizer`
   - Build Pack: **Dockerfile**
   - Ports Exposes: sem porta exposta (worker puro)
   - Variáveis de ambiente: adicionar uma a uma (bulk paste pode falhar com JSON longo)
   - Volume persistente: criar volume em `/app/state` (cache de arquivos já processados)
   - Deploy
4. Rodar com `DRY_RUN=true` por alguns dias, monitorar logs e alertas Telegram.
5. Quando confiante, trocar para `DRY_RUN=false` e deixar rodar.

## Regras de Parsing

Veja `instrucoes-claude-code-organizador-drive.md` (no workspace raiz) para as regras exatas de renomeação por pasta. O resumo:

- **Pasta 01 (BTG Notas):** `MM-DD - Ativo - TipoOp - R$Valor.pdf`
- **Pasta 02 (Performance):** `yy-mm-dd - Performance - yy-mm - yy-mm.pdf`
- **Pasta 03 (Extratos):** `yyyy-mm.pdf`
- **Pasta 04 (Banking):** `yyyy-mm - NomeDoBanco.pdf` ou `yy-mm - yy-mm - Banking.pdf`

## Estrutura do Código

- **config.py** — Settings via env vars
- **drive_client.py** — autenticação (service account) + list/download/rename no Drive
- **parser.py** — regras determinísticas de parsing por pasta + regex
- **llm_fallback.py** — chamada à Anthropic API para campos ambíguos
- **notifier.py** — notificações via Telegram (stdlib, sem `requests`)
- **state.py** — cache persistente em JSON (não notifica 2x o mesmo arquivo)
- **main.py** — loop principal (polling + orquestração)

## Logs

O script loga em INFO por padrão (útil pra ver ciclos de polling, renames, erros). Trocar `logging.basicConfig(level=logging.INFO)` em `main.py` para `DEBUG` se precisar mais verbosidade.

## Troubleshooting

- **"pdftotext não encontrado"** — instalar `poppler-utils` (`apt-get install poppler-utils` no Linux/Mac via Homebrew)
- **Erro de autenticação Drive** — verificar se o service account tem acesso às pastas (compartilhar como Editor)
- **Arquivo não renomeado, mas parecia válido** — checar logs; se campo obrigatório não foi reconhecido, vai pra Telegram

## Próximos Passos

- Melhorar detecção de banco em Pasta 04 (atualmente heurística no nome do arquivo)
- Implementar lógica de duplicata em Pasta 03 (comparar hash/tamanho)
- Adicionar teste unitários (`pytest`)
