#!/bin/bash

# ================================================
# Deploy per service ke GKE melalui Bastion VM (image dari Nexus)
# Application: ms-bribrain-nilam-ocr-kk (orchestrator, guardrails, extraction, structuring, scoring)
#
# Usage:
#   ./deploy.sh <service> [tag]           # satu service
#   ./deploy.sh <service> <service> [..]  # beberapa service, tag default
#   ./deploy.sh all [tag]                 # kelimanya (urutan: hilir dulu, orchestrator terakhir)
#   ./deploy.sh --tag 1.0.1 scoring       # tag eksplisit
#   ./deploy.sh --keep-config scoring     # jangan timpa ConfigMap yang sudah ada di cluster
#                                         # (mis. nilainya diubah lewat Rancher)
#
# Service: orchestrator guardrails extraction structuring scoring
#
# Kredensial Nexus, secret aplikasi, dan alamat lingkungan dibaca dari deploy.env
# (di sebelah script ini), dari environment variable, atau ditanyakan bila kurang.
# Salin deploy.env.example menjadi deploy.env lalu isi.
#
# Secret aplikasi bisa dibuat di Rancher (Storage > Secrets) sebelum deploy; kalau API_KEY dan
# DATABASE_URL di deploy.env kosong, script memakai Secret yang sudah ada dan tidak mengubahnya.
# ================================================

set -euo pipefail

cd "$(dirname "$0")"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Configuration (boleh ditimpa dari deploy.env)
PROJECT_ID="shared-kubecluster-dev-01"
CLUSTER_NAME="gc-bigpool-dev-gke-cluster-01"
REGION="asia-southeast2"
NAMESPACE="bribrain-ocr-nilam"
DEFAULT_TAG="1.0.0"
K8S_DIR="k8s"

# Nexus Configuration
NEXUS_REGISTRY_URL="new-nexus.gcp.bri.co.id"
NEXUS_SECRET_NAME="nexus-registry-secret"
APP_SECRET_NAME="ms-bribrain-nilam-ocr-kk-secrets"

# Nilai lingkungan yang mengisi placeholder di manifest (wajib diisi di deploy.env)
FILE_URL_ALLOWED_HOSTS="${FILE_URL_ALLOWED_HOSTS:-}"

# Secret aplikasi (dibuat dari deploy.env bila diisi; kalau kosong, Secret harus sudah ada di cluster)
API_KEY="${API_KEY:-}"
API_KEYS="${API_KEYS:-}"
DATABASE_URL="${DATABASE_URL:-}"
ORCHESTRATION_API_KEY="${ORCHESTRATION_API_KEY:-}"
ELASTIC_APM_SECRET_TOKEN="${ELASTIC_APM_SECRET_TOKEN:-}"
EXTRACTION_OCR_API_KEY="${EXTRACTION_OCR_API_KEY:-}"
ORCHESTRATION_CALLBACK_KEY="${ORCHESTRATION_CALLBACK_KEY:-}"

if [ -f deploy.env ]; then
    # shellcheck disable=SC1091
    source deploy.env
fi

# Hilir dulu supaya hulu tidak memanggil service yang belum ada.
ALL_SERVICES=(scoring structuring extraction guardrails orchestrator)

STEP=0
step() { STEP=$((STEP + 1)); echo -e "\n${YELLOW}Step $STEP: $1${NC}"; }
ok()   { echo -e "${GREEN}✓ $1${NC}"; }
fail() { echo -e "${RED}✗ $1${NC}"; exit 1; }

