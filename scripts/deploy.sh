#!/usr/bin/env bash
# =============================================================================
# deploy.sh — Deploy scraper-api to a remote Raspberry Pi (or any Linux host)
#              running Podman + Quadlet.
# =============================================================================
# Usage:
#   ./scripts/deploy.sh user@raspberry-host          # basic deploy (build locally)
#   ./scripts/deploy.sh user@host --tag v1.0.0       # pull pre-built image from GHCR
#   ./scripts/deploy.sh user@host --with-env         # overwrite remote .env
#   ./scripts/deploy.sh user@host --without-config   # skip YAML config
#   ./scripts/deploy.sh user@host --pull-only        # only pull, don't build
#   ./scripts/deploy.sh user@host --no-healthcheck   # skip health check
#   ./scripts/deploy.sh user@host --key-path ~/.ssh/id_rsa --port 2222
#
# Default remote directory: /home/<remote-user>/scraper-api
# =============================================================================

set -euo pipefail

# ------------------------------------------------------------------- config
REMOTE_USER=""
REMOTE_HOST=""
REMOTE_DIR=""
SSH_PORT=22
KEY_PATH=""
WITH_ENV=false
WITHOUT_CONFIG=false
PULL_ONLY=false
NO_HEALTHCHECK=false
SHOW_HELP=false
IMAGE_TAG=""
IMAGE_REGISTRY="ghcr.io/javiyt/scrapping-service"

# ------------------------------------------------------------------ helpers

remote_exec() {
    local ssh_cmd="ssh"
    [[ -n "$KEY_PATH" ]] && ssh_cmd="$ssh_cmd -i $KEY_PATH"
    [[ -n "$SSH_PORT" ]] && ssh_cmd="$ssh_cmd -p $SSH_PORT"
    $ssh_cmd "${REMOTE_USER}@${REMOTE_HOST}" "$@"
}

remote_copy() {
    local src="$1"
    local dst="$2"
    local scp_cmd="scp"
    [[ -n "$KEY_PATH" ]] && scp_cmd="$scp_cmd -i $KEY_PATH"
    [[ -n "$SSH_PORT" ]] && scp_cmd="$scp_cmd -P $SSH_PORT"
    $scp_cmd "$src" "${REMOTE_USER}@${REMOTE_HOST}:${dst}"
}

remote_copy_dir() {
    local src="$1"
    local dst="$2"
    local scp_cmd="scp -r"
    [[ -n "$KEY_PATH" ]] && scp_cmd="$scp_cmd -i $KEY_PATH"
    [[ -n "$SSH_PORT" ]] && scp_cmd="$scp_cmd -P $SSH_PORT"
    $scp_cmd "$src" "${REMOTE_USER}@${REMOTE_HOST}:${dst}"
}

# ------------------------------------------------------------- parse args
POSITIONAL=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --key-path)     KEY_PATH="$2";     shift 2 ;;
        --port)         SSH_PORT="$2";     shift 2 ;;
        --app-dir)      REMOTE_DIR="$2";   shift 2 ;;
        --with-env)     WITH_ENV=true;     shift ;;
        --without-config) WITHOUT_CONFIG=true; shift ;;
        --pull-only)    PULL_ONLY=true;    shift ;;
        --no-healthcheck) NO_HEALTHCHECK=true; shift ;;
        --tag)          IMAGE_TAG="$2";    shift 2 ;;
        --help)         SHOW_HELP=true;    shift ;;
        *)              POSITIONAL+=("$1"); shift ;;
    esac
done

