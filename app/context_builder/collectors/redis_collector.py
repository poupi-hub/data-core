"""
RedisCollector — verifica conectividade e estado do Redis.

Coleta:
  - PING (conectividade básica)
  - INFO server (versão, uptime, memória)
  - INFO stats (comandos/s, keyspace hits)
  - INFO clients (conexões ativas)
  - Memória usada vs max
  - Keyspace (DBs em uso e tamanho)
"""

from __future__ import annotations

import socket
from typing import Any
from urllib.parse import urlparse

from app.context_builder.collectors.base import BaseCollector
from core.config import settings

# Redis ports relevantes na plataforma
_REDIS_TARGETS: list[dict[str, Any]] = [
    {"name": "main",     "url": settings.redis_url},
    {"name": "crypto",   "url": "redis://localhost:6380/2"},
    {"name": "volatile", "url": "redis://localhost:6379/3"},
]


def _parse_redis_url(url: str) -> tuple[str, int, int]:
    """Retorna (host, port, db)."""
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 6379
    db = int(parsed.path.lstrip("/") or "0")
    return host, port, db


def _redis_info_lightweight(host: str, port: int, db: int, timeout: float) -> dict[str, Any]:
    """
    Implementação leve de PING + INFO usando socket puro.
    Evita dependência de redis-py no path crítico.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))

        # SELECT db
        sock.sendall(f"*2\r\n$6\r\nSELECT\r\n${len(str(db))}\r\n{db}\r\n".encode())
        sock.recv(16)  # +OK

        # PING
        sock.sendall(b"*1\r\n$4\r\nPING\r\n")
        pong = sock.recv(16).decode(errors="replace").strip()

        # INFO all (limited)
        sock.sendall(b"*1\r\n$4\r\nINFO\r\n")
        # Read until we have enough
        info_raw = b""
        while len(info_raw) < 2048:
            chunk = sock.recv(2048)
            if not chunk:
                break
            info_raw += chunk
            if b"\r\n\r\n" in info_raw:
                break

        info_text = info_raw.decode(errors="replace")
        info = {}
        for line in info_text.splitlines():
            if ":" in line and not line.startswith("#"):
                k, _, v = line.partition(":")
                info[k.strip()] = v.strip()

        return {
            "connected": True,
            "ping": "PONG" in pong,
            "redis_version": info.get("redis_version"),
            "uptime_seconds": int(info.get("uptime_in_seconds", 0)),
            "used_memory_human": info.get("used_memory_human"),
            "maxmemory_human": info.get("maxmemory_human"),
            "connected_clients": int(info.get("connected_clients", 0)),
            "total_commands_processed": int(info.get("total_commands_processed", 0)),
            "keyspace_hits": int(info.get("keyspace_hits", 0)),
            "keyspace_misses": int(info.get("keyspace_misses", 0)),
            "role": info.get("role"),
            "mem_fragmentation_ratio": info.get("mem_fragmentation_ratio"),
        }

    except (ConnectionRefusedError, socket.timeout, OSError) as exc:
        return {"connected": False, "error": str(exc)}
    finally:
        sock.close()


class RedisCollector(BaseCollector):
    name = "redis"
    timeout_seconds = 5.0

    def collect_data(self, context: dict[str, Any]) -> dict[str, Any]:
        service = context.get("service", "")
        results: dict[str, Any] = {}

        # Determinar quais Redis verificar baseado no serviço
        targets = _REDIS_TARGETS[:1]  # sempre verificar o main
        if service == "poupi-crypto":
            targets = [t for t in _REDIS_TARGETS if t["name"] in ("main", "crypto")]
        elif service == "poupi-baby":
            targets = [t for t in _REDIS_TARGETS if t["name"] == "main"]

        all_connected = True
        for target in targets:
            name = target["name"]
            host, port, db = _parse_redis_url(target["url"])
            result = _redis_info_lightweight(host, port, db, timeout=self.timeout_seconds)
            results[name] = {"host": host, "port": port, "db": db, **result}
            if not result.get("connected", False):
                all_connected = False

        return {
            "service": service,
            "all_connected": all_connected,
            "instances": results,
            "instances_checked": len(targets),
        }