usage() {
    sed -n '4,22p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

# Escape untuk sisi pengganti sed: \ & dan delimiter |
sed_escape() { printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'; }

# ---------- argumen ----------
TAG="$DEFAULT_TAG"
KEEP_CONFIG=0
REQUESTED=()
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help) usage 0 ;;
        --tag) TAG="${2:?--tag butuh nilai}"; shift 2 ;;
        --keep-config) KEEP_CONFIG=1; shift ;;
        all) REQUESTED+=("${ALL_SERVICES[@]}"); shift ;;
        orchestrator|guardrails|extraction|structuring|scoring) REQUESTED+=("$1"); shift ;;
        *)
            # argumen terakhir yang bukan nama service dianggap tag (bentuk: ./deploy.sh scoring 1.0.1)
            if [ ${#REQUESTED[@]} -gt 0 ] && [ $# -eq 1 ]; then TAG="$1"; shift
            else echo "Argumen tidak dikenal: $1" >&2; usage 1; fi ;;
    esac
done
[ ${#REQUESTED[@]} -gt 0 ] || usage 1

# Hilangkan duplikat, pertahankan urutan: untuk 'all' sesuai ALL_SERVICES, selain itu sesuai input.
SERVICES=()
for s in "${REQUESTED[@]}"; do
    dup=0
    for t in "${SERVICES[@]:-}"; do [ "$t" = "$s" ] && dup=1; done
    [ "$dup" -eq 1 ] || SERVICES+=("$s")
done

echo -e "${GREEN}================================================${NC}"
echo -e "${GREEN}Starting Deployment Process${NC}"
echo -e "${GREEN}Services:  ${SERVICES[*]}${NC}"
echo -e "${GREEN}Version:   $TAG${NC}"
echo -e "${GREEN}Cluster:   $CLUSTER_NAME ($PROJECT_ID)${NC}"
echo -e "${GREEN}Namespace: $NAMESPACE${NC}"
echo -e "${GREEN}================================================${NC}"

step "Checking required tools..."
for cmd in kubectl gcloud sed; do
    command -v "$cmd" &> /dev/null || fail "$cmd not found! Please install it first."
    ok "$cmd is installed"
done

step "Checking manifest files in '$K8S_DIR/'..."
for s in "${SERVICES[@]}"; do
    for f in configmap.yaml deployment.yaml service.yaml; do
        [ -f "$K8S_DIR/$s/$f" ] || fail "Missing required manifest: $K8S_DIR/$s/$f"
    done
done
ok "Required manifests found"

step "Loading Nexus credentials..."
if [ -z "${NEXUS_USERNAME:-}" ]; then
    read -rp "Nexus username: " NEXUS_USERNAME || true
fi
if [ -z "${NEXUS_PASSWORD:-}" ]; then
    read -rsp "Nexus password: " NEXUS_PASSWORD || true
    echo
fi
NEXUS_EMAIL="${NEXUS_EMAIL:-}"
[ -n "${NEXUS_USERNAME:-}" ] && [ -n "${NEXUS_PASSWORD:-}" ] || fail "Nexus username and password are required"
ok "Credentials loaded for user '$NEXUS_USERNAME'"

step "Configuring kubectl for GKE cluster..."
gcloud container clusters get-credentials "$CLUSTER_NAME" \
    --region="$REGION" \
    --project="$PROJECT_ID" \
    || fail "Failed to configure kubectl"
ok "Successfully configured kubectl"

step "Verifying cluster connection..."
kubectl cluster-info || fail "Cannot reach the cluster"
ok "Connected to cluster"

step "Checking namespace '$NAMESPACE'..."
if kubectl get namespace "$NAMESPACE" &> /dev/null; then
    ok "Namespace '$NAMESPACE' exists"
else
    echo -e "${YELLOW}Creating namespace '$NAMESPACE'...${NC}"
    kubectl create namespace "$NAMESPACE"
    ok "Namespace created"
fi

step "Deciding ConfigMap handling..."
declare -A APPLY_CONFIG
for s in "${SERVICES[@]}"; do
    APPLY_CONFIG[$s]=1
    if [ "$KEEP_CONFIG" -eq 1 ] && kubectl get configmap "ms-bribrain-nilam-ocr-kk-$s-config" -n "$NAMESPACE" &> /dev/null; then
        APPLY_CONFIG[$s]=0
        ok "$s: ConfigMap yang ada di cluster dipertahankan (--keep-config)"
    elif [ "$KEEP_CONFIG" -eq 1 ]; then
        echo -e "${YELLOW}$s: ConfigMap belum ada di cluster, dibuat dari manifest${NC}"
    fi
done

need() {  # need <VAR> <service...>: wajib terisi bila ConfigMap salah satu service itu akan di-apply
    local var="$1"; shift
    for s in "${SERVICES[@]}"; do
        [ "${APPLY_CONFIG[$s]}" -eq 1 ] || continue
        for w in "$@"; do
            if [ "$s" = "$w" ] && [ -z "${!var:-}" ]; then
                fail "$var kosong; isi di deploy.env (dibutuhkan oleh $s), atau buat ConfigMap-nya lebih dulu dan pakai --keep-config"
            fi
        done
    done
}
need FILE_URL_ALLOWED_HOSTS orchestrator extraction
ok "Environment values present"

step "Setting up Nexus registry secret..."
kubectl create secret docker-registry "$NEXUS_SECRET_NAME" \
    --docker-server="$NEXUS_REGISTRY_URL" \
    --docker-username="$NEXUS_USERNAME" \
    --docker-password="$NEXUS_PASSWORD" \
    --docker-email="$NEXUS_EMAIL" \
    --namespace="$NAMESPACE" \
    --dry-run=client -o yaml | kubectl apply -f - \
    || fail "Failed to create registry secret"
ok "Registry secret '$NEXUS_SECRET_NAME' is up to date"

step "Setting up application secret '$APP_SECRET_NAME'..."
if [ -n "$API_KEY" ] && [ -n "$DATABASE_URL" ]; then
    SECRET_ARGS=(--from-literal=API_KEY="$API_KEY" --from-literal=DATABASE_URL="$DATABASE_URL")
    [ -z "$API_KEYS" ] || SECRET_ARGS+=(--from-literal=API_KEYS="$API_KEYS")
    [ -z "$EXTRACTION_OCR_API_KEY" ] || SECRET_ARGS+=(--from-literal=EXTRACTION_OCR_API_KEY="$EXTRACTION_OCR_API_KEY")
    [ -z "$ORCHESTRATION_API_KEY" ] || SECRET_ARGS+=(--from-literal=ORCHESTRATION_API_KEY="$ORCHESTRATION_API_KEY")
    [ -z "$ELASTIC_APM_SECRET_TOKEN" ] || SECRET_ARGS+=(--from-literal=ELASTIC_APM_SECRET_TOKEN="$ELASTIC_APM_SECRET_TOKEN")
    [ -z "$ORCHESTRATION_CALLBACK_KEY" ] || SECRET_ARGS+=(--from-literal=ORCHESTRATION_CALLBACK_KEY="$ORCHESTRATION_CALLBACK_KEY")
    kubectl create secret generic "$APP_SECRET_NAME" "${SECRET_ARGS[@]}"         --namespace="$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -         || fail "Failed to create application secret"
    ok "Secret '$APP_SECRET_NAME' is up to date (dari deploy.env)"
elif kubectl get secret "$APP_SECRET_NAME" -n "$NAMESPACE" &> /dev/null; then
    # Secret dibuat di luar script (mis. lewat Rancher): jangan diubah, cukup periksa key wajibnya.
    SECRET_KEYS=" $(kubectl get secret "$APP_SECRET_NAME" -n "$NAMESPACE" -o go-template='{{range $k, $v := .data}}{{$k}} {{end}}') "
    MISSING=()
    [[ "$SECRET_KEYS" == *" API_KEY "* ]] || MISSING+=(API_KEY)
    for s in "${SERVICES[@]}"; do
        case "$s" in
            orchestrator|extraction|structuring|scoring)
                [[ "$SECRET_KEYS" == *" DATABASE_URL "* ]] || MISSING+=("DATABASE_URL")
                break ;;
        esac
    done
    if [ ${#MISSING[@]} -gt 0 ]; then
        fail "Secret '$APP_SECRET_NAME' ada tapi key berikut belum ada: ${MISSING[*]}. Tambahkan di Rancher (Storage > Secrets > $APP_SECRET_NAME > Edit)"
    fi
    ok "Secret '$APP_SECRET_NAME' sudah ada di cluster (key wajib lengkap), tidak diubah"
else
    echo -e "${RED}Secret '$APP_SECRET_NAME' belum ada di namespace '$NAMESPACE'.${NC}"
    echo "Pilih salah satu:"
    echo "  1) Isi API_KEY dan DATABASE_URL di deploy.env, lalu jalankan ulang script ini."
    echo "     Atau: salin secret.yaml.example ke secret.yaml, isi, lalu kubectl apply -f secret.yaml (muncul juga di Rancher)."
    echo "  2) Buat di Rancher: cluster > Storage > Secrets > Create > Opaque"
    echo "       Namespace: $NAMESPACE   Name: $APP_SECRET_NAME"
    echo "       Key wajib: API_KEY, DATABASE_URL"
    echo "       Key opsional: API_KEYS, EXTRACTION_OCR_API_KEY (wajib untuk extraction), ORCHESTRATION_API_KEY, ORCHESTRATION_CALLBACK_KEY"
    exit 1
fi

RENDER_DIR=$(mktemp -d)
trap 'rm -rf "$RENDER_DIR"' EXIT

for SERVICE in "${SERVICES[@]}"; do
    APP_NAME="ms-bribrain-nilam-ocr-kk-$SERVICE"
    echo -e "\n${GREEN}------------------------------------------------${NC}"
    echo -e "${GREEN}Service: $SERVICE ($APP_NAME)${NC}"
    echo -e "${GREEN}------------------------------------------------${NC}"

    step "[$SERVICE] Rendering manifests..."
    mkdir -p "$RENDER_DIR/$SERVICE"
    for f in configmap.yaml deployment.yaml service.yaml; do
        if [ "$f" = "configmap.yaml" ] && [ "${APPLY_CONFIG[$SERVICE]}" -eq 0 ]; then continue; fi
        sed -e "s|<NEXUS_REGISTRY_URL>|$(sed_escape "$NEXUS_REGISTRY_URL")|g" \
            -e "s|<IMAGE_VERSION>|$(sed_escape "$TAG")|g" \
            -e "s|<FILE_URL_ALLOWED_HOSTS>|$(sed_escape "$FILE_URL_ALLOWED_HOSTS")|g" \
            "$K8S_DIR/$SERVICE/$f" > "$RENDER_DIR/$SERVICE/$f"
        if grep -qE '<[A-Z_]+>' "$RENDER_DIR/$SERVICE/$f"; then
            fail "Unreplaced placeholder left in $K8S_DIR/$SERVICE/$f"
        fi
    done
    ok "Image: $(grep -m1 'image:' "$RENDER_DIR/$SERVICE/deployment.yaml" | awk '{print $2}')"

    CONFIG_OUTPUT=""
    if [ "${APPLY_CONFIG[$SERVICE]}" -eq 1 ]; then
        step "[$SERVICE] Applying ConfigMap..."
        CONFIG_OUTPUT=$(kubectl apply -f "$RENDER_DIR/$SERVICE/configmap.yaml")
        echo "$CONFIG_OUTPUT"
        ok "ConfigMap applied"
    else
        step "[$SERVICE] ConfigMap dilewati (--keep-config)"
        ok "ConfigMap di cluster tidak diubah"
    fi

    step "[$SERVICE] Applying Deployment..."
    DEPLOY_OUTPUT=$(kubectl apply -f "$RENDER_DIR/$SERVICE/deployment.yaml")
    echo "$DEPLOY_OUTPUT"
    # ConfigMap berubah atau image di-push ulang dengan tag sama: pod tidak ikut berganti sendiri
    if [[ "$DEPLOY_OUTPUT" == *unchanged* ]]; then
        echo -e "${YELLOW}Manifest unchanged, restarting pods to pick up latest image/config...${NC}"
        kubectl rollout restart deployment/"$APP_NAME" -n "$NAMESPACE"
    elif [[ "$CONFIG_OUTPUT" == *configured* ]]; then
        kubectl rollout restart deployment/"$APP_NAME" -n "$NAMESPACE"
    fi
    ok "Deployment applied"

    step "[$SERVICE] Applying Service..."
    kubectl apply -f "$RENDER_DIR/$SERVICE/service.yaml"
    ok "Service applied"

    # HPA opsional: hanya service yang punya hpa.yaml (guardrails, satu-satunya yang CPU-bound).
    if [ -f "$K8S_DIR/$SERVICE/hpa.yaml" ]; then
        step "[$SERVICE] Applying HorizontalPodAutoscaler..."
        cp "$K8S_DIR/$SERVICE/hpa.yaml" "$RENDER_DIR/$SERVICE/hpa.yaml"
        kubectl apply -f "$RENDER_DIR/$SERVICE/hpa.yaml"
        ok "HPA applied"
    fi

    # Ingress opsional: hanya service yang punya ingress.yaml (orchestrator) yang dibuka ke luar cluster.
    if [ -f "$K8S_DIR/$SERVICE/ingress.yaml" ]; then
        step "[$SERVICE] Applying Ingress..."
        cp "$K8S_DIR/$SERVICE/ingress.yaml" "$RENDER_DIR/$SERVICE/ingress.yaml"
        kubectl apply -f "$RENDER_DIR/$SERVICE/ingress.yaml"
        ok "Ingress applied"
    fi

    step "[$SERVICE] Waiting for deployment to complete..."
    if kubectl rollout status deployment/"$APP_NAME" -n "$NAMESPACE" --timeout=10m; then
        ok "$SERVICE deployed successfully"
    else
        echo -e "${RED}✗ Deployment of $SERVICE failed${NC}"
        echo -e "\n${YELLOW}Pod status:${NC}"
        kubectl get pods -n "$NAMESPACE" -l app="$APP_NAME" || true
        echo -e "\n${YELLOW}Recent logs:${NC}"
        kubectl logs deployment/"$APP_NAME" -n "$NAMESPACE" --tail=30 || true
        echo -e "\n${YELLOW}Recent events in '$NAMESPACE':${NC}"
        kubectl get events -n "$NAMESPACE" --sort-by=.lastTimestamp | tail -n 20 || true
        echo -e "\n${YELLOW}To roll back to the previous version, run:${NC}"
        echo -e "kubectl rollout undo deployment/$APP_NAME -n $NAMESPACE"
        exit 1
    fi
done

echo -e "\n${GREEN}================================================${NC}"
echo -e "${GREEN}Deployment Summary${NC}"
echo -e "${GREEN}================================================${NC}"

echo -e "\n${YELLOW}Pods:${NC}"
kubectl get pods -n "$NAMESPACE" -l app.kubernetes.io/part-of=ms-bribrain-nilam-ocr-kk -o wide

echo -e "\n${YELLOW}Services:${NC}"
kubectl get svc -n "$NAMESPACE" -l app.kubernetes.io/part-of=ms-bribrain-nilam-ocr-kk

echo -e "
${YELLOW}Ingress:${NC}"
kubectl get ingress -n "$NAMESPACE" -l app.kubernetes.io/part-of=ms-bribrain-nilam-ocr-kk

echo -e "\n${GREEN}================================================${NC}"
echo -e "${GREEN}Deployment completed successfully!${NC}"
echo -e "${GREEN}================================================${NC}"

echo -e "\n${GREEN}To view live logs, run:${NC}"
echo -e "kubectl logs -f deployment/ms-bribrain-nilam-ocr-kk-<service> -n $NAMESPACE"

echo -e "\n${GREEN}To roll back a service, run:${NC}"
echo -e "kubectl rollout undo deployment/ms-bribrain-nilam-ocr-kk-<service> -n $NAMESPACE"
