# Personal File Organizer

Automação de renomeação de PDFs no Google Drive — notas BTG, extratos, faturas de banco. Roda 24/7 numa VPS (Coolify) com webhook do Google Drive (notificação quase imediata de mudanças).

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
- `WEBHOOK_BASE_URL` — URL pública onde o Drive pode chamar (ex: `http://localhost:8000` pra teste local, `https://organizer.barreto.ai` em produção)
- `WEBHOOK_TOKEN` — string aleatória pra validar notificações do Drive

### Executar (teste local)

```bash
export DRY_RUN=true  # Testar sem fazer rename real
uvicorn app.main:app --reload  # Roda o servidor FastAPI
```

Com `DRY_RUN=true`, o script loga o que _teria_ feito sem modificar nenhum arquivo.

### Webhook Local (para teste)

Se quiser testar o webhook localmente (sem expor porta pública):

1. Usar ngrok ou similar pra criar um túnel público: `ngrok http 8000`
2. Copiar a URL pública gerada
3. Setar `WEBHOOK_BASE_URL=https://xxx.ngrok.io` antes de rodar
4. Webhook vai funcionar (Drive vai chamar sua URL pública)

## Deploy no Coolify

1. Criar um repositório GitHub novo (`personal-file-organizer`).
2. Subir este código pro repo.
3. Seguir `COOLIFY_SETUP.md` deste workspace para:
   - Criar resource no Coolify
   - Configurar porta 8000 como exposta
   - Adicionar variáveis de env (especialmente `WEBHOOK_BASE_URL` e `WEBHOOK_TOKEN`)
   - Criar volume persistente em `/app/state`
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
- **drive_client.py** — autenticação (service account) + webhook + list/download/rename no Drive
- **parser.py** — regras determinísticas de parsing por pasta + regex
- **llm_fallback.py** — chamada à Anthropic API para campos ambíguos
- **notifier.py** — notificações via Telegram (stdlib, sem `requests`)
- **state.py** — cache persistente em JSON (não notifica 2x o mesmo arquivo)
- **main.py** — FastAPI + webhook + orquestração

## Logs

O script loga em INFO por padrão (útil pra ver notificações de webhook, renames, erros). Trocar `logging.basicConfig(level=logging.INFO)` em `main.py` para `DEBUG` se precisar mais verbosidade.

## Troubleshooting

- **"pdftotext não encontrado"** — instalar `poppler-utils` (`apt-get install poppler-utils` no Linux, `brew install poppler` no Mac)
- **Erro de autenticação Drive** — verificar se o service account tem acesso às pastas (compartilhar como Editor)
- **Webhook não registra no Drive** — verificar se `WEBHOOK_BASE_URL` é público e acessível
- **Arquivo não renomeado, mas parecia válido** — checar logs; se campo obrigatório não foi reconhecido, vai pra Telegram

## Próximos Passos

- Melhorar detecção de banco em Pasta 04 (atualmente heurística no nome do arquivo)
- Implementar lógica de duplicata em Pasta 03 (comparar hash/tamanho)
- Adicionar testes unitários (`pytest`)
