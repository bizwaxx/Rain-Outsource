"""Read-only check of x402 payloads against the public testnet facilitator.

Signs a real EIP-3009 transferWithAuthorization (EIP-712) with a throwaway, UNFUNDED
key and calls ONLY the facilitator's read-only POST /verify. No funds exist, nothing is
settled. Expected answer: isValid false, invalid_exact_evm_insufficient_balance, which
proves the facilitator parsed the payload, recovered the signature and simulated the
transfer on Base Sepolia USDC.

    uv venv /tmp/x402venv && uv pip install --python /tmp/x402venv/bin/python eth-account
    /tmp/x402venv/bin/python scripts/x402_testnet_probe.py
"""
import base64, json, os, secrets, sys, time, urllib.request
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from eth_account import Account
from eth_account.messages import encode_typed_data
from rainout_agent import x402

acct = Account.create()  # throwaway, unfunded
cfg = x402.config({"X402_PAY_TO": "0x209693Bc6afc0C5328bA36FaF03C514EF312287C"})
req = x402.requirements(cfg)
now = int(time.time())
auth = {"from": acct.address, "to": req["payTo"], "value": req["amount"], "validAfter": str(now - 600),
        "validBefore": str(now + 60), "nonce": "0x" + secrets.token_hex(32)}
typed = {"types": {"EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                                    {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"}],
                   "TransferWithAuthorization": [{"name": "from", "type": "address"}, {"name": "to", "type": "address"},
                                                 {"name": "value", "type": "uint256"}, {"name": "validAfter", "type": "uint256"},
                                                 {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"}]},
         "primaryType": "TransferWithAuthorization",
         "domain": {"name": "USDC", "version": "2", "chainId": 84532, "verifyingContract": x402.USDC_BASE_SEPOLIA},
         "message": {**auth, "value": int(auth["value"]), "validAfter": int(auth["validAfter"]), "validBefore": int(auth["validBefore"]),
                     "nonce": bytes.fromhex(auth["nonce"][2:])}}
sig = acct.sign_message(encode_typed_data(full_message=typed)).signature.hex()
payload = {"x402Version": 2, "accepted": req, "payload": {"signature": "0x" + sig.removeprefix("0x"), "authorization": auth}}
x402.check_payload(payload, req)
body = {"x402Version": 2, "paymentPayload": payload, "paymentRequirements": req}
r = urllib.request.Request("https://x402.org/facilitator/verify", data=json.dumps(body).encode(),
                           headers={"Content-Type": "application/json", "User-Agent": "rainout-source-x402/0.1"}, method="POST")
try:
    with urllib.request.urlopen(r, timeout=20) as resp:
        print(resp.status, resp.read().decode()[:400])
except urllib.error.HTTPError as e:
    print(e.code, e.read().decode()[:400])
