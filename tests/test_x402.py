import base64
import json
import time

import pytest

from rainout_agent import api_keys, premium_api, x402

PAY_TO = "0x209693Bc6afc0C5328bA36FaF03C514EF312287C"
ENV = {"X402_PAY_TO": PAY_TO}


class FakeFacilitator:
    def __init__(self, valid=True, settle_ok=True):
        self.valid, self.settle_ok, self.calls = valid, settle_ok, []

    def __call__(self, url, body):
        self.calls.append((url.rsplit("/", 1)[1], body))
        assert body["x402Version"] == 2 and body["paymentRequirements"]["network"] == "eip155:84532"
        if url.endswith("/verify"):
            return 200, ({"isValid": True, "payer": "0xabc"} if self.valid else {"isValid": False, "invalidReason": "insufficient_funds"})
        if self.settle_ok:
            return 200, {"success": True, "transaction": "0x" + "ab" * 32, "network": "eip155:84532", "payer": "0xabc"}
        return 200, {"success": False, "errorReason": "invalid_transaction_state", "transaction": "", "network": "eip155:84532"}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("X402_PAY_TO", "X402_NETWORK", "X402_PRICE_USDC", "X402_FACILITATOR_URL"):
        monkeypatch.delenv(k, raising=False)
    api_keys._LOCAL_STORE.clear()
    yield
    api_keys._LOCAL_STORE.clear()


def payment(req, now=None, nonce="11", **over):
    now = int(now or time.time())
    auth = {"from": "0x857b06519E91e3A54538791bDbb0E22373e36b66", "to": req["payTo"], "value": req["amount"],
            "validAfter": str(now - 10), "validBefore": str(now + 60), "nonce": "0x" + nonce * 32}
    auth.update(over)
    p = {"x402Version": 2, "accepted": req, "payload": {"signature": "0x" + "cd" * 65, "authorization": auth}}
    return base64.b64encode(json.dumps(p).encode()).decode()


def call(headers=None, items=None, env=ENV, fac=None, monkeypatch=None, status_fn=None):
    if fac is not None:
        monkeypatch.setattr(x402, "TRANSPORT", fac)
    body = json.dumps({"items": items or [{"field_id": "Krieg", "game_time": "2026-10-24T18:30:00-05:00"}]}).encode()
    status, h, raw = premium_api.handle_premium_request(
        "POST", "/v1/premium/batch", "", body, headers or {}, env=env,
        status_fn=status_fn or (lambda item: {"field_id": item["field_id"], "game_time_play_probability_percent": 90}))
    return status, h, json.loads(raw)


def req_from(h):
    return json.loads(base64.b64decode(h["PAYMENT-REQUIRED"]))["accepts"][0]


def test_constants_match_official_sources():
    # Base Sepolia chain id 84532 (docs.base.org); USDC on Base Sepolia (developers.circle.com)
    assert x402.NETWORK == "eip155:84532"
    assert x402.USDC_BASE_SEPOLIA == "0x036CbD53842c5426634e7929541eC2318f3dCF7e"


def test_challenge_format(monkeypatch):
    fac = FakeFacilitator()
    status, h, body = call(fac=fac, monkeypatch=monkeypatch)
    assert status == 402 and fac.calls == []
    pr = json.loads(base64.b64decode(h["PAYMENT-REQUIRED"]))
    assert pr["x402Version"] == 2 and pr["error"] == "PAYMENT-SIGNATURE header is required"
    assert pr["resource"]["url"].endswith("/v1/premium/batch")
    acc = pr["accepts"][0]
    assert acc == {"scheme": "exact", "network": "eip155:84532", "amount": "20000",
                   "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e", "payTo": PAY_TO, "maxTimeoutSeconds": 60,
                   "extra": {"name": "USDC", "version": "2", "assetTransferMethod": "eip3009"}}
    assert body["accepts"] == pr["accepts"] and "PAYMENT-REQUIRED" in h["Access-Control-Expose-Headers"]


def test_paid_call_verifies_runs_and_settles(monkeypatch):
    fac = FakeFacilitator()
    _, h, _ = call(fac=fac, monkeypatch=monkeypatch)
    status, h2, body = call({"PAYMENT-SIGNATURE": payment(req_from(h))}, fac=fac, monkeypatch=monkeypatch)
    assert status == 200 and body["plan"] == "pay_per_call" and body["ok"] == 1
    assert [c[0] for c in fac.calls] == ["verify", "settle"]
    settled = json.loads(base64.b64decode(h2["PAYMENT-RESPONSE"]))
    assert settled["success"] and settled["network"] == "eip155:84532"
    # replaying the same authorization is refused before the facilitator is asked again
    status, h3, body = call({"PAYMENT-SIGNATURE": payment(req_from(h))}, fac=fac, monkeypatch=monkeypatch)
    assert status == 402 and "nonce_used" in body["error"] and len(fac.calls) == 2


