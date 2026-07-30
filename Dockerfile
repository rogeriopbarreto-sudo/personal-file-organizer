FROM python:3.12-slim

WORKDIR /

# Instalar poppler-utils (pdftotext)
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils && rm -rf /var/lib/apt/lists/*

# Copiar e instalar dependências
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar código como package
COPY . app/

# Rodar como worker (loop infinito, sem porta exposta)
CMD ["python", "-m", "app"]