[[ $SHOW_HELP == true ]] && {
    echo ""
    echo "Deploy scraper-api to a remote host via SSH."
    echo ""
    echo "Usage:"
    echo "  $0 user@host [options]"
    echo ""
    echo "Options:"
    echo "  --key-path PATH        SSH private key path"
    echo "  --port PORT            SSH port (default: 22)"
    echo "  --app-dir PATH         Remote app directory (default: ~/scraper-api)"
    echo "  --with-env             Upload local .env (default: preserve remote)"
    echo "  --tag TAG             Pull pre-built image from GHCR (e.g. v1.0.0, latest)"
    echo "  --without-config       Skip uploading config YAML"
    echo "  --pull-only            Only pull base image, skip build (local build mode)"
    echo "  --no-healthcheck       Skip health check after deploy"
    echo "  --help                 Show this help"
    echo ""
    exit 0
}

# Parse user@host
if [[ ${#POSITIONAL[@]} -eq 0 ]]; then
    echo "Error: remote host required (user@host)"
    echo "Usage: $0 user@host [options]"
    exit 1
fi

REMOTE_TARGET="${POSITIONAL[0]}"
if [[ "$REMOTE_TARGET" == *"@"* ]]; then
    REMOTE_USER="${REMOTE_TARGET%%@*}"
    REMOTE_HOST="${REMOTE_TARGET#*@}"
else
    echo "Error: invalid format '$REMOTE_TARGET'. Use user@host"
    exit 1
fi

[[ -z "$REMOTE_DIR" ]] && REMOTE_DIR="/home/${REMOTE_USER}/scraper-api"

echo "═══ Deploying scraper-api to ${REMOTE_USER}@${REMOTE_HOST} ═══"
if [[ -n "$IMAGE_TAG" ]]; then
    IMAGE_REF="${IMAGE_REGISTRY}:${IMAGE_TAG}"
    echo "  Mode:        Pull from GHCR (${IMAGE_REF})"
else
    IMAGE_REF="localhost/scraper-api:latest"
    echo "  Mode:        Build locally on remote"
fi
echo "  Remote dir:  $REMOTE_DIR"
echo "  SSH port:    $SSH_PORT"
echo "  With .env:   $WITH_ENV"
echo "  Pull only:   $PULL_ONLY"
echo ""

# ------------------------------------------------------------- setup remote
echo "▸ Creating remote directories..."
remote_exec "mkdir -p ${REMOTE_DIR}/{app,configs,data,debug,logs,remote}"

# ----------------------------------------------------------- preserve .env
if [[ "$WITH_ENV" != "true" ]]; then
    echo "▸ Preserving remote .env (if exists)..."
    remote_exec "[[ -f ${REMOTE_DIR}/.env ]] && cp ${REMOTE_DIR}/.env ${REMOTE_DIR}/.env.backup || true"
fi

# --------------------------------------------------------------- copy files
if [[ -z "$IMAGE_TAG" ]]; then
    # Local build mode — copy source code and Dockerfile
    echo "▸ Copying source files..."
    remote_copy_dir "$(dirname "$0")/../app/"     "${REMOTE_DIR}/app/"
    remote_copy_dir "$(dirname "$0")/../remote/"  "${REMOTE_DIR}/remote/"

    remote_copy "$(dirname "$0")/../requirements.txt"         "${REMOTE_DIR}/requirements.txt"
    remote_copy "$(dirname "$0")/../Dockerfile"               "${REMOTE_DIR}/Dockerfile"
    remote_copy "$(dirname "$0")/../.dockerignore"            "${REMOTE_DIR}/.dockerignore"
else
    # GHCR mode — only copy the Quadlet file (remote/ dir)
    echo "▸ Copying Quadlet files..."
    remote_copy_dir "$(dirname "$0")/../remote/"  "${REMOTE_DIR}/remote/"
fi

if [[ "$WITHOUT_CONFIG" != "true" ]]; then
    echo "▸ Copying config..."
    remote_copy "$(dirname "$0")/../configs/config.example.yaml" "${REMOTE_DIR}/configs/config.yaml"
fi

if [[ "$WITH_ENV" == "true" ]]; then
    [[ -f "$(dirname "$0")/../.env" ]] && remote_copy "$(dirname "$0")/../.env" "${REMOTE_DIR}/.env"
    remote_exec "chmod 600 ${REMOTE_DIR}/.env"
fi

# ------------------------------------------------------------- build / pull image
if [[ -n "$IMAGE_TAG" ]]; then
    echo "▸ Pulling pre-built image ${IMAGE_REF}..."
    remote_exec "podman pull ${IMAGE_REF}"
elif [[ "$PULL_ONLY" == "true" ]]; then
    echo "▸ Pulling base image (no build)..."
    remote_exec "cd ${REMOTE_DIR} && podman pull python:3.12-slim-bookworm"
else
    echo "▸ Building container image (this may take a while on a Pi)..."
    remote_exec "cd ${REMOTE_DIR} && podman build -t localhost/scraper-api:latest -f Dockerfile ."
fi

# ----------------------------------------------------------- deploy Quadlet
echo "▸ Deploying Quadlet service..."
remote_exec "mkdir -p ~/.config/containers/systemd/"
remote_exec "cp ${REMOTE_DIR}/remote/scraper-api.container ~/.config/containers/systemd/"

# When pulling from GHCR, update the Quadlet image reference so systemd
# uses the remote image (and survives reboots/restarts).
if [[ -n "$IMAGE_TAG" ]]; then
    echo "▸ Patching Quadlet image reference to ${IMAGE_REF}..."
    remote_exec "sed -i 's|^Image=.*|Image=${IMAGE_REF}|' ~/.config/containers/systemd/scraper-api.container"
fi

echo "▸ Reloading systemd and enabling linger..."
remote_exec "systemctl --user daemon-reload"
remote_exec "loginctl enable-linger ${REMOTE_USER} 2>/dev/null || true"

echo "▸ Starting / restarting service..."
remote_exec "systemctl --user restart scraper-api.service" || {
    echo "⚠ Warning: restart failed. Trying start..."
    remote_exec "systemctl --user start scraper-api.service"
}

# ----------------------------------------------------------- health check
if [[ "$NO_HEALTHCHECK" != "true" ]]; then
    echo "▸ Waiting for health check..."
    sleep 5
    HEALTH_OK=false
    for i in $(seq 1 12); do
        if remote_exec "curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/health" 2>/dev/null | grep -q 200; then
            HEALTH_OK=true
            break
        fi
        echo "   Attempt $i/12..."
        sleep 5
    done

    if [[ "$HEALTH_OK" == "true" ]]; then
        echo "✓ Health check passed!"
    else
        echo "✗ Health check failed. Check service status:"
        echo "  systemctl --user status scraper-api.service"
        echo "  journalctl --user -u scraper-api.service"
    fi
fi

# ----------------------------------------------------------- restore .env
if [[ "$WITH_ENV" != "true" ]]; then
    echo "▸ Restoring preserved .env (if exists)..."
    remote_exec "[[ -f ${REMOTE_DIR}/.env.backup ]] && mv ${REMOTE_DIR}/.env.backup ${REMOTE_DIR}/.env || true"
fi

# ------------------------------------------------------------- print summary
echo ""
echo "═══ Deploy complete ═══"
echo ""
echo "Management commands:"
echo ""
echo "  # View logs"
echo "  journalctl --user -u scraper-api.service -f"
echo ""
echo "  # Check status"
echo "  systemctl --user status scraper-api.service"
echo ""
echo "  # Restart"
echo "  systemctl --user restart scraper-api.service"
echo ""
echo "  # Stop"
echo "  systemctl --user stop scraper-api.service"
echo ""
echo "  # Test the API"
echo "  curl -s http://${REMOTE_HOST}:8080/health"
echo "  curl -s -H 'Authorization: Bearer \$(cat ${REMOTE_DIR}/.env | grep SCRAPER_API_KEY | cut -d= -f2)' \\"
echo "       -H 'Content-Type: application/json' \\"
echo "       -d '{\"url\":\"https://example.com\"}' \\"
echo "       http://${REMOTE_HOST}:8080/v1/scrape"
echo ""
