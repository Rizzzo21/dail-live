"""Shared Base chain verification: the chain is the source of truth.

Extracted from usdc_payments.py so every USDC rail (manual + x402) uses one
pinned-contract receipt check. Behavior is identical to the original:
fetch the tx receipt ourselves via Base RPC and only credit what the
receipt proves arrived at our address as real, Circle-issued USDC.
"""
import json
import urllib.request

BASE_CHAIN_ID = 8453
# Native USDC on Base, 6 decimals. Pinned + verified 2026-10-02 against
# Circle's developer docs, Basescan, and Circle's FiatTokenV2_2 source
# (circlefin/stablecoin-evm). NOTE: this repo previously carried a corrupted
# 41-hex-char variant (...A4a1309); it failed is_hex_address and could never
# match a real Transfer log, so the manual USDC rail could not verify any
# deposit. Always re-verify against an authoritative source before mainnet.
BASE_USDC_CONTRACT = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_DECIMALS = 6
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"  # Transfer(address,address,uint256)
DEFAULT_RPC_URL = "https://mainnet.base.org"
FALLBACK_RPC_URLS = ["https://base.llamarpc.com", "https://1rpc.io/base"]
DEFAULT_TREASURY = "0xCd787bCf82279c121835EaaB37b34A502A6b8dBC"  # DAiL deposit wallet (receive-only)


class ChainError(ValueError):
    """Chain verification failure (agent-facing)."""


class ChainRevert(ChainError):
    """An eth_call / tx simulation reverted on-chain (distinct from the RPC
    being unreachable: a revert is the contract saying NO). Subclasses
    ChainError so existing fail-closed handlers catch it."""


class ChainVerifier:
    """Pinned-contract USDC receipt checks against Base RPC with fallbacks."""

    def __init__(self, treasury, rpc_urls=None, min_confirmations=2):
        self.treasury = (treasury or "").strip()
        self.rpc_urls = [u for u in (rpc_urls or []) if u]
        if not self.rpc_urls:
            self.rpc_urls = [DEFAULT_RPC_URL] + FALLBACK_RPC_URLS
        self.min_confirmations = max(1, int(min_confirmations or 2))

    @property
    def ready(self):
        return bool(self.treasury and self.rpc_urls)

    def _rpc(self, method, params):
        """Base JSON-RPC with fallback endpoints. The public primary is
        rate-limited and flakes; a flaky RPC must never block a legitimate
        deposit, so we try each URL in order and only fail when all do."""
        body = json.dumps({"jsonrpc": "2.0", "id": 1,
                           "method": method, "params": params}).encode()
        last_err = None
        for url in self.rpc_urls:
            req = urllib.request.Request(url, data=body,
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    resp = json.loads(r.read().decode())
            except Exception as e:
                last_err = e
                continue
            if not isinstance(resp, dict) or "error" in resp:
                last_err = resp.get("error") if isinstance(resp, dict) else "bad_response"
                continue
            return resp.get("result")
        raise ChainError(f"base_rpc_unreachable: {last_err!r}"[:200])

    def simulate_call(self, call):
        """eth_call that distinguishes a contract REVERT from RPC failure.

        Returns the result data on success. Raises ChainRevert when every
        reachable endpoint reports the call reverted (the contract said no),
        ChainError when no endpoint could be reached at all.
        """
        body = json.dumps({"jsonrpc": "2.0", "id": 1,
                           "method": "eth_call", "params": [call, "latest"]}).encode()
        last_err, saw_revert = None, None
        for url in self.rpc_urls:
            req = urllib.request.Request(url, data=body,
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    resp = json.loads(r.read().decode())
            except Exception as e:
                last_err = e
                continue
            if not isinstance(resp, dict):
                last_err = "bad_response"
                continue
            if "error" in resp:
                saw_revert = resp["error"]
                continue  # a revert is an answer; try another endpoint anyway
            return resp.get("result")
        if saw_revert is not None:
            raise ChainRevert(f"simulation reverted: {saw_revert!r}"[:200])
        raise ChainError(f"base_rpc_unreachable: {last_err!r}"[:200])

    def verify_usdc_to_treasury(self, tx_hash):
        """Returns (usdc_base_units_to_treasury, confirmations).

        Raises ChainError when the tx does not prove real USDC arrived.
        """
        receipt = self._rpc("eth_getTransactionReceipt", [tx_hash])
        if not receipt:
            raise ChainError("transaction not found on Base (is the hash correct and the tx sent?)")
        if receipt.get("status") != "0x1":
            raise ChainError("transaction failed on-chain; no credit")
        total = 0
        for log in receipt.get("logs") or []:
            if (log.get("address") or "").lower() != BASE_USDC_CONTRACT.lower():
                continue  # not real USDC: ignore lookalike tokens
            topics = log.get("topics") or []
            if len(topics) < 3 or (topics[0] or "").lower() != TRANSFER_TOPIC:
                continue
            to_addr = "0x" + topics[2][-40:]
            if to_addr.lower() != self.treasury.lower():
                continue  # USDC moved, but not to us
            try:
                total += int(log.get("data") or "0x0", 16)
            except ValueError:
                continue
        if total <= 0:
            raise ChainError("no USDC transfer to the DAiL deposit address in this transaction")
        latest = self._rpc("eth_blockNumber", [])
        try:
            confs = int(latest, 16) - int(receipt["blockNumber"], 16)
        except (TypeError, ValueError):
            raise ChainError("could not determine confirmations; try again")
        return total, confs
