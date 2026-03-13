FROM python:3.11-slim

# Instala Node.js 20 LTS + git (necessário pro npm/baileys)
RUN apt-get update && apt-get install -y curl git espeak-ng ffmpeg && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependências Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir --upgrade edge-tts

# Dependências Node (microserviço WhatsApp)
COPY whatsapp-service/package.json whatsapp-service/
RUN cd whatsapp-service && npm install --omit=dev

# Código da aplicação
COPY . .

# Diretório de dados persistentes do WhatsApp
RUN mkdir -p /data/auth_info_baileys

# Script de inicialização
RUN chmod +x start.sh

EXPOSE 7860

CMD ["bash", "start.sh"]
