#!/usr/bin/env python3
"""Generate Traefik file-provider dynamic YAML from Docker container labels.

This script reads Docker container labels from a Docker Engine API endpoint
(unix socket or TCP), extracts Traefik HTTP routers/middlewares/services,
and writes a Traefik dynamic config file suitable for the file provider.

It is designed for cross-host scenarios where Traefik runs on host A while
containers run on host B and expose only Docker labels over a protected API.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import threading
import time
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse


TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}


class UnixHTTPConnection(HTTPConnection):
    """HTTPConnection variant that talks to a unix domain socket."""

    def __init__(self, socket_path: str, timeout: int = 10):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


@dataclass
class DockerEndpoint:
    scheme: str
    location: str


class DockerAPIClient:
    def __init__(self, endpoint: str, timeout: int = 15):
        self.endpoint = parse_endpoint(endpoint)
        self.timeout = timeout

    def _connection(self) -> HTTPConnection:
        if self.endpoint.scheme == "unix":
            return UnixHTTPConnection(self.endpoint.location, timeout=self.timeout)
        if self.endpoint.scheme == "http":
            return HTTPConnection(self.endpoint.location, timeout=self.timeout)
        if self.endpoint.scheme == "https":
            return HTTPSConnection(self.endpoint.location, timeout=self.timeout)
        raise ValueError(f"Unsupported Docker endpoint scheme: {self.endpoint.scheme}")

    def get_json(self, path: str, query: dict[str, Any] | None = None) -> Any:
        conn = self._connection()
        try:
            qp = f"?{urlencode(query)}" if query else ""
            conn.request("GET", f"{path}{qp}")
            response = conn.getresponse()
            raw = response.read().decode("utf-8", errors="replace")
            if response.status >= 400:
                raise RuntimeError(f"Docker API {response.status} {response.reason}: {raw[:500]}")
            if not raw.strip():
                return None
            return json.loads(raw)
        finally:
            conn.close()

    def list_containers(self, include_stopped: bool) -> list[dict[str, Any]]:
        payload = self.get_json("/containers/json", {"all": int(include_stopped)})
        return payload if isinstance(payload, list) else []

    def iter_events(self, filters: dict[str, Any] | None = None):
        """Yield Docker daemon events as decoded JSON objects."""
        conn = self._connection()
        try:
            query: dict[str, Any] = {}
            if filters:
                query["filters"] = json.dumps(filters)
            qp = f"?{urlencode(query)}" if query else ""
            conn.request("GET", f"/events{qp}")
            response = conn.getresponse()
            if response.status >= 400:
                raw = response.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"Docker API {response.status} {response.reason}: {raw[:500]}")

            while True:
                line = response.fp.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    yield payload
        finally:
            conn.close()


def parse_endpoint(endpoint: str) -> DockerEndpoint:
    if endpoint.startswith("unix://"):
        return DockerEndpoint(scheme="unix", location=endpoint[len("unix://") :])
    parsed = urlparse(endpoint)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return DockerEndpoint(scheme=parsed.scheme, location=parsed.netloc)
    raise ValueError(
        "Invalid endpoint. Use unix:///var/run/docker.sock, http://host:2375, or https://host:2376"
    )


def parse_bool(text: str) -> bool | None:
    value = text.strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    return None


def to_number(text: str) -> int | float | str:
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    return text


def normalize_segment(segment: str) -> str:
    mapping = {
        "entrypoints": "entryPoints",
        "redirectregex": "redirectRegex",
        "redirectscheme": "redirectScheme",
        "stripprefix": "stripPrefix",
        "stripprefixregex": "stripPrefixRegex",
        "addprefix": "addPrefix",
        "basicauth": "basicAuth",
        "forwardauth": "forwardAuth",
        "ratelimit": "rateLimit",
        "ipallowlist": "ipAllowList",
        "loadbalancer": "loadBalancer",
        "passhostheader": "passHostHeader",
        "certresolver": "certResolver",
    }
    return mapping.get(segment.lower(), segment)


def maybe_csv(key: str, value: str) -> Any:
    lower = key.lower()
    if lower in {"entrypoints", "middlewares", "sourcerange"}:
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


def parse_value(key: str, value: str) -> Any:
    csv_or_value = maybe_csv(key, value)
    if isinstance(csv_or_value, list):
        return csv_or_value
    as_bool = parse_bool(csv_or_value)
    if as_bool is not None:
        return as_bool
    return to_number(csv_or_value)


def set_nested(target: dict[str, Any], path: list[str], value: Any) -> None:
    node = target
    for i, raw in enumerate(path):
        key = normalize_segment(raw)
        last = i == len(path) - 1
        if last:
            node[key] = value
            return
        nxt = node.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            node[key] = nxt
        node = nxt


def clean_container_name(container: dict[str, Any]) -> str:
    names = container.get("Names") or []
    if not names:
        return container.get("Id", "container")[:12]
    return (names[0] or "").lstrip("/") or container.get("Id", "container")[:12]


def should_include_container(labels: dict[str, str], include_disabled: bool) -> bool:
    if include_disabled:
        return True
    enabled = labels.get("traefik.enable")
    if enabled is None:
        return False
    enabled_bool = parse_bool(enabled)
    return enabled_bool is True


def apply_redirect_labels(
    labels: dict[str, str],
    routers: dict[str, dict[str, Any]],
    middlewares: dict[str, dict[str, Any]],
) -> None:
    """Build redirectRegex middleware from opt-in labels and attach it to routers.

    Supported labels:
    - traefik.file.redirect.target (required)
    - traefik.file.redirect.regex (optional, default: .*)
    - traefik.file.redirect.permanent (optional, default: true)
    - traefik.file.redirect.middleware (optional explicit middleware name)
    """
    target = labels.get("traefik.file.redirect.target", "").strip()
    if not target:
        return

    regex = labels.get("traefik.file.redirect.regex", ".*")
    permanent_raw = labels.get("traefik.file.redirect.permanent", "true")
    parsed_permanent = parse_bool(permanent_raw)
    permanent = True if parsed_permanent is None else parsed_permanent
    explicit_middleware = labels.get("traefik.file.redirect.middleware", "").strip()

    for router_name, router_cfg in routers.items():
        middleware_name = explicit_middleware or f"{router_name}-middleware"
        if middleware_name not in middlewares:
            middlewares[middleware_name] = {
                "redirectRegex": {
                    "regex": regex,
                    "replacement": target,
                    "permanent": permanent,
                }
            }

        current = router_cfg.get("middlewares")
        if isinstance(current, str):
            current_list = [mw.strip() for mw in current.split(",") if mw.strip()]
        elif isinstance(current, list):
            current_list = [mw for mw in current if isinstance(mw, str)]
        else:
            current_list = []

        if middleware_name not in current_list:
            current_list.append(middleware_name)
        router_cfg["middlewares"] = current_list
        router_cfg["service"] = "noop@internal"


def extract_traefik_blocks(
    labels: dict[str, str],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    routers: dict[str, dict[str, Any]] = {}
    middlewares: dict[str, dict[str, Any]] = {}
    services: dict[str, dict[str, Any]] = {}

    for key, raw_value in labels.items():
        if not key.startswith("traefik.http."):
            continue

        parts = key.split(".")
        if len(parts) < 5:
            continue

        kind = parts[2]
        object_name = parts[3]
        attr_path = parts[4:]
        parsed_value = parse_value(attr_path[-1], raw_value)

        if kind == "routers":
            target = routers.setdefault(object_name, {})
            set_nested(target, attr_path, parsed_value)
            continue
        if kind == "middlewares":
            target = middlewares.setdefault(object_name, {})
            set_nested(target, attr_path, parsed_value)
            continue
        if kind == "services":
            target = services.setdefault(object_name, {})
            set_nested(target, attr_path, parsed_value)

    return routers, middlewares, services


def resolve_published_tcp_port(container: dict[str, Any], private_port: int) -> tuple[int, str] | None:
    """Resolve published port and bind address. Returns (port, host_or_ip) or None.
    
    If bound to 0.0.0.0, returns the Docker server hostname; otherwise returns the bound IP.
    """
    ports = container.get("Ports")
    if not isinstance(ports, list):
        return None
    for port_info in ports:
        if not isinstance(port_info, dict):
            continue
        if port_info.get("PrivatePort") != private_port:
            continue
        if str(port_info.get("Type", "tcp")).lower() != "tcp":
            continue
        public_port = port_info.get("PublicPort")
        if not isinstance(public_port, int):
            continue
        bind_ip = port_info.get("IP", "0.0.0.0")
        return (public_port, bind_ip)
    return None


def convert_service_port_to_url(
    services: dict[str, dict[str, Any]],
    container: dict[str, Any],
    upstream_host: str | None,
    docker_hostname: str | None,
) -> set[str]:
    """Translate Docker-style loadBalancer.server.port into file-provider servers.url.
    
    If port is bound to 0.0.0.0, uses docker_hostname; otherwise uses the bound IP.
    Returns: set of service names with unpublished ports (excluded from output).
    """
    unpublished = set()
    for service_name, service_cfg in services.items():
        lb = service_cfg.get("loadBalancer")
        if not isinstance(lb, dict):
            continue

        server = lb.get("server")
        if not isinstance(server, dict):
            continue

        if "url" in server:
            continue

        if "port" not in server:
            continue

        if not upstream_host:
            print(
                f"warning: service '{service_name}' has loadBalancer.server.port but no --upstream-host; skipping",
                file=sys.stderr,
            )
            unpublished.add(service_name)
            continue

        scheme = str(server.get("scheme", "http"))
        private_port = int(server["port"])
        port_info = resolve_published_tcp_port(container, private_port)

        if port_info is None:
            print(
                f"warning: service '{service_name}' port {private_port} is not published; skipping",
                file=sys.stderr,
            )
            unpublished.add(service_name)
            continue

        published_port, bind_ip = port_info
        if bind_ip == "0.0.0.0" and docker_hostname:
            effective_host = docker_hostname
        else:
            effective_host = bind_ip or upstream_host

        lb.pop("server", None)
        lb["servers"] = [{"url": f"{scheme}://{effective_host}:{published_port}"}]
    
    return unpublished


def merge_with_collision_handling(
    aggregate: dict[str, dict[str, Any]],
    incoming: dict[str, dict[str, Any]],
    container_name: str,
    object_kind: str,
    prefix: str | None,
) -> dict[str, str]:
    """Merge namespaced objects and return old->new name map."""
    remap: dict[str, str] = {}
    for base_name, cfg in incoming.items():
        candidate = f"{prefix}{base_name}" if prefix else base_name
        final_name = candidate
        if final_name in aggregate:
            final_name = f"{candidate}__{container_name.replace('.', '_').replace('-', '_')}"
            print(
                f"warning: {object_kind} name collision for '{candidate}', using '{final_name}'",
                file=sys.stderr,
            )
        aggregate[final_name] = cfg
        remap[base_name] = final_name
    return remap


def remap_router_refs(
    routers: dict[str, dict[str, Any]],
    middleware_name_map: dict[str, str],
    service_name_map: dict[str, str],
) -> None:
    available_services = set(service_name_map.values())
    for router_name, router_cfg in routers.items():
        middlewares = router_cfg.get("middlewares")
        if isinstance(middlewares, str):
            middlewares = [mw.strip() for mw in middlewares.split(",") if mw.strip()]
            router_cfg["middlewares"] = middlewares
        if isinstance(middlewares, list):
            router_cfg["middlewares"] = [middleware_name_map.get(mw, mw) for mw in middlewares]

        service = router_cfg.get("service")
        if isinstance(service, str) and "@" not in service:
            router_cfg["service"] = service_name_map.get(service, service)
        elif not service:
            inferred = None
            inferred = service_name_map.get(router_name)
            if inferred is None and len(available_services) == 1:
                inferred = next(iter(available_services))
            if inferred:
                router_cfg["service"] = inferred

        if "service" not in router_cfg:
            router_cfg["service"] = "noop@internal"


def sorted_dict(source: dict[str, Any]) -> dict[str, Any]:
    return {key: source[key] for key in sorted(source.keys())}


def yaml_quote(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if value is None:
        return "null"
    text = str(value)
    safe = re.fullmatch(r"[A-Za-z0-9._/@:-]+", text)
    return text if safe else yaml_quote(text)


def dump_yaml(value: Any, indent: int = 0) -> list[str]:
    prefix = " " * indent
    lines: list[str] = []

    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                lines.append(f"{prefix}{key}:")
                lines.extend(dump_yaml(item, indent + 2))
            else:
                lines.append(f"{prefix}{key}: {yaml_scalar(item)}")
        return lines

    if isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                lines.append(f"{prefix}-")
                lines.extend(dump_yaml(item, indent + 2))
            else:
                lines.append(f"{prefix}- {yaml_scalar(item)}")
        return lines

    lines.append(f"{prefix}{yaml_scalar(value)}")
    return lines


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def sanitize_filename(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", text.strip())
    return cleaned or "unknown"


def infer_docker_server_name(endpoint: str) -> str:
    if endpoint.startswith("unix://"):
        return socket.gethostname()
    parsed = urlparse(endpoint)
    if parsed.hostname:
        return parsed.hostname
    return "docker-host"


def _deprovider(name: str) -> str:
    return name.split("@", 1)[0]


def build_single_router_config(config: dict[str, Any], router_name: str) -> dict[str, Any]:
    http = config.get("http", {})
    routers = http.get("routers", {})
    middlewares = http.get("middlewares", {})
    services = http.get("services", {})

    router_cfg = routers.get(router_name)
    if not isinstance(router_cfg, dict):
        raise ValueError(f"Router '{router_name}' not found in generated config")

    out_router = {router_name: router_cfg}
    out_middlewares: dict[str, Any] = {}
    out_services: dict[str, Any] = {}

    router_mws = router_cfg.get("middlewares", [])
    if isinstance(router_mws, str):
        router_mws = [mw.strip() for mw in router_mws.split(",") if mw.strip()]
    if isinstance(router_mws, list):
        for mw in router_mws:
            if isinstance(mw, str):
                mw_name = _deprovider(mw)
                if mw_name in middlewares:
                    out_middlewares[mw_name] = middlewares[mw_name]

    router_service = router_cfg.get("service")
    if isinstance(router_service, str) and "@" not in router_service:
        svc_name = _deprovider(router_service)
        if svc_name in services:
            out_services[svc_name] = services[svc_name]

    out_http: dict[str, Any] = {"routers": out_router}
    if out_middlewares:
        out_http["middlewares"] = out_middlewares
    if out_services:
        out_http["services"] = out_services
    return {"http": out_http}


def write_split_per_router(
    config: dict[str, Any],
    output_dir: Path,
    docker_server_name: str,
    dry_run: bool,
) -> int:
    routers = config.get("http", {}).get("routers", {})
    if not isinstance(routers, dict) or not routers:
        if dry_run:
            print("no routers found")
            return 0
        output_dir.mkdir(parents=True, exist_ok=True)
        print("wrote 0 router files")
        return 0

    written = 0
    for router_name in sorted(routers.keys()):
        if router_name.startswith(f"{docker_server_name}"):
            file_name = f"{docker_server_name}.{router_name[len(docker_server_name) + 1 :]}"  # strip server name prefix if present
        else:
            file_name = router_name
        file_name = f"{sanitize_filename(file_name)}.yml"
        per_router = build_single_router_config(config, router_name)
        yaml_text = "\n".join(dump_yaml(per_router)) + "\n"

        if dry_run:
            print(f"# {file_name}")
            print(yaml_text)
            continue

        output_path = output_dir / file_name
        atomic_write(output_path, yaml_text)
        written += 1

    if not dry_run:
        print(f"wrote {written} router file(s) to {output_dir}")
    return 0


def build_dynamic_config(
    containers: list[dict[str, Any]],
    include_disabled: bool,
    upstream_host: str | None,
    name_prefix: str | None,
    docker_hostname: str | None = None,
) -> dict[str, Any]:
    all_routers: dict[str, dict[str, Any]] = {}
    all_middlewares: dict[str, dict[str, Any]] = {}
    all_services: dict[str, dict[str, Any]] = {}
    all_unpublished_services: set[str] = set()

    for container in containers:
        labels = container.get("Labels") or {}
        if not isinstance(labels, dict):
            continue
        if not should_include_container(labels, include_disabled):
            continue

        container_name = clean_container_name(container)
        routers, middlewares, services = extract_traefik_blocks(labels)
        if not routers and not middlewares and not services:
            continue

        apply_redirect_labels(labels, routers, middlewares)

        unpublished = convert_service_port_to_url(services, container=container, upstream_host=upstream_host, docker_hostname=docker_hostname)
        all_unpublished_services.update(unpublished)
        for unpub in unpublished:
            services.pop(unpub, None)

        middleware_map = merge_with_collision_handling(
            all_middlewares,
            middlewares,
            container_name=container_name,
            object_kind="middleware",
            prefix=name_prefix,
        )
        service_map = merge_with_collision_handling(
            all_services,
            services,
            container_name=container_name,
            object_kind="service",
            prefix=name_prefix,
        )
        remap_router_refs(routers, middleware_name_map=middleware_map, service_name_map=service_map)
        merge_with_collision_handling(
            all_routers,
            routers,
            container_name=container_name,
            object_kind="router",
            prefix=name_prefix,
        )

    # Filter out routers that reference unpublished services or only have noop@internal
    filtered_routers = {}
    for router_name, router_cfg in all_routers.items():
        service_ref = router_cfg.get("service", "")
        if isinstance(service_ref, str):
            # Skip routers with only noop@internal (redirect-only, no backend)
            if service_ref == "noop@internal":
                continue
            # Skip routers referencing unpublished services
            svc_name = service_ref.split("@", 1)[0]
            if svc_name in all_unpublished_services:
                continue
        filtered_routers[router_name] = router_cfg

    http_block: dict[str, Any] = {}
    if filtered_routers:
        http_block["routers"] = sorted_dict(filtered_routers)
    if all_middlewares:
        http_block["middlewares"] = sorted_dict(all_middlewares)
    if all_services:
        http_block["services"] = sorted_dict(all_services)

    return {"http": http_block}


def cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate Traefik dynamic YAML from Docker labels.",
    )
    parser.add_argument(
        "--docker-endpoint",
        default=os.getenv("DOCKER_HOST", "unix:///var/run/docker.sock"),
        help="Docker API endpoint (unix:///var/run/docker.sock, http://host:2375, https://host:2376)",
    )
    parser.add_argument(
        "--output",
        default="dynamic.generated.yml",
        help="Output YAML file path, or output directory when --split-per-router is set.",
    )
    parser.add_argument(
        "--no-split-per-router",
        action="store_false",
        dest="split_per_router",
        help="Write one file per router with filename routerName.hostName.yml.",
    )
    parser.add_argument(
        "--docker-server-name",
        default="",
        help="Name used as {hostName} in split filenames; defaults to endpoint host or local hostname.",
    )
    parser.add_argument(
        "--include-stopped",
        action="store_true",
        help="Include stopped containers when reading labels.",
    )
    parser.add_argument(
        "--include-disabled",
        action="store_true",
        help="Include containers even if traefik.enable is missing or false.",
    )
    parser.add_argument(
        "--upstream-host",
        default=None,
        help="Host/IP used to translate loadBalancer.server.port into servers.url; defaults to docker server name.",
    )
    parser.add_argument(
        "--name-prefix",
        default="",
        help="Prefix added to generated router/middleware/service names (default: <docker-host>-).",
    )
    parser.add_argument(
        "--container-name-regex",
        default="",
        help="Only include containers whose name matches this regex.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print YAML to stdout instead of writing a file.",
    )
    parser.add_argument(
        "--loop-seconds",
        type=int,
        default=0,
        help="Run continuously; after each execution sleep this many seconds before the next run.",
    )
    parser.add_argument(
        "--watch-docker-events",
        action="store_true",
        help="Trigger regeneration when Docker emits container events.",
    )
    parser.add_argument(
        "--event-debounce-seconds",
        type=float,
        default=1.5,
        help="Coalesce bursty triggers by waiting this many seconds before regenerating.",
    )
    parser.add_argument(
        "--webhook-bind",
        default="",
        help="Optional webhook listener bind as host:port (example: 0.0.0.0:8080).",
    )
    parser.add_argument(
        "--webhook-path",
        default="/generate",
        help="Webhook path that triggers regeneration (POST only).",
    )
    parser.add_argument(
        "--webhook-token",
        default="",
        help="Optional shared token expected in X-Webhook-Token header.",
    )
    parser.add_argument(
        "--skip-initial-run",
        action="store_true",
        help="In daemon mode, do not generate immediately on startup.",
    )
    return parser.parse_args()


def filter_by_container_regex(containers: list[dict[str, Any]], pattern: str) -> list[dict[str, Any]]:
    if not pattern:
        return containers
    regex = re.compile(pattern)
    result: list[dict[str, Any]] = []
    for container in containers:
        name = clean_container_name(container)
        if regex.search(name):
            result.append(container)
    return result


def generate_once(args: argparse.Namespace) -> int:
    client = DockerAPIClient(endpoint=args.docker_endpoint)
    containers = client.list_containers(include_stopped=args.include_stopped)
    containers = filter_by_container_regex(containers, args.container_name_regex)
    docker_server_name = args.docker_server_name or infer_docker_server_name(args.docker_endpoint)
    upstream_host = args.upstream_host or docker_server_name
    effective_name_prefix = args.name_prefix if args.name_prefix else f"{sanitize_filename(docker_server_name)}-"
    config = build_dynamic_config(
        containers,
        include_disabled=args.include_disabled,
        upstream_host=upstream_host,
        name_prefix=effective_name_prefix,
        docker_hostname=docker_server_name,
    )

    output = Path(args.output)
    if (not args.dry_run) and output.exists():
        if output.is_file():
            output.unlink()
        elif output.is_dir():
            existing = output.glob(f"{docker_server_name}*.yml")
            for file in existing:
                file.unlink()

    if (not args.dry_run) and args.split_per_router:
        return write_split_per_router(
            config,
            output_dir=output,
            docker_server_name=docker_server_name,
            dry_run=args.dry_run,
        )

    yaml_text = "\n".join(dump_yaml(config)) + "\n"
    if args.dry_run:
        print(yaml_text)
        return 0

    atomic_write(output, yaml_text)
    print(f"wrote {output} with {len(config.get('http', {}).get('routers', {}))} router(s)")
    return 0


def parse_host_port(bind: str) -> tuple[str, int]:
    value = bind.strip()
    if not value or ":" not in value:
        raise ValueError("--webhook-bind must be in host:port format")
    host, port_text = value.rsplit(":", 1)
    if not host:
        host = "0.0.0.0"
    port = int(port_text)
    if port < 1 or port > 65535:
        raise ValueError("--webhook-bind port must be between 1 and 65535")
    return host, port


def start_webhook_listener(args: argparse.Namespace, trigger: threading.Event) -> ThreadingHTTPServer | None:
    if not args.webhook_bind:
        return None

    host, port = parse_host_port(args.webhook_bind)
    webhook_path = args.webhook_path or "/generate"
    webhook_token = args.webhook_token

    class WebhookHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/healthz":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"ok\n")
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            if self.path != webhook_path:
                self.send_response(404)
                self.end_headers()
                return
            if webhook_token:
                token = self.headers.get("X-Webhook-Token", "")
                if token != webhook_token:
                    self.send_response(403)
                    self.end_headers()
                    return
            trigger.set()
            self.send_response(202)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"queued\n")

        def log_message(self, format: str, *args):
            return

    server = ThreadingHTTPServer((host, port), WebhookHandler)
    thread = threading.Thread(target=server.serve_forever, name="webhook-listener", daemon=True)
    thread.start()
    print(f"webhook listener active on {host}:{port}{webhook_path}")
    return server


def start_docker_event_watcher(args: argparse.Namespace, trigger: threading.Event, stop: threading.Event) -> threading.Thread | None:
    if not args.watch_docker_events:
        return None

    def watch() -> None:
        while not stop.is_set():
            try:
                client = DockerAPIClient(endpoint=args.docker_endpoint, timeout=60)
                for event in client.iter_events(filters={"type": ["container"]}):
                    if stop.is_set():
                        break
                    action = str(event.get("Action") or event.get("status") or "").lower()
                    if not action:
                        continue
                    trigger.set()
            except Exception as exc:
                if stop.is_set():
                    break
                print(f"warning: event watcher reconnecting after error: {exc}", file=sys.stderr)
                time.sleep(2)

    thread = threading.Thread(target=watch, name="docker-event-watcher", daemon=True)
    thread.start()
    print("docker event watcher active")
    return thread


def run_daemon_mode(args: argparse.Namespace) -> int:
    trigger = threading.Event()
    stop = threading.Event()
    webhook_server: ThreadingHTTPServer | None = None
    _watch_thread: threading.Thread | None = None

    try:
        if not args.skip_initial_run:
            rc = generate_once(args)
            if rc != 0:
                return rc

        webhook_server = start_webhook_listener(args, trigger)
        _watch_thread = start_docker_event_watcher(args, trigger, stop)

        while True:
            timeout = args.loop_seconds if args.loop_seconds > 0 else None
            fired = trigger.wait(timeout=timeout)
            if fired:
                trigger.clear()
                if args.event_debounce_seconds > 0:
                    time.sleep(args.event_debounce_seconds)
                while trigger.is_set():
                    trigger.clear()
            rc = generate_once(args)
            if rc != 0:
                return rc
    except KeyboardInterrupt:
        return 0
    finally:
        stop.set()
        if webhook_server is not None:
            webhook_server.shutdown()
            webhook_server.server_close()


def main() -> int:
    args = cli_args()
    try:
        daemon_mode = bool(args.watch_docker_events or args.webhook_bind or args.loop_seconds > 0)
        if daemon_mode:
            return run_daemon_mode(args)
        return generate_once(args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
