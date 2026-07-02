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

# Cleanup temp files on exit.
cleanup() {
    [[ -n "${QUADLET_PATCHED:-}" && -f "$QUADLET_PATCHED" ]] && rm -f "$QUADLET_PATCHED"
}
trap cleanup EXIT

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

# ---------------------------------------------------------- read server port
# Reads the server port from (in priority order):
#   1. SCRAPER_SERVER_PORT env var (exported in shell)
#   2. .env file (SCRAPER_SERVER_PORT=...)
#   3. configs/config.yaml "port:" field under the "server" section
#   Default: 8080
_read_port() {
    local port="${SCRAPER_SERVER_PORT:-}"
    local project_root
    project_root="$(cd "$(dirname "$0")/.." && pwd)"

    # 2. Read from local .env file.
    if [[ -z "$port" ]]; then
        local env_file="${project_root}/.env"
        if [[ -f "$env_file" ]]; then
            port=$(grep -E '^SCRAPER_SERVER_PORT=' "$env_file" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'" | xargs)
        fi
    fi

    # 3. Read from configs/config.yaml server section.
    if [[ -z "$port" ]]; then
        local config_file="${project_root}/configs/config.yaml"
        if [[ -f "$config_file" ]]; then
            # Extract port from the server section (avoids matching cache/scraper ports)
            port=$(sed -n '/^server:/,/^[a-z]/{ /port:/ { s/.*port:[[:space:]]*//; p } }' "$config_file" 2>/dev/null | head -1)
        fi
    fi

    echo "${port:-8080}"
}

APP_PORT=$(_read_port)

# Sanitize port to plain digits (strips any \r, spaces, etc.)
APP_PORT=$(echo "$APP_PORT" | tr -cd '0-9')

# Prepares a patched copy of the Quadlet file with the correct port.
QUADLET_SRC="$(dirname "$0")/../remote/scraper-api.container"
QUADLET_PATCHED=""
if [[ "$APP_PORT" != "8080" ]]; then
    echo "▸ Patching Quadlet port to ${APP_PORT}..."
    QUADLET_PATCHED="$(mktemp /tmp/scraper-api.container.XXXXXX)"
    sed -e "s/PublishPort=8080:8080/PublishPort=${APP_PORT}:8080/" \
        -e "s/^Environment=SCRAPER_SERVER_PORT=8080$/Environment=SCRAPER_SERVER_PORT=${APP_PORT}/" \
        "$QUADLET_SRC" > "$QUADLET_PATCHED"
fi

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
        --image-tag)    IMAGE_TAG="$2";    shift 2 ;;
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

# Strip leading "v" from image tags for GHCR compatibility
# (the CI publishing workflow strips it: v1.0.0 → 1.0.0)
IMAGE_TAG="${IMAGE_TAG#v}"

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
echo "  App port:    $APP_PORT"
echo "  With .env:   $WITH_ENV"
echo "  Pull only:   $PULL_ONLY"
echo ""

# ------------------------------------------------------------- setup remote
echo "▸ Creating remote directories..."
remote_exec "mkdir -p ${REMOTE_DIR}/{app,configs,data,debug,logs,remote}"
# The container runs as the "scraper" user (non-root). Make data, debug
# and logs world-writable so the container can create files in them.
remote_exec "chmod 777 ${REMOTE_DIR}/{data,debug,logs}"

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
    remote_copy "${QUADLET_PATCHED:-$QUADLET_SRC}" "${REMOTE_DIR}/remote/scraper-api.container"

    remote_copy "$(dirname "$0")/../requirements.txt"         "${REMOTE_DIR}/requirements.txt"
    remote_copy "$(dirname "$0")/../Dockerfile"               "${REMOTE_DIR}/Dockerfile"
    remote_copy "$(dirname "$0")/../.dockerignore"            "${REMOTE_DIR}/.dockerignore"
