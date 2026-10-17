"""Hard safety gates. A token that fails any of these is never bought, whatever its score.

Solana: on-chain mint checks (mint/freeze authority, dangerous Token-2022 extensions) plus the
RugCheck report (holder concentration, insiders, flagged risks).
EVM: GoPlus token security (honeypot, taxes, owner powers, holder concentration).
Both chains additionally get a buy-then-sell quote check in the engine before any order.
"""

from __future__ import annotations

import logging
from typing import Any

from .config import SafetyConfig
from .data.http import Http
from .data.solana_rpc import TOKEN_2022_PROGRAM, SolanaRPC
from .models import PairSnapshot, SafetyReport
from .util import safe_float

log = logging.getLogger(__name__)

RUGCHECK = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
GOPLUS = "https://api.gopluslabs.io/api/v1/token_security/{chain_id}"
GOPLUS_CHAIN_IDS = {"ethereum": "1", "bsc": "56", "base": "8453"}

# Token-2022 extensions that let the issuer block, claw back or tax transfers.
DANGEROUS_EXTENSIONS = {"permanentDelegate", "transferHook", "nonTransferable", "defaultAccountState", "pausableConfig"}


def _flag(d: dict, key: str) -> bool:
    return str(d.get(key, "0")).strip() == "1"


def evaluate_rugcheck(report: dict, cfg: SafetyConfig) -> tuple[list[str], list[str], dict[str, Any]]:
    reasons: list[str] = []
    warnings: list[str] = []
    token = report.get("token") or {}

    if report.get("rugged"):
        reasons.append("rugcheck: marked as rugged")
    mint_auth = report.get("mintAuthority", token.get("mintAuthority"))
    freeze_auth = report.get("freezeAuthority", token.get("freezeAuthority"))
    if cfg.require_mint_revoked and mint_auth:
        reasons.append("mint authority not revoked")
    if cfg.require_freeze_revoked and freeze_auth:
        reasons.append("freeze authority not revoked")

    for risk in report.get("risks") or []:
        level = str(risk.get("level", "")).lower()
        name = risk.get("name", "risk")
        if level == "danger":
            reasons.append(f"rugcheck danger: {name}")
        elif level == "warn":
            warnings.append(f"rugcheck warn: {name}")

    risk_norm = safe_float(report.get("score_normalised"), default=-1)
    if risk_norm > cfg.max_rugcheck_risk:
        reasons.append(f"rugcheck risk score {risk_norm:.0f} > {cfg.max_rugcheck_risk:.0f}")

    # Holder concentration, ignoring pool / bonding-curve accounts.
    market_accounts: set[str] = set()
    for m in report.get("markets") or []:
        for k in ("pubkey", "liquidityA", "liquidityB", "liquidityAAccount", "liquidityBAccount"):
            if m.get(k):
                market_accounts.add(m[k])
    holders = [
        h
        for h in report.get("topHolders") or []
        if h.get("address") not in market_accounts and h.get("owner") not in market_accounts
    ]
    top10 = sum(safe_float(h.get("pct")) for h in holders[:10])
    insider = sum(safe_float(h.get("pct")) for h in holders if h.get("insider"))
    if holders and top10 > cfg.max_top10_holders_pct:
        reasons.append(f"top-10 holders own {top10:.1f}% > {cfg.max_top10_holders_pct:.0f}%")
    if insider > cfg.max_insider_pct:
        reasons.append(f"insider wallets hold {insider:.1f}%")
    total_holders = int(safe_float(report.get("totalHolders")))
    if total_holders and total_holders < cfg.min_holders:
        reasons.append(f"only {total_holders} holders")

    data = {
        "risk_norm": risk_norm if risk_norm >= 0 else None,
        "top10_pct": top10 if holders else None,
        "insider_pct": insider,
        "holders": total_holders or None,
    }
    return reasons, warnings, data


def evaluate_mint(info: dict, cfg: SafetyConfig) -> tuple[list[str], list[str]]:
    reasons: list[str] = []
    warnings: list[str] = []
    if cfg.require_mint_revoked and info.get("mintAuthority"):
        reasons.append("mint authority not revoked (on-chain)")
    if cfg.require_freeze_revoked and info.get("freezeAuthority"):
        reasons.append("freeze authority not revoked (on-chain)")
    if info.get("program") == TOKEN_2022_PROGRAM:
        for ext in info.get("extensions") or []:
            name = ext.get("extension", "")
            if name in DANGEROUS_EXTENSIONS:
                reasons.append(f"dangerous Token-2022 extension: {name}")
            elif name == "transferFeeConfig":
                state = ext.get("state") or {}
                fee = (state.get("newerTransferFee") or state.get("olderTransferFee") or {}).get("transferFeeBasisPoints", 0)
                if safe_float(fee) / 100 > cfg.max_sell_tax_pct:
                    reasons.append(f"Token-2022 transfer fee {safe_float(fee) / 100:.1f}%")
                elif fee:
                    warnings.append(f"Token-2022 transfer fee {safe_float(fee) / 100:.1f}%")
    return reasons, warnings


