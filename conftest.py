# Lets `pytest` run from the repo root without setting PYTHONPATH.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest


@pytest.fixture(autouse=True)
def _isolated_weather_cache(monkeypatch):
    """Every test gets a fresh in-memory cache and never a remote backend."""
    from rainout_agent import cache

    for name in ("RAINOUT_CACHE_BACKEND", "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN",
                 "KV_REST_API_URL", "KV_REST_API_TOKEN", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_KEY"):
        monkeypatch.delenv(name, raising=False)
    cache.set_cache(cache.MemoryCache())
    yield
    cache.set_cache(None)


@pytest.fixture(autouse=True)
def _no_live_lightning(monkeypatch):
    """Status / go_no_go answers never call GLM or NWS alerts in tests (lightning tests set their own CHECK)."""
    from rainout_agent import lightning

    monkeypatch.setattr(lightning, "CHECK", lambda *a, **k: None)
