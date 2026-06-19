# traefik-labels-to-file

Runtime usage for the published image.

This container generates Traefik file-provider dynamic YAML from Docker container labels.

## Image

- ghcr.io/itsalljustdata/traefik-labels-to-file:latest

## Quick Start

Create a .env file in the repository root:

```bash
DOCKER_SERVER_NAME=your-docker-hostname
PUID=1000
PGID=1000
DOCKER_GID=998
USER_SHELL=bash
LOG_LEVEL=ERROR
```

Find host docker.sock gid:

```bash
stat -c '%g' /var/run/docker.sock
```

Run with runtime compose (root file):

```bash
docker compose up -d
docker compose logs -f traefik-generator
```

## Runtime Compose

Runtime compose is at:

- compose.yaml

```yaml
services:
  traefik-generator:
    image: ghcr.io/itsalljustdata/traefik-labels-to-file:latest
    pull_policy: always
    container_name: traefik-generator
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - ./dynamic:/dynamic
    environment:
      DOCKER_SERVER_NAME: ${DOCKER_SERVER_NAME:?set DOCKER_SERVER_NAME in .env or shell}
      PUID: ${PUID:-1000}
      PGID: ${PGID:-1000}
      DOCKER_GID: ${DOCKER_GID:-999}
      USER_SHELL: ${USER_SHELL:-bash}
      LOG_LEVEL: ${LOG_LEVEL:-ERROR}
      LOOP_SECONDS: ${LOOP_SECONDS:-300}
      WATCH_DOCKER_EVENTS: ${WATCH_DOCKER_EVENTS:-false}
      EVENT_DEBOUNCE_SECONDS: ${EVENT_DEBOUNCE_SECONDS:-1.5}
      # DOCKER_ENDPOINT: https://remote-docker-host:2376
      # UPSTREAM_HOST: remote-docker-host
      # WEBHOOK_BIND: 0.0.0.0:8080
      # WEBHOOK_PATH: /generate
      # WEBHOOK_TOKEN: change-me
    restart: on-failure:3
    # Uncomment if using WEBHOOK_BIND
    # ports:
    #   - "8080:8080"

    # Keep isolated by default. Disable this if you want webhook-trigger mode.
    network_mode: none
```


This root compose file:
- pulls the GHCR image
- mounts /var/run/docker.sock and ./dynamic
- maps runtime identity with PUID/PGID/DOCKER_GID
- runs on LOOP_SECONDS interval by default

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| DOCKER_SERVER_NAME | required | Host token used in generated names/files |
| PUID | 1000 | Runtime UID used for the created user |
| PGID | 1000 | Runtime GID used for the created user |
| DOCKER_GID | 999 | Host docker.sock group gid |
| USER_SHELL | bash | User shell for created runtime user; if not absolute, /usr/bin/ is prefixed and it must exist in /etc/shells |
| LOG_LEVEL | ERROR | Logging verbosity: DEBUG, INFO, WARNING, ERROR, CRITICAL |
| LOOP_SECONDS | 300 | Sleep interval between runs |
| WATCH_DOCKER_EVENTS | false | Enable event-triggered runs |
| EVENT_DEBOUNCE_SECONDS | 1.5 | Debounce delay for event/webhook bursts |
| DOCKER_ENDPOINT | unix:///var/run/docker.sock | Docker API endpoint override |
| UPSTREAM_HOST | auto | Override host for generated service URLs |
| NAME_PREFIX | auto | Prefix for generated object names |
| OUTPUT_DIR | /dynamic | Output directory in container |
| INCLUDE_DISABLED | false | Include containers missing traefik.enable=true |
| INCLUDE_STOPPED | false | Include stopped containers |
| WEBHOOK_BIND | unset | Enable webhook listener host:port |
| WEBHOOK_PATH | /generate | Webhook path |
| WEBHOOK_TOKEN | unset | Optional X-Webhook-Token shared secret |
| SKIP_INITIAL_RUN | false | Skip first generation in daemon mode |

## Webhook Mode

Default compose sets network_mode: none. To use webhook mode:

1. Remove or override network_mode.
2. Expose a port.
3. Set WEBHOOK_BIND and optional WEBHOOK_TOKEN.

## Output

Generated files are written to:

- ./dynamic

## Repository Layout

- docker-compose.yml: runtime image usage
- src/: build sources (Dockerfile, script, entrypoint, build compose)
- PUBLISHING.md: GHCR publishing docs