def evaluate_goplus(sec: dict, cfg: SafetyConfig) -> tuple[list[str], list[str], dict[str, Any]]:
    reasons: list[str] = []
    warnings: list[str] = []
    for key, label in (
        ("is_honeypot", "honeypot"),
        ("cannot_sell_all", "cannot sell all"),
        ("hidden_owner", "hidden owner"),
        ("can_take_back_ownership", "ownership can be reclaimed"),
        ("owner_change_balance", "owner can change balances"),
        ("selfdestruct", "self-destruct"),
        ("is_blacklisted", "blacklist function"),
        ("transfer_pausable", "transfers pausable"),
        ("slippage_modifiable", "tax modifiable"),
        ("personal_slippage_modifiable", "per-wallet tax modifiable"),
    ):
        if _flag(sec, key):
            reasons.append(label)
    if cfg.require_mint_revoked and _flag(sec, "is_mintable"):
        reasons.append("mintable")
    if sec.get("is_open_source") is not None and not _flag(sec, "is_open_source"):
        reasons.append("contract not verified")
    if _flag(sec, "is_proxy"):
        warnings.append("upgradeable proxy")
    if _flag(sec, "trading_cooldown"):
        warnings.append("trading cooldown")

    buy_tax = safe_float(sec.get("buy_tax")) * 100
    sell_tax = safe_float(sec.get("sell_tax")) * 100
    if buy_tax > cfg.max_buy_tax_pct:
        reasons.append(f"buy tax {buy_tax:.1f}%")
    if sell_tax > cfg.max_sell_tax_pct:
        reasons.append(f"sell tax {sell_tax:.1f}%")

    lp_addrs = {str(h.get("address", "")).lower() for h in sec.get("lp_holders") or []}
    holders = [
        h
        for h in sec.get("holders") or []
        if not h.get("is_locked") and str(h.get("address", "")).lower() not in lp_addrs
        and str(h.get("tag", "")).lower() not in ("burn", "null", "dead")
    ]
    top10 = sum(safe_float(h.get("percent")) * 100 for h in holders[:10])
    if holders and top10 > cfg.max_top10_holders_pct:
        reasons.append(f"top-10 holders own {top10:.1f}%")
    holder_count = int(safe_float(sec.get("holder_count")))
    if holder_count and holder_count < cfg.min_holders:
        reasons.append(f"only {holder_count} holders")
    locked_lp = sum(safe_float(h.get("percent")) for h in sec.get("lp_holders") or [] if h.get("is_locked"))
    if sec.get("lp_holders") and locked_lp < 0.5:
        warnings.append(f"only {locked_lp * 100:.0f}% of LP locked/burned")
    data = {"buy_tax": buy_tax, "sell_tax": sell_tax, "top10_pct": top10 if holders else None, "holders": holder_count or None}
    return reasons, warnings, data


class SafetyChecker:
    def __init__(self, cfg: SafetyConfig, http: Http, solana_rpc: SolanaRPC | None = None):
        self.cfg = cfg
        self.http = http
        self.rpc = solana_rpc

    async def check(self, chain: str, address: str, pair: PairSnapshot | None = None) -> SafetyReport:
        try:
            if chain == "solana":
                return await self._check_solana(address)
            return await self._check_evm(chain, address)
        except Exception as exc:  # network or parse failure
            log.warning("Safety check failed for %s:%s: %s", chain, address, exc)
            if self.cfg.fail_closed:
                return SafetyReport(ok=False, reasons=[f"safety data unavailable: {exc}"])
            return SafetyReport(ok=True, warnings=[f"safety data unavailable: {exc}"])

    async def _check_solana(self, mint: str) -> SafetyReport:
        reasons: list[str] = []
        warnings: list[str] = []
        data: dict[str, Any] = {}
        if self.rpc is not None:
            try:
                info = await self.rpc.get_mint_info(mint)
                r, w = evaluate_mint(info, self.cfg)
                reasons += r
                warnings += w
                data["decimals"] = info.get("decimals")
            except Exception as exc:
                warnings.append(f"on-chain mint check failed: {exc}")
        report = await self.http.get_json(RUGCHECK.format(mint=mint))
        r, w, d = evaluate_rugcheck(report or {}, self.cfg)
        reasons += r
        warnings += w
        data.update(d)
        return SafetyReport(ok=not reasons, reasons=reasons, warnings=warnings, data=data)

    async def _check_evm(self, chain: str, address: str) -> SafetyReport:
        resp = await self.http.get_json(
            GOPLUS.format(chain_id=GOPLUS_CHAIN_IDS[chain]), params={"contract_addresses": address}
        )
        result = (resp or {}).get("result") or {}
        sec = result.get(address.lower()) or next(iter(result.values()), None)
        if not sec:
            raise RuntimeError("GoPlus returned no data")
        reasons, warnings, data = evaluate_goplus(sec, self.cfg)
        return SafetyReport(ok=not reasons, reasons=reasons, warnings=warnings, data=data)
