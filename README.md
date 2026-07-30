# Personal File Organizer

Renomeia automaticamente os PDFs financeiros no Google Drive — notas de negociação BTG, relatórios de performance, extratos e faturas de banco. Roda 24/7 numa VPS (Coolify) e reage em segundos, via webhook do Google Drive.

## Como funciona

1. Um PDF chega numa das pastas monitoradas.
2. O Drive chama `POST /drive-webhook`.
3. O serviço lista as mudanças, baixa o PDF, extrai os campos e renomeia.
4. Cada resultado vira uma mensagem no Telegram.

No boot também roda uma **varredura completa** das pastas, para recuperar o que tenha chegado com o serviço fora do ar.

### Detalhes que importam

- **Rajada de webhooks.** O Drive dispara várias notificações por upload. As varreduras são serializadas por um lock e agrupadas por um debounce — sem isso o mesmo arquivo é processado N vezes e o Telegram enche.
- **Extração por coluna.** As notas do BTG são tabelas: com `pdftotext -layout`, o cabeçalho `Valor Líquido` fica numa linha e o valor numa linha seguinte, na mesma posição horizontal. Procurar `valor líquido\s+N` por regex nunca funciona — o valor é localizado por alinhamento de coluna.
- **Nunca sobrescreve.** Se o nome de destino já existe, ganha sufixo `(2)`, `(3)`...
- **`DRY_RUN` não "queima" arquivos.** O cache marca o registro como simulação, então ao desligar o `DRY_RUN` os arquivos são renomeados de verdade.
- **Banco vem da subpasta.** Na Pasta 04 o banco é o nome da subpasta (`BTG`, `Itau`), não um palpite pelo nome do arquivo. Subpasta nova passa a funcionar sozinha.
- **PDF com senha** vira aviso no Telegram, não erro silencioso.

## Padrões de nome

| Pasta | Padrão |
| :---- | :----- |
| 01 — Notas de Corretagem | `MM-DD - Ativo - TipoOp - R$Valor.pdf` |
| 02 — Performance | `AA-MM-DD - Performance - AA-MM - AA-MM.pdf` |
| 03 — Extrato Investimentos | `AAAA-MM.pdf` |
| 04 — Banking | `AAAA-MM - Banco.pdf` ou `AA-MM - AA-MM - Banking.pdf` |

Campo não reconhecido vira `??`. Se **nenhum** campo for reconhecido, o arquivo não é renomeado — só gera aviso.

## Variáveis de ambiente

Copiar `.env.example` para `.env`. Obrigatórias:

- `GOOGLE_SERVICE_ACCOUNT_JSON` — credencial completa do service account
- `DRIVE_FOLDER_01..04` — IDs das pastas
- `WEBHOOK_BASE_URL` — **precisa ser HTTPS** (o Google recusa HTTP)
- `WEBHOOK_TOKEN` — string aleatória que autentica as chamadas do Drive
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`

Opcionais: `DRY_RUN`, `WEBHOOK_DEBOUNCE_SECONDS`, `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `USAR_LLM_FALLBACK`, `STATE_DIR`, `LOG_LEVEL`.

## Endpoints

| Rota | O que faz |
| :--- | :-------- |
| `GET /` | Health check com contadores (varreduras, renomeados, último erro) |
| `POST /drive-webhook` | Recebe as notificações do Drive |
| `POST /varrer` | Varredura manual — exige o header `x-token: $WEBHOOK_TOKEN` |

Forçar uma varredura completa (útil logo depois de desligar o `DRY_RUN`):

```bash
curl -X POST https://SEU-HOST/varrer -H "x-token: SEU_TOKEN"
```

## Rodando local

```bash
pip install -r requirements.txt          # precisa também do poppler-utils
cp .env.example .env                     # preencher
uvicorn app.main:app --reload
```

Para o webhook funcionar local é preciso um túnel HTTPS público (ex.: `ngrok http 8000`) e apontar `WEBHOOK_BASE_URL` para a URL gerada. Sem túnel, o serviço sobe e a varredura do boot funciona, mas não chega notificação.

## Testes

```bash
python app/tests/test_regressao_parser.py
```

Roda o parser contra os PDFs reais já renomeados e compara com o nome atual de cada arquivo — que é o ground truth. Exige a pasta do Drive sincronizada localmente (ou `PFO_PASTA_RAIZ` apontando para ela).

## Estrutura

| Arquivo | Responsabilidade |
| :------ | :--------------- |
| `config.py` | Variáveis de ambiente e validação |
| `drive_client.py` | Auth, webhook, listagem, download, rename, colisão de nome |
| `parser.py` | Extração determinística por pasta (sem rede — testável offline) |
| `llm_fallback.py` | Último recurso via Anthropic, com validação de formato |
| `notifier.py` | Telegram (stdlib), com anti-flood |
| `state.py` | Cache persistente do que já foi processado |
| `main.py` | FastAPI, webhook, lock/debounce e orquestração |

## Problemas comuns

- **`WebHook callback must be HTTPS`** — `WEBHOOK_BASE_URL` está em HTTP. Configure um domínio com TLS.
- **Nada acontece ao subir um arquivo** — o Drive só notifica mudanças *reais*; re-subir um arquivo idêntico que já está lá pode não gerar evento. Use `POST /varrer`.
- **`pdftotext não encontrado`** — falta `poppler-utils` (já vem no Dockerfile).
- **Erro de permissão no rename** — as pastas precisam estar compartilhadas como **Editor** com o e-mail do service account.
