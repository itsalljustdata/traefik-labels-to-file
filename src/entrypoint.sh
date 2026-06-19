#!/bin/bash
# Entrypoint wrapper for traefik-label-generator container.
# Bootstraps runtime user/group from PUID/PGID/DOCKER_GID, then drops privileges.

set -euo pipefail

docker_sock_file="/var/run/docker.sock"
app_user="reader"
app_group="reader"

die() {
    echo "error: $*" >&2
    exit 1
}

DOCKER_ENDPOINT="${DOCKER_ENDPOINT:-${DOCKER_SOCK:-unix://$docker_sock_file}}"
OUTPUT_DIR="${OUTPUT_DIR:-/dynamic}"
UPSTREAM_HOST="${UPSTREAM_HOST:-}"
NAME_PREFIX="${NAME_PREFIX:-}"
DOCKER_SERVER_NAME="${DOCKER_SERVER_NAME:-}"
INCLUDE_DISABLED="${INCLUDE_DISABLED:-false}"
INCLUDE_STOPPED="${INCLUDE_STOPPED:-false}"
LOOP_SECONDS="${LOOP_SECONDS:-300}"
WATCH_DOCKER_EVENTS="${WATCH_DOCKER_EVENTS:-false}"
EVENT_DEBOUNCE_SECONDS="${EVENT_DEBOUNCE_SECONDS:-1.5}"
WEBHOOK_BIND="${WEBHOOK_BIND:-}"
WEBHOOK_PATH="${WEBHOOK_PATH:-/generate}"
WEBHOOK_TOKEN="${WEBHOOK_TOKEN:-}"
SKIP_INITIAL_RUN="${SKIP_INITIAL_RUN:-false}"
LOG_LEVEL="${LOG_LEVEL:-ERROR}"
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
DOCKER_GID="${DOCKER_GID:-999}"

if [ -z "$DOCKER_SERVER_NAME" ]; then
    die "DOCKER_SERVER_NAME must be set"
fi

if ! [[ "$PUID" =~ ^[0-9]+$ && "$PGID" =~ ^[0-9]+$ && "$DOCKER_GID" =~ ^[0-9]+$ ]]; then
    die "PUID, PGID, and DOCKER_GID must be numeric"
fi

if [ "$PUID" = "0" ]; then
    die "PUID=0 is not allowed; set a non-root uid"
fi

if [ "$(id -u)" != "0" ]; then
    die "entrypoint must start as root to create runtime user; remove compose user override"
fi

# Ensure primary group exists for PGID.
primary_group_name="$(getent group "$PGID" | cut -d: -f1 || true)"
if [ -z "$primary_group_name" ]; then
    if getent group "$app_group" >/dev/null 2>&1; then
        groupmod -g "$PGID" "$app_group"
    else
        groupadd -g "$PGID" "$app_group"
    fi
    primary_group_name="$app_group"
fi

# Ensure socket group exists for DOCKER_GID.
docker_group_name="$(getent group "$DOCKER_GID" | cut -d: -f1 || true)"
if [ -z "$docker_group_name" ]; then
    docker_group_name="dockerhost"
    if getent group "$docker_group_name" >/dev/null 2>&1; then
        groupmod -g "$DOCKER_GID" "$docker_group_name"
    else
        groupadd -g "$DOCKER_GID" "$docker_group_name"
    fi
fi

# Ensure runtime user exists and matches PUID/PGID.
run_user=""
uid_owner="$(getent passwd "$PUID" | cut -d: -f1 || true)"
if [ -n "$uid_owner" ]; then
    run_user="$uid_owner"
    current_gid="$(id -g "$run_user")"
    if [ "$current_gid" != "$PGID" ]; then
        usermod -g "$PGID" "$run_user"
    fi
