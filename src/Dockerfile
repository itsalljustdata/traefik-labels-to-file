FROM python:3.12-slim AS builder

RUN apt-get update \
	&& apt-get install -y --no-install-recommends gosu \
	&& rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY docker_labels_to_dynamic.py entrypoint.sh ./
RUN chmod +x docker_labels_to_dynamic.py entrypoint.sh

FROM python:3.12-slim AS runtime

ARG PUID=1000
ARG PGID=1000
ARG DOCKER_GID=999

WORKDIR /app

# Keep defaults from build args; entrypoint can override at runtime.
ENV PUID=${PUID} \
	PGID=${PGID} \
	DOCKER_GID=${DOCKER_GID}

RUN mkdir -p /dynamic

COPY --from=builder /usr/sbin/gosu /usr/sbin/gosu
COPY --from=builder /src/docker_labels_to_dynamic.py /app/
COPY --from=builder /src/entrypoint.sh /app/

RUN chmod +x /app/docker_labels_to_dynamic.py /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
