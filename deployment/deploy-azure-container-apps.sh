#!/usr/bin/env bash
# Deploy the NFS Vendor Risk Deep Agent to Azure Container Apps (PoC).
#   - builds the image in Azure Container Registry (no local Docker push needed)
#   - MCP server: internal ingress (reachable only inside the Container Apps environment)
#   - API + UI:   external HTTPS ingress
#   - console logs (JSON events) -> Log Analytics workspace created with the environment
#
# Usage:  source .env && ./deployment/deploy-azure-container-apps.sh
set -euo pipefail

: "${AZURE_OPENAI_ENDPOINT:?set AZURE_OPENAI_ENDPOINT}"
: "${AZURE_OPENAI_API_KEY:?set AZURE_OPENAI_API_KEY}"
RG="${RG:-rg-nfs-vendor-risk}"
LOCATION="${LOCATION:-westeurope}"
ACR="${ACR:-acrnfsvendorrisk$(date +%s | tail -c 6)}"
ENV_NAME="${ENV_NAME:-cae-nfs-vendor-risk}"
IMAGE="nfs-vendor-risk-agent:$(git rev-parse --short HEAD 2>/dev/null || echo latest)"

COMMON_ENV=(
  "AZURE_OPENAI_ENDPOINT=${AZURE_OPENAI_ENDPOINT}"
  "AZURE_OPENAI_API_KEY=secretref:aoai-key"
  "AZURE_OPENAI_API_VERSION=${AZURE_OPENAI_API_VERSION:-2024-10-21}"
  "AZURE_OPENAI_CHAT_DEPLOYMENT=${AZURE_OPENAI_CHAT_DEPLOYMENT:-gpt-4.1}"
  "AZURE_OPENAI_EMBEDDING_DEPLOYMENT=${AZURE_OPENAI_EMBEDDING_DEPLOYMENT:-text-embedding-3-small}"
  "LLM_TEMPERATURE=${LLM_TEMPERATURE:-0}"
  "EMBEDDING_PROVIDER=azure"
)
# Optional: a Langfuse instance reachable from Azure (self-hosted or cloud)
if [[ -n "${LANGFUSE_AZURE_HOST:-}" ]]; then
  COMMON_ENV+=("LANGFUSE_HOST=${LANGFUSE_AZURE_HOST}" "LANGFUSE_PUBLIC_KEY=${LANGFUSE_PUBLIC_KEY}"
               "LANGFUSE_SECRET_KEY=secretref:langfuse-sk")
  SECRETS=("aoai-key=${AZURE_OPENAI_API_KEY}" "langfuse-sk=${LANGFUSE_SECRET_KEY}")
else
  SECRETS=("aoai-key=${AZURE_OPENAI_API_KEY}")
fi

echo ">> resource group, registry, image build"
az group create -n "$RG" -l "$LOCATION" -o none
az acr create -g "$RG" -n "$ACR" --sku Basic --admin-enabled true -o none
az acr build -r "$ACR" -t "$IMAGE" "$(dirname "$0")/.." -o none

echo ">> container apps environment (creates a Log Analytics workspace)"
az containerapp env create -g "$RG" -n "$ENV_NAME" -l "$LOCATION" -o none

ACR_SERVER="$ACR.azurecr.io"
ACR_USER=$(az acr credential show -n "$ACR" --query username -o tsv)
ACR_PASS=$(az acr credential show -n "$ACR" --query "passwords[0].value" -o tsv)

echo ">> MCP server (internal ingress)"
az containerapp create -g "$RG" -n nfs-mcp-server --environment "$ENV_NAME" \
  --image "$ACR_SERVER/$IMAGE" --registry-server "$ACR_SERVER" \
  --registry-username "$ACR_USER" --registry-password "$ACR_PASS" \
  --ingress internal --target-port 8001 --transport http \
  --command nfs-agent --args mcp \
  --cpu 1 --memory 2Gi --min-replicas 1 --max-replicas 1 \
  --secrets "${SECRETS[@]}" --env-vars "${COMMON_ENV[@]}" "MCP_HOST=0.0.0.0" "MCP_PORT=8001" -o none

echo ">> API + UI (external ingress; image default CMD runs the API on :8000)"
az containerapp create -g "$RG" -n nfs-vendor-risk-api --environment "$ENV_NAME" \
  --image "$ACR_SERVER/$IMAGE" --registry-server "$ACR_SERVER" \
  --registry-username "$ACR_USER" --registry-password "$ACR_PASS" \
  --ingress external --target-port 8000 \
  --cpu 1 --memory 2Gi --min-replicas 1 --max-replicas 1 \
  --secrets "${SECRETS[@]}" \
  --env-vars "${COMMON_ENV[@]}" "MCP_SERVER_URL=http://nfs-mcp-server/mcp" "MCP_MODE=remote" -o none

FQDN=$(az containerapp show -g "$RG" -n nfs-vendor-risk-api --query properties.configuration.ingress.fqdn -o tsv)
echo ">> deployed: https://$FQDN   (health: https://$FQDN/api/health, API docs: https://$FQDN/docs)"
