FROM python:3.12-slim

WORKDIR /app

# Instalar poppler-utils (pdftotext)
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils && rm -rf /var/lib/apt/lists/*

# Copiar e instalar dependências
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar código
COPY . .

# Rodar como worker (loop infinito, sem porta exposta)
CMD ["python", "-m", "main"]