else
    if id "$app_user" >/dev/null 2>&1; then
        current_uid="$(id -u "$app_user")"
        if [ "$current_uid" != "$PUID" ]; then
            usermod -u "$PUID" "$app_user"
        fi
        current_gid="$(id -g "$app_user")"
        if [ "$current_gid" != "$PGID" ]; then
            usermod -g "$PGID" "$app_user"
        fi
        run_user="$app_user"
    else
        # /usr/sbin/nologin
        if [ -n "${USER_SHELL:-}" ]; then
            case "$USER_SHELL" in
                /*)
                    user_shell="$USER_SHELL"
                    ;;
                *)
                    user_shell="/usr/bin/$USER_SHELL"
                    ;;
            esac
            if [ ! -f /etc/shells ] || ! grep -q "^${user_shell}$" /etc/shells; then
                die "USER_SHELL '$user_shell' is not present in /etc/shells"
            fi
        else
            user_shell="/usr/sbin/nologin"
        fi
        useradd -u "$PUID" -g "$PGID" -m -s "$user_shell" "$app_user"
        run_user="$app_user"
    fi
fi

# Ensure runtime user is in docker socket group.
if ! id -Gn "$run_user" | tr ' ' '\n' | grep -Fx "$docker_group_name" >/dev/null; then
    usermod -aG "$docker_group_name" "$run_user"
fi

mkdir -p "$OUTPUT_DIR"
chown -R "$PUID:$PGID" /app

if [ "$DOCKER_ENDPOINT" = "unix://$docker_sock_file" ]; then
    if [ ! -e "$docker_sock_file" ]; then
        die "docker socket file '$docker_sock_file' does not exist"
    fi
    if ! gosu "$run_user" test -r "$docker_sock_file"; then
        sock_gid="$(stat -c '%g' "$docker_sock_file")"
        die "cannot read $docker_sock_file as uid=$PUID gid=$PGID (socket gid=$sock_gid, configured DOCKER_GID=$DOCKER_GID)"
    fi
fi

if ! gosu "$run_user" test -w "$OUTPUT_DIR"; then
    die "output directory '$OUTPUT_DIR' is not writable as uid=$PUID gid=$PGID"
fi

# Build argument list
ARGS=("--docker-endpoint" "$DOCKER_ENDPOINT" "--output" "$OUTPUT_DIR" "--docker-server-name" "$DOCKER_SERVER_NAME")

if [ -n "$UPSTREAM_HOST" ]; then
    ARGS+=("--upstream-host" "$UPSTREAM_HOST")
fi

if [ -n "$NAME_PREFIX" ]; then
    ARGS+=("--name-prefix" "$NAME_PREFIX")
fi

if [ "$INCLUDE_DISABLED" = "true" ]; then
    ARGS+=("--include-disabled")
fi

if [ "$INCLUDE_STOPPED" = "true" ]; then
    ARGS+=("--include-stopped")
fi

if [ -n "$LOOP_SECONDS" ]; then
    ARGS+=("--loop-seconds" "$LOOP_SECONDS")
fi

if [ "$WATCH_DOCKER_EVENTS" = "true" ]; then
    ARGS+=("--watch-docker-events")
fi

if [ -n "$EVENT_DEBOUNCE_SECONDS" ]; then
    ARGS+=("--event-debounce-seconds" "$EVENT_DEBOUNCE_SECONDS")
fi

if [ -n "$WEBHOOK_BIND" ]; then
    ARGS+=("--webhook-bind" "$WEBHOOK_BIND")
fi

if [ -n "$WEBHOOK_PATH" ]; then
    ARGS+=("--webhook-path" "$WEBHOOK_PATH")
fi

if [ -n "$WEBHOOK_TOKEN" ]; then
    ARGS+=("--webhook-token" "$WEBHOOK_TOKEN")
fi

if [ "$SKIP_INITIAL_RUN" = "true" ]; then
    ARGS+=("--skip-initial-run")
fi

if [ -n "$LOG_LEVEL" ]; then
    ARGS+=("--log-level" "$LOG_LEVEL")
fi

exec gosu "$run_user" /app/docker_labels_to_dynamic.py "${ARGS[@]}"
