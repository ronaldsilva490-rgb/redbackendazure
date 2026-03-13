#!/bin/bash
set -e

echo "🔍 Verificando volume montado em /data/auth_info_baileys..."
mkdir -p /data/auth_info_baileys

# Garante que o symlink aponta para o volume (recria sempre)
rm -f /app/whatsapp-service/auth_info_baileys
ln -sfn /data/auth_info_baileys /app/whatsapp-service/auth_info_baileys
echo "🔗 Symlink: /app/whatsapp-service/auth_info_baileys -> /data/auth_info_baileys"

# Sanidade: se existirem pastas de tenant sem creds.json, remove para evitar loop sem QR
for tenant_dir in /data/auth_info_baileys/tenant_*/; do
  if [ -d "$tenant_dir" ] && [ ! -f "${tenant_dir}creds.json" ]; then
    echo "⚠️  Removendo dir de tenant corrompido (sem creds.json): $tenant_dir"
    rm -rf "$tenant_dir"
  fi
done

echo "🚀 Iniciando Microserviço WhatsApp (Node.js) em background..."
cd /app/whatsapp-service && node index.js &
NODE_PID=$!

sleep 3

if ! kill -0 $NODE_PID 2>/dev/null; then
  echo "❌ Microserviço WhatsApp falhou ao iniciar!"
  exit 1
fi

echo "✅ Microserviço WhatsApp rodando (PID $NODE_PID)"
echo "🚀 Iniciando API Principal (Gunicorn/Flask)..."
cd /app && exec gunicorn --bind 0.0.0.0:7860 --workers 2 --worker-class sync --timeout 60 main:app
