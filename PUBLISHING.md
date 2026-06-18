# Publishing

This repository publishes its container image to GHCR.

## Image

- ghcr.io/itsalljustdata/traefik-labels-to-file

## Automated Publish (GitHub Actions)

Workflow:
- .github/workflows/publish-ghcr.yml

Triggers:
- Push to main
- Tag push matching v*
- Manual workflow_dispatch

Behavior:
- Builds from src/Dockerfile with context src/
- Publishes multi-arch images (linux/amd64, linux/arm64)
- Publishes tags for latest (default branch), branch, tag, and sha

## Manual Publish

Prerequisites:
- Docker Buildx configured
- GitHub token with write:packages in GHCR_TOKEN

Login:

```bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u itsalljustdata --password-stdin
```

Build and push:

```bash
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -f src/Dockerfile \
  -t ghcr.io/itsalljustdata/traefik-labels-to-file:latest \
  --push src
```

## Local Build Compose

Build-oriented compose file is in src/docker-compose.yml.

```bash
docker compose -f src/docker-compose.yml up --build
```