def test_invalid_payment_and_mismatches(monkeypatch):
    fac = FakeFacilitator(valid=False)
    _, h, _ = call(fac=fac, monkeypatch=monkeypatch)
    req = req_from(h)
    status, _, body = call({"PAYMENT-SIGNATURE": payment(req)}, fac=fac, monkeypatch=monkeypatch)
    assert status == 402 and "insufficient_funds" in body["error"]
    for bad in ({"value": "1"}, {"to": "0x" + "00" * 20}, {"validBefore": str(int(time.time()) - 1)}):
        status, _, body = call({"PAYMENT-SIGNATURE": payment(req, nonce="22", **bad)}, fac=fac, monkeypatch=monkeypatch)
        assert status == 402, bad
    cheap = dict(req, amount="1")
    status, _, body = call({"PAYMENT-SIGNATURE": payment(cheap, nonce="33")}, fac=fac, monkeypatch=monkeypatch)
    assert status == 402 and "requirements_mismatch" in body["error"]
    mainnet = dict(req, network="eip155:8453")
    assert call({"PAYMENT-SIGNATURE": payment(mainnet, nonce="44")}, fac=fac, monkeypatch=monkeypatch)[0] == 402
    assert call({"PAYMENT-SIGNATURE": "not-base64!"}, fac=fac, monkeypatch=monkeypatch)[0] == 400
    assert len(fac.calls) == 1  # only the first well-formed payment reached the facilitator


def test_v1_header_gets_a_helpful_challenge(monkeypatch):
    status, _, body = call({"X-PAYMENT": "abc"}, fac=FakeFacilitator(), monkeypatch=monkeypatch)
    assert status == 402 and "PAYMENT-SIGNATURE" in body["error"]


def test_no_charge_when_every_item_fails(monkeypatch):
    fac = FakeFacilitator()
    _, h, _ = call(fac=fac, monkeypatch=monkeypatch)

    def broken(item):
        raise RuntimeError("weather down")
    status, h2, body = call({"PAYMENT-SIGNATURE": payment(req_from(h))}, fac=fac, monkeypatch=monkeypatch, status_fn=broken)
    assert status == 502 and [c[0] for c in fac.calls] == ["verify"] and "PAYMENT-RESPONSE" not in h2


def test_settlement_failure_withholds_result(monkeypatch):
    fac = FakeFacilitator(settle_ok=False)
    _, h, _ = call(fac=fac, monkeypatch=monkeypatch)
    status, h2, body = call({"PAYMENT-SIGNATURE": payment(req_from(h))}, fac=fac, monkeypatch=monkeypatch)
    assert status == 402 and body["error"] == "settlement_failed" and "results" not in body
    assert json.loads(base64.b64decode(h2["PAYMENT-RESPONSE"]))["success"] is False


def test_testnet_only_and_config_guards(monkeypatch):
    status, _, body = call(env={})
    assert status == 503 and body["error"] == "x402_not_configured"
    status, _, body = call(env={**ENV, "X402_NETWORK": "eip155:8453"})
    assert status == 503 and body["error"] == "x402_network_refused"
    assert call(env={**ENV, "X402_PAY_TO": "not-an-address"})[0] == 503
    cfg = x402.config({**ENV, "X402_PRICE_USDC": "0.05"})
    assert cfg["amount"] == "50000"


def test_input_validated_before_any_payment(monkeypatch):
    fac = FakeFacilitator()
    status, _, body = call(items=[{"field_id": "x", "game_time": "t"}] * 51, fac=fac, monkeypatch=monkeypatch)
    assert status == 413 and fac.calls == []


def test_api_key_holders_skip_the_paywall(monkeypatch):
    monkeypatch.setenv("RAINOUT_API_KEY_SECRET", "z" * 40)
    key = api_keys.mint_key("league")
    status, h, body = call({"X-API-Key": key}, env={}, fac=FakeFacilitator(), monkeypatch=monkeypatch)
    assert status == 200 and body["plan"] == "league" and "PAYMENT-RESPONSE" not in h
