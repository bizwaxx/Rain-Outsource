"""x402 pay-per-call lane (TESTNET ONLY: Base Sepolia USDC, 'exact' scheme, x402 v2).

Spec: https://github.com/coinbase/x402 (specs/x402-specification-v2.md,
specs/transports-v2/http.md, specs/schemes/exact/scheme_exact_evm.md).

HTTP flow (v2 headers):
1. Request without ``PAYMENT-SIGNATURE`` -> 402 + ``PAYMENT-REQUIRED`` header
   (base64 JSON PaymentRequired; same object in the body for readability).
2. Client signs an EIP-3009 ``transferWithAuthorization`` (EIP-712) for the exact
   amount to ``payTo`` and retries with ``PAYMENT-SIGNATURE`` (base64 PaymentPayload).
3. Server checks the payload matches its requirements, calls the facilitator's
   read-only ``POST /verify``, runs the handler, then ``POST /settle`` (the facilitator
   submits transferWithAuthorization and pays gas) and returns ``PAYMENT-RESPONSE``.
   If the handler fails, nothing is settled, so the client is not charged.

Network and asset are fixed (testnet only):
* Base Sepolia, CAIP-2 ``eip155:84532`` (chain id 84532: https://docs.base.org/base-chain/quickstart/connecting-to-base)
* USDC ``0x036CbD53842c5426634e7929541eC2318f3dCF7e`` (Circle:
  https://developers.circle.com/stablecoins/usdc-contract-addresses; also x402's
  python/x402/mechanisms/evm/default_assets.py), EIP-712 domain name "USDC", version "2".

Env: ``X402_PAY_TO`` (0x... receiving address, required to enable the lane),
``X402_FACILITATOR_URL`` (default https://x402.org/facilitator, the public testnet
facilitator), ``X402_PRICE_USDC`` (default 0.02). ``X402_NETWORK``, if set, must be
eip155:84532; anything else (e.g. Base mainnet eip155:8453) disables the lane.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from rainout_agent import api_keys

X402_VERSION = 2
NETWORK = "eip155:84532"  # Base Sepolia
USDC_BASE_SEPOLIA = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
USDC_DECIMALS = 6
EIP712_DOMAIN = {"name": "USDC", "version": "2"}
DEFAULT_FACILITATOR = "https://x402.org/facilitator"
DEFAULT_PRICE = "0.02"
MAX_TIMEOUT_S = 60
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
NONCE_TTL_S = 24 * 3600
REQ_HEADER, PAY_HEADER, RESP_HEADER = "PAYMENT-REQUIRED", "PAYMENT-SIGNATURE", "PAYMENT-RESPONSE"

Transport = Callable[[str, dict[str, Any]], tuple[int, dict[str, Any]]]


class X402Error(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _env(env: dict[str, str] | None) -> dict[str, str]:
    return dict(os.environ) if env is None else env


def config(env: dict[str, str] | None = None) -> dict[str, Any]:
    e = _env(env)
    network = e.get("X402_NETWORK") or NETWORK
    if network != NETWORK:
        raise X402Error(503, "x402_network_refused", f"Only Base Sepolia ({NETWORK}) is allowed in this build; got {network}.")
    pay_to = (e.get("X402_PAY_TO") or "").strip()
    if not ADDRESS_RE.match(pay_to):
        raise X402Error(503, "x402_not_configured", "Pay-per-call is not set up on this deployment (X402_PAY_TO missing).")
    try:
        price = Decimal(e.get("X402_PRICE_USDC") or DEFAULT_PRICE)
    except InvalidOperation as exc:
        raise X402Error(503, "x402_not_configured", "X402_PRICE_USDC is not a number.") from exc
    if not (Decimal("0.001") <= price <= Decimal("5")):
        raise X402Error(503, "x402_not_configured", "X402_PRICE_USDC must be between 0.001 and 5.")
    return {"pay_to": pay_to, "facilitator": (e.get("X402_FACILITATOR_URL") or DEFAULT_FACILITATOR).rstrip("/"),
            "amount": str(int(price * 10 ** USDC_DECIMALS)), "price_usdc": str(price)}


def requirements(cfg: dict[str, Any]) -> dict[str, Any]:
    return {"scheme": "exact", "network": NETWORK, "amount": cfg["amount"], "asset": USDC_BASE_SEPOLIA,
            "payTo": cfg["pay_to"], "maxTimeoutSeconds": MAX_TIMEOUT_S,
            "extra": {**EIP712_DOMAIN, "assetTransferMethod": "eip3009"}}


def payment_required(cfg: dict[str, Any], resource_url: str, description: str, error: str) -> dict[str, Any]:
    return {"x402Version": X402_VERSION, "error": error,
            "resource": {"url": resource_url, "description": description, "mimeType": "application/json",
                         "serviceName": "Rainout Source", "tags": ["weather", "sports", "rainout"]},
            "accepts": [requirements(cfg)]}


def b64json(obj: dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode()


def decode_header(value: str) -> dict[str, Any]:
    try:
        data = json.loads(base64.b64decode(value.strip(), validate=False))
    except (ValueError, TypeError) as exc:
        raise X402Error(400, "invalid_payment", "PAYMENT-SIGNATURE must be base64-encoded JSON.") from exc
    if not isinstance(data, dict):
        raise X402Error(400, "invalid_payment", "PAYMENT-SIGNATURE must decode to a JSON object.")
    return data


def check_payload(payload: dict[str, Any], req: dict[str, Any], now: float | None = None) -> None:
    """Local checks before asking the facilitator (cheap rejects; the facilitator checks the signature and balance)."""
    if payload.get("x402Version") != X402_VERSION:
        raise X402Error(400, "unsupported_version", "Send an x402 v2 payment (PAYMENT-SIGNATURE header, x402Version 2).")
    acc = payload.get("accepted") or {}
    for key in ("scheme", "network", "amount"):
        if str(acc.get(key)) != str(req[key]):
            raise X402Error(402, "requirements_mismatch", f"accepted.{key} does not match this resource's requirements.")
    if str(acc.get("asset", "")).lower() != req["asset"].lower() or str(acc.get("payTo", "")).lower() != req["payTo"].lower():
        raise X402Error(402, "requirements_mismatch", "accepted.asset or accepted.payTo does not match.")
    auth = ((payload.get("payload") or {}).get("authorization")) or {}
    sig = (payload.get("payload") or {}).get("signature")
    if not sig or not auth:
        raise X402Error(400, "invalid_payment", "payload.signature and payload.authorization are required (EIP-3009).")
    if str(auth.get("to", "")).lower() != req["payTo"].lower() or str(auth.get("value")) != str(req["amount"]):
        raise X402Error(402, "requirements_mismatch", "authorization.to/value must equal payTo/amount.")
    now = now or time.time()
    try:
        valid_after, valid_before = int(auth.get("validAfter", 0)), int(auth.get("validBefore", 0))
    except (TypeError, ValueError) as exc:
        raise X402Error(400, "invalid_payment", "validAfter/validBefore must be unix seconds.") from exc
    if valid_before <= now + 5 or valid_after > now:
        raise X402Error(402, "authorization_expired", "The authorization is not valid right now; sign a fresh one.")
    nonce = str(auth.get("nonce") or "")
    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", nonce):
        raise X402Error(400, "invalid_payment", "authorization.nonce must be 32 bytes hex.")


def _urllib_transport(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "rainout-source-x402/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - configured facilitator URL
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}


TRANSPORT: Transport = _urllib_transport

Response = tuple[int, dict[str, str], bytes]


def paywall(headers: dict[str, str] | None, resource_url: str, description: str,
            handler: Callable[[], Response], env: dict[str, str] | None = None, now: float | None = None) -> Response:
    """Wrap ``handler`` with an x402 payment. Returns (status, headers, body)."""
    try:
        cfg = config(env)
    except X402Error as exc:
        return _json(exc.status, {"error": exc.code, "message": exc.message})
    req = requirements(cfg)
    h = {k.lower(): v for k, v in (headers or {}).items()}
    raw = h.get(PAY_HEADER.lower())
    if not raw:
        msg = "PAYMENT-SIGNATURE header is required"
        if h.get("x-payment"):
            msg = "This server speaks x402 v2: send PAYMENT-SIGNATURE (X-PAYMENT is v1)."
        return _challenge(cfg, resource_url, description, msg)
    try:
        payload = decode_header(raw)
        check_payload(payload, req, now)
        nonce_key = f"x402:nonce:{payload['payload']['authorization']['nonce'].lower()}"
        if api_keys.store().get(nonce_key):
            raise X402Error(402, "nonce_used", "This authorization was already used; sign a new one.")
        body = {"x402Version": X402_VERSION, "paymentPayload": payload, "paymentRequirements": req}
        status, verify = TRANSPORT(f"{cfg['facilitator']}/verify", body)
        if status != 200 or not verify.get("isValid"):
            raise X402Error(402, "payment_invalid", verify.get("invalidReason") or f"facilitator verify returned {status}")
    except X402Error as exc:
        if exc.status == 402:
            return _challenge(cfg, resource_url, description, f"{exc.code}: {exc.message}")
        return _json(exc.status, {"error": exc.code, "message": exc.message})
    except Exception as exc:  # facilitator unreachable
        return _json(502, {"error": "facilitator_unavailable", "message": str(exc)[:200]})

    status, out_headers, out_body = handler()
    if status >= 400:  # don't charge for a failed request
        return status, out_headers, out_body
    api_keys.store().set(nonce_key, True, NONCE_TTL_S)
    try:
        s_status, settle = TRANSPORT(f"{cfg['facilitator']}/settle", body)
    except Exception as exc:
        settle, s_status = {"success": False, "errorReason": "unexpected_settle_error", "transaction": "",
                            "network": NETWORK, "message": str(exc)[:200]}, 502
    if s_status != 200 or not settle.get("success"):
        settle = {"success": False, "errorReason": settle.get("errorReason") or "unexpected_settle_error",
                  "transaction": settle.get("transaction") or "", "network": NETWORK, "payer": verify.get("payer")}
        return 402, {**_base_headers(), RESP_HEADER: b64json(settle)}, json.dumps({"error": "settlement_failed", **settle}).encode()
    return status, {**out_headers, RESP_HEADER: b64json(settle)}, out_body


def _base_headers() -> dict[str, str]:
    return {"Content-Type": "application/json", "Cache-Control": "no-store", "Access-Control-Allow-Origin": "*",
            "Access-Control-Expose-Headers": f"{REQ_HEADER}, {RESP_HEADER}"}


def _json(status: int, payload: dict[str, Any], extra: dict[str, str] | None = None) -> Response:
    return status, {**_base_headers(), **(extra or {})}, json.dumps(payload).encode()


def _challenge(cfg: dict[str, Any], url: str, description: str, error: str) -> Response:
    pr = payment_required(cfg, url, description, error)
    return _json(402, {**pr, "price": f"{cfg['price_usdc']} USDC (Base Sepolia testnet, no real money)",
                       "how_to_pay": "Use an x402 v2 client (e.g. @x402/fetch or the x402 Python SDK) with a Base Sepolia wallet."},
                 {REQ_HEADER: b64json(pr)})
