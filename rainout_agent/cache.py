"""Small key/value cache with TTLs for weather lookups.

Default: in-memory, per serverless instance. Optional shared backends, picked
with ``RAINOUT_CACHE_BACKEND`` (``auto`` by default):

* ``upstash`` - Upstash Redis / Vercel KV over the REST API
  (``UPSTASH_REDIS_REST_URL`` + ``UPSTASH_REDIS_REST_TOKEN``, or
  ``KV_REST_API_URL`` + ``KV_REST_API_TOKEN``).
* ``supabase`` - a Supabase Postgres table through PostgREST
  (``SUPABASE_URL`` + ``SUPABASE_SERVICE_ROLE_KEY``; table from
  ``RAINOUT_CACHE_TABLE``, default ``rainout_cache``; SQL in docs/caching.md).
* ``file`` - JSON files in ``RAINOUT_CACHE_DIR`` behind memory (the box CLI).
* ``memory`` - in-memory only.  ``none`` - disable caching.

``auto`` uses Upstash/KV when its env vars exist, else Supabase when its env vars
exist, else memory. Shared backends always sit behind the in-memory tier, use
short timeouts, and on any error are skipped for a cool-down period, so a
broken or missing backend never breaks a request.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Callable

log = logging.getLogger("rainout.cache")

REMOTE_TIMEOUT_S = 1.5
REMOTE_COOLDOWN_S = 60.0
KEY_PREFIX = "rainout:v1:"


class Cache:
    name = "none"

    def get(self, key: str) -> Any | None:
        return None

    def set(self, key: str, value: Any, ttl: float) -> None:
        return None

    def describe(self) -> dict[str, Any]:
        return {"backend": self.name}


class NullCache(Cache):
    name = "none"


class MemoryCache(Cache):
    """Thread-safe TTL cache with LRU eviction, local to one instance."""

    name = "memory"

    def __init__(self, max_entries: int = 512, clock: Callable[[], float] = time.time):
        self.max_entries = max_entries
        self.clock = clock
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            expires, value = item
            if expires <= self.clock():
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return value

    def set(self, key: str, value: Any, ttl: float) -> None:
        if ttl <= 0:
            return
        with self._lock:
            self._data[key] = (self.clock() + ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


class FileCache(Cache):
    """JSON files in one directory (for the box CLI, so every agent shares one cache).

    Writes are atomic (temp file + rename); unreadable or expired files count as misses.
    """

    name = "file"

    def __init__(self, directory: str, clock: Callable[[], float] = time.time):
        self.directory = directory
        self.clock = clock
        os.makedirs(directory, exist_ok=True)

    def _path(self, key: str) -> str:
        import hashlib

        return os.path.join(self.directory, hashlib.sha1(key.encode()).hexdigest() + ".json")

    def get(self, key: str) -> Any | None:
        try:
            with open(self._path(key), encoding="utf-8") as fh:
                item = json.load(fh)
        except (OSError, ValueError):
            return None
        if item.get("key") != key or float(item.get("expires", 0)) <= self.clock():
            return None
        return item.get("value")

    def set(self, key: str, value: Any, ttl: float) -> None:
        if ttl <= 0:
            return
        path = self._path(key)
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"key": key, "expires": self.clock() + ttl, "value": value}, fh)
            os.replace(tmp, path)
        except (OSError, TypeError, ValueError):
            try:
                os.remove(tmp)
            except OSError:
                pass

    def describe(self) -> dict[str, Any]:
        return {"backend": self.name, "directory": self.directory}


Transport = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, bytes]]


def _urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


class _RemoteCache(Cache):
    """Shared behaviour: JSON values, short timeouts and a failure cool-down."""

    def __init__(self, transport: Transport | None = None, clock: Callable[[], float] = time.time):
        self.transport = transport or _urllib_transport
        self.clock = clock
        self._disabled_until = 0.0
        self.errors = 0

    def _available(self) -> bool:
        return self.clock() >= self._disabled_until

    def _fail(self, op: str, exc: Exception) -> None:
        self.errors += 1
        self._disabled_until = self.clock() + REMOTE_COOLDOWN_S
        log.warning("cache backend %s %s failed (%s); skipping it for %ss", self.name, op, exc.__class__.__name__, REMOTE_COOLDOWN_S)

    def describe(self) -> dict[str, Any]:
        return {"backend": self.name, "available": self._available(), "errors": self.errors}


class UpstashCache(_RemoteCache):
    """Upstash Redis / Vercel KV via the REST API (single-command JSON bodies)."""

    name = "upstash"

    def __init__(self, url: str, token: str, **kwargs):
        super().__init__(**kwargs)
        self.url = url.rstrip("/")
        self.token = token

    def _command(self, *args: Any) -> Any:
        body = json.dumps([str(a) for a in args]).encode("utf-8")
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        status, raw = self.transport("POST", self.url, headers, body, REMOTE_TIMEOUT_S)
        if status != 200:
            raise RuntimeError(f"HTTP {status}")
        payload = json.loads(raw.decode("utf-8"))
        if isinstance(payload, dict) and payload.get("error"):
            raise RuntimeError(str(payload["error"]))
        return payload.get("result") if isinstance(payload, dict) else None

    def get(self, key: str) -> Any | None:
        if not self._available():
            return None
        try:
            result = self._command("GET", KEY_PREFIX + key)
            return None if result is None else json.loads(result)
        except Exception as exc:
            self._fail("get", exc)
            return None

    def set(self, key: str, value: Any, ttl: float) -> None:
        if not self._available() or ttl <= 0:
            return
        try:
            self._command("SET", KEY_PREFIX + key, json.dumps(value), "EX", max(1, int(ttl)))
        except Exception as exc:
            self._fail("set", exc)


class SupabaseCache(_RemoteCache):
    """Supabase Postgres table via PostgREST: key text primary key, value jsonb, expires_at timestamptz."""

    name = "supabase"

    def __init__(self, url: str, key: str, table: str = "rainout_cache", **kwargs):
        super().__init__(**kwargs)
        self.base = url.rstrip("/") + "/rest/v1/" + urllib.parse.quote(table)
        self.key = key

    def _headers(self, **extra: str) -> dict[str, str]:
        return {"apikey": self.key, "Authorization": f"Bearer {self.key}", "Content-Type": "application/json", **extra}

    def get(self, key: str) -> Any | None:
        if not self._available():
            return None
        now = datetime.fromtimestamp(self.clock(), timezone.utc).isoformat()
        query = urllib.parse.urlencode({"key": f"eq.{KEY_PREFIX}{key}", "expires_at": f"gt.{now}", "select": "value", "limit": "1"})
        try:
            status, raw = self.transport("GET", f"{self.base}?{query}", self._headers(), None, REMOTE_TIMEOUT_S)
            if status != 200:
                raise RuntimeError(f"HTTP {status}")
            rows = json.loads(raw.decode("utf-8"))
            return rows[0]["value"] if rows else None
        except Exception as exc:
            self._fail("get", exc)
            return None

    def set(self, key: str, value: Any, ttl: float) -> None:
        if not self._available() or ttl <= 0:
            return
        expires = datetime.fromtimestamp(self.clock() + ttl, timezone.utc).isoformat()
        body = json.dumps({"key": KEY_PREFIX + key, "value": value, "expires_at": expires}).encode("utf-8")
        try:
            status, _ = self.transport(
                "POST", f"{self.base}?on_conflict=key", self._headers(Prefer="resolution=merge-duplicates,return=minimal"), body, REMOTE_TIMEOUT_S
            )
            if status not in (200, 201, 204):
                raise RuntimeError(f"HTTP {status}")
        except Exception as exc:
            self._fail("set", exc)


class TieredCache(Cache):
    """In-memory first, then a shared backend; remote hits warm the memory tier."""

    def __init__(self, memory: MemoryCache, remote: Cache, memory_ttl_cap: float = 600.0):
        self.memory = memory
        self.remote = remote
        self.memory_ttl_cap = memory_ttl_cap
        self.name = f"memory+{remote.name}"

    def get(self, key: str) -> Any | None:
        value = self.memory.get(key)
        if value is not None:
            return value
        value = self.remote.get(key)
        if value is not None:
            self.memory.set(key, value, min(self.memory_ttl_cap, 60.0))
        return value

    def set(self, key: str, value: Any, ttl: float) -> None:
        self.memory.set(key, value, min(ttl, self.memory_ttl_cap))
        self.remote.set(key, value, ttl)

    def describe(self) -> dict[str, Any]:
        return {"backend": self.name, "remote": self.remote.describe()}


def build_cache(env: dict[str, str] | None = None, transport: Transport | None = None) -> Cache:
    """Pick a cache backend from environment variables (see module docstring)."""
    env = dict(os.environ if env is None else env)
    choice = (env.get("RAINOUT_CACHE_BACKEND") or "auto").strip().lower()
    upstash_url = env.get("UPSTASH_REDIS_REST_URL") or env.get("KV_REST_API_URL")
    upstash_token = env.get("UPSTASH_REDIS_REST_TOKEN") or env.get("KV_REST_API_TOKEN")
    supa_url = env.get("SUPABASE_URL")
    supa_key = env.get("SUPABASE_SERVICE_ROLE_KEY") or env.get("SUPABASE_KEY")
    table = env.get("RAINOUT_CACHE_TABLE") or "rainout_cache"

    if choice == "none":
        return NullCache()
    memory = MemoryCache()
    if choice == "memory":
        return memory
    if choice == "file":
        return TieredCache(memory, FileCache(env.get("RAINOUT_CACHE_DIR") or "/tmp/rainout-cache"))
    if choice in {"auto", "upstash", "kv", "vercel-kv"} and upstash_url and upstash_token:
        return TieredCache(memory, UpstashCache(upstash_url, upstash_token, transport=transport))
    if choice in {"auto", "supabase"} and supa_url and supa_key:
        return TieredCache(memory, SupabaseCache(supa_url, supa_key, table, transport=transport))
    if choice not in {"auto", "memory"}:
        log.warning("cache backend %r requested but its env vars are missing or unknown; using memory", choice)
    return memory


_CACHE: Cache | None = None
_CACHE_LOCK = threading.Lock()


def get_cache() -> Cache:
    global _CACHE
    with _CACHE_LOCK:
        if _CACHE is None:
            _CACHE = build_cache()
        return _CACHE


def set_cache(cache: Cache | None) -> None:
    """Replace the process cache (tests; None re-reads the environment)."""
    global _CACHE
    with _CACHE_LOCK:
        _CACHE = cache