else
    # GHCR mode — only copy the Quadlet file
    echo "▸ Copying Quadlet files..."
    remote_copy "${QUADLET_PATCHED:-$QUADLET_SRC}" "${REMOTE_DIR}/remote/scraper-api.container"
fi

if [[ "$WITHOUT_CONFIG" != "true" ]]; then
    echo "▸ Copying config..."
    remote_copy "$(dirname "$0")/../configs/config.yaml" "${REMOTE_DIR}/configs/config.yaml"
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

# Verify the Quadlet file is in place
echo "▸ Verifying Quadlet installation..."
remote_exec "ls -la ~/.config/containers/systemd/scraper-api.container"

# Remove any stale static service file left over from a previous
# "podman generate systemd" fallback — it takes precedence over
# the Quadlet-generated unit and would pin the old image + port mapping.
echo "▸ Cleaning up stale static service (if any)..."
remote_exec "rm -f ~/.config/systemd/user/scraper-api.service"

# Enable user linger FIRST so the user's systemd instance is active.
# Quadlet won't generate units without a running systemd --user.
echo "▸ Enabling user linger and reloading systemd..."
remote_exec "loginctl enable-linger ${REMOTE_USER} 2>/dev/null || true"
remote_exec "systemctl --user daemon-reload"

echo "▸ Checking generated service..."
SERVICE_EXISTS=false
SERVICE_STARTED=false

# daemon-reload is synchronous but give Quadlet extra time on slower hosts.
for attempt in 1 2 3; do
    sleep 2
    if remote_exec "systemctl --user list-unit-files | grep -q scraper-api.service" 2>/dev/null; then
        SERVICE_EXISTS=true
        break
    fi
done

if [[ "$SERVICE_EXISTS" == "true" ]]; then
    # Force-remove any stale container so systemd creates a fresh one
    # with the updated Quadlet configuration (image, ports, env vars).
    echo "▸ Removing stale container (if any)..."
    remote_exec "podman rm -f scraper-api 2>/dev/null || true"

    echo "▸ Starting / restarting service..."
    remote_exec "systemctl --user restart scraper-api.service" && SERVICE_STARTED=true || {
        echo "⚠ Warning: restart failed. Trying start..."
        remote_exec "systemctl --user start scraper-api.service" && SERVICE_STARTED=true || true
    }
else
    echo "⚠ Quadlet did not generate the service. Falling back to podman run..."
    echo ""

    # Ensure data dirs exist and are writable by the container's scraper user.
    remote_exec "mkdir -p ${REMOTE_DIR}/{data,debug,logs}" || true
    remote_exec "chmod 777 ${REMOTE_DIR}/{data,debug,logs}" || true

    PODMAN_RUN_CMD="podman run -d --name scraper-api --replace \
        -p ${APP_PORT}:8080 \
        -v ${REMOTE_DIR}/configs/config.yaml:/config/config.yaml:ro,z \
        -v ${REMOTE_DIR}/data:/data:z \
        -v ${REMOTE_DIR}/debug:/debug:z \
        -v ${REMOTE_DIR}/logs:/logs:z \
        --env CONFIG_PATH=/config/config.yaml \
        --env SCRAPER_CACHE_SQLITE_PATH=/data/scraper-cache.db \
        --env SCRAPER_DEBUG_DIR=/debug \
        --env-file ${REMOTE_DIR}/.env \
        --restart always \
        ${IMAGE_REF}"

    echo "▸ Starting container with podman run..."
    remote_exec "$PODMAN_RUN_CMD" && SERVICE_STARTED=true || {
        echo "⚠ podman run failed. Trying to start existing container..."
        remote_exec "podman start scraper-api" && SERVICE_STARTED=true || true
    }

    if [[ "$SERVICE_STARTED" == "true" ]]; then
        # Give the container a moment to initialize.
        sleep 3
        CONTAINER_OK=false
        if remote_exec "podman ps --filter name=scraper-api --filter status=running --format '{{.ID}}'" 2>/dev/null | grep -q .; then
            CONTAINER_OK=true
        fi

        if [[ "$CONTAINER_OK" == "true" ]]; then
            echo "▸ Generating systemd service from running container..."
            remote_exec "mkdir -p ~/.config/systemd/user"
            remote_exec "podman generate systemd --new --name scraper-api > ~/.config/systemd/user/scraper-api.service" || true
            remote_exec "systemctl --user daemon-reload"
            remote_exec "systemctl --user enable scraper-api.service" || true
            # systemctl restart starts a fresh container via the generated unit.
            remote_exec "systemctl --user restart scraper-api.service" && SERVICE_STARTED=true || true
            echo "✓ Systemd service installed (survives reboots)"
        else
            echo "✗ Container exited after podman run. Checking logs..."
            remote_exec "podman logs scraper-api 2>&1 | tail -15" || true
            SERVICE_STARTED=false
        fi
    else
        echo "✗ Fallback also failed."
    fi
