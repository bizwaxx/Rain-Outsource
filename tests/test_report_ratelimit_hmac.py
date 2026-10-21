import hashlib
import hmac
from datetime import datetime, timezone

import pytest

from rainout_agent import metrics, reports

IP = "203.0.113.9"
T = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc).timestamp()  # 10:00 CT


def test_key_is_keyed_hmac_not_plain_sha256(monkeypatch):
    monkeypatch.setenv("RAINOUT_METRICS_SALT", "test-salt-a")
    key = reports.client_key(IP, T)
    assert key != hashlib.sha256(IP.encode()).hexdigest()[:16]
    assert key != hashlib.sha256(IP.encode()).hexdigest()[:24]
    expected = hmac.new(metrics._salt("2026-10-21"), b"reports-rl:" + IP.encode(), hashlib.sha256).hexdigest()[:24]
    assert key == expected and len(key) == 24 and IP not in key


def test_key_depends_on_secret(monkeypatch):
    monkeypatch.setenv("RAINOUT_METRICS_SALT", "test-salt-a")
    a = reports.client_key(IP, T)
    monkeypatch.setenv("RAINOUT_METRICS_SALT", "test-salt-b")
    assert reports.client_key(IP, T) != a


def test_key_stable_within_a_day_and_rotates_daily(monkeypatch):
    monkeypatch.setenv("RAINOUT_METRICS_SALT", "test-salt-a")
    assert reports.client_key(IP, T) == reports.client_key(IP, T + 3 * 3600)
    assert reports.client_key(IP, T) != reports.client_key(IP, T + 86400)


def test_key_distinct_from_metrics_hash(monkeypatch):
    monkeypatch.setenv("RAINOUT_METRICS_SALT", "test-salt-a")
    assert reports.client_key(IP, T) != metrics.client_hash(IP, "2026-10-21")


def test_unset_salt_falls_back_without_breaking(monkeypatch):
    monkeypatch.delenv("RAINOUT_METRICS_SALT", raising=False)
    k1 = reports.client_key(IP, T)
    assert k1 == reports.client_key(IP, T) and len(k1) == 24
    assert k1 != hashlib.sha256(IP.encode()).hexdigest()[:24]


def test_rate_limit_still_enforced_with_one_hour_ttl(monkeypatch):
    monkeypatch.setenv("RAINOUT_METRICS_SALT", "test-salt-a")
    store = reports.MemoryCache(max_entries=100)
    seen = []
    real_set = store.set
    store.set = lambda k, v, ttl=None: (seen.append((k, ttl)), real_set(k, v, ttl))[1]
    key = reports.client_key(IP, T)
    for _ in range(reports.RATE_LIMIT_FIELD_PER_HOUR):
        reports._check_rate(store, key, "f1", T)
    with pytest.raises(reports.ReportError) as exc:
        reports._check_rate(store, key, "f1", T)
    assert exc.value.status == 429
    assert seen and all(ttl == 3600 for _, ttl in seen)
    assert all(IP not in k for k, _ in seen)


def test_salt_never_appears_in_keys_or_errors(monkeypatch):
    secret = "super-secret-salt-value"
    monkeypatch.setenv("RAINOUT_METRICS_SALT", secret)
    store = reports.MemoryCache(max_entries=100)
    key = reports.client_key(IP, T)
    assert secret not in key
    for _ in range(reports.RATE_LIMIT_FIELD_PER_HOUR):
        reports._check_rate(store, key, "f1", T)
    with pytest.raises(reports.ReportError) as exc:
        reports._check_rate(store, key, "f1", T)
    assert secret not in str(exc.value.payload())