fi

# ----------------------------------------------------------- health check
if [[ "$NO_HEALTHCHECK" != "true" && "$SERVICE_STARTED" == "true" ]]; then
    echo "▸ Waiting for health check..."
    sleep 5
    HEALTH_OK=false
    for i in $(seq 1 12); do
        if remote_exec "curl -s -o /dev/null -w '%{http_code}' http://localhost:${APP_PORT}/health" 2>/dev/null | grep -q 200; then
            HEALTH_OK=true
            break
        fi
        echo "   Attempt $i/12..."
        sleep 5
    done

    if [[ "$HEALTH_OK" == "true" ]]; then
        echo "✓ Health check passed!"
    else
        echo "✗ Health check failed. Debugging:"
        echo ""
        echo "  # Deployed Quadlet port config:"
        remote_exec "grep -E '(PublishPort|SCRAPER_SERVER_PORT)' ~/.config/containers/systemd/scraper-api.container" || true
        echo ""
        echo "  # Container env (running):"
        remote_exec "podman exec scraper-api env | grep -E 'SERVER_PORT|PORT|SCRAPER_' 2>/dev/null || echo 'container not running or no exec'" || true
        echo ""
        echo "  # Container logs:"
        remote_exec "podman logs --tail 10 scraper-api 2>&1 || true" || true
        echo ""
        echo "  # Service status:"
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
if [[ "$SERVICE_STARTED" == "true" ]]; then
    if [[ "$NO_HEALTHCHECK" == "true" ]] || [[ "$HEALTH_OK" == "true" ]]; then
        echo "═══ Deploy complete — container is running ═══"
    else
        echo "═══ Deploy complete — container is running BUT health check FAILED ═══"
        echo ""
        echo "The container is up but not responding on port ${APP_PORT}."
        echo "Check the debug output above. Common causes:"
        echo "  - Port mismatch: the image may be hardcoded to a different port"
        echo "  - Check: podman logs --tail 20 scraper-api"
        echo "  - Check: podman port scraper-api"
    fi
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
else
    echo "═══ Deploy incomplete — container was not started ═══"
    echo ""
    echo "Review the debug output above, or log into the Pi and check:"
    echo ""
    echo "  podman images"
    echo "  systemctl --user daemon-reload"
    echo "  systemctl --user list-unit-files | grep scraper"
    echo "  journalctl --user -u scraper-api.service 2>&1 | tail -20"
    echo "  cat ~/.config/containers/systemd/scraper-api.container"
fi
echo ""
echo "  # Test the API"
echo "  curl -s http://${REMOTE_HOST}:${APP_PORT}/health"
echo "  curl -s -H 'Authorization: Bearer \$(cat ${REMOTE_DIR}/.env | grep SCRAPER_API_KEY | cut -d= -f2)' \\"
echo "       -H 'Content-Type: application/json' \\"
echo "       -d '{\"url\":\"https://example.com\"}' \\"
echo "       http://${REMOTE_HOST}:${APP_PORT}/v1/scrape"
echo ""
