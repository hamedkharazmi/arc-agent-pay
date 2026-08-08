# Trust & Safety

arc-agent-pay lets autonomous agents **spend money on their own**, so safety is a first-class concern. This document states the safeguards that are actually in place today and is honest about the current limits.

## TL;DR

- **Testnet only.** The SDK is configured for testnet USDC on **Arc Testnet** (chain `5042002`). Do not use it with mainnet funds.
- **The agent has pre-signature spend controls.** A hard budget cap is checked before any payment is signed, and optional reputation/allowlist/denylist policies can refuse providers before money moves.
- **Every payment is a single-use on-chain authorization.** Payments use EIP-3009 `transferWithAuthorization` with unique nonces.
- **Open source is part of the security model.** This code touches signing keys; users should be able to inspect exactly what is signed.

## Spending safety

- **Hard budget cap (`BudgetGuard`).** Every session has a `budget_usdc` ceiling. Each payment is checked against the remaining budget *before* it is signed; once the cap is hit, further payments are blocked ([budget.py](arc_agent_pay/budget.py)).
- **Cross-run spending caps (`SpendCaps` + `SpendLedger`).** Three independent rolling-window caps survive across restarts and separate runs — `daily_cap_usdc` (24 h total), `max_payments_per_hour` (velocity brake), and `provider_daily_cap_usdc` (per-counterparty ceiling). Backed by a durable SQLite ledger (stdlib, no extra dependency); fail-open so a broken ledger never crashes a run ([spending.py](arc_agent_pay/spending.py)).
- **Reputation-gated spending.** With a trust policy set (`min_provider_reputation`), the agent reads a provider's on-chain ERC-8004 reputation and refuses to pay anyone below the floor — before any money moves ([agent/trust.py](arc_agent_pay/agent/trust.py)). It is off by default and fail-open unless you opt into stricter identity requirements.
- **Allowlist, denylist, kill switch.** `ResearchAgent` supports provider allowlists, provider denylists, and `payments_disabled` for stopping all paid calls instantly.

## Key & signing safety

- **Use dedicated low-balance testnet keys.** Do not reuse a wallet that holds mainnet assets.
- **Private keys are supplied by the caller.** The SDK signs EIP-3009 authorizations locally from the account/key you provide. It does not require a hosted custody service.
- **MCP keys come from environment only.** The MCP server reads the wallet key and budget ceiling from environment variables, not from tool arguments, so a model cannot pass arbitrary keys in-band.

## Payment integrity

- **Single-use authorizations.** Payments are EIP-3009 `transferWithAuthorization` with a unique nonce; each authorization is single-use and replay-safe.
- **Budget is checked before signing.** Failed budget checks prevent signature creation, not just HTTP retry.
- **On-chain writes are receipt-checked.** ERC-8004 write helpers check transaction receipt status and raise on revert so a failed write is not reported as success ([identity/erc8004.py](arc_agent_pay/identity/erc8004.py)).

## Validation verdict integrity

- **Orders are fixed before delivery.** Work-order hashes bind the escrow,
  parties, validator, asset, amount, chain, deadlines, task hash, and a unique
  nonce using a Solidity-compatible fixed-width layout.
- **Verdicts are delivery-bound.** A validator signs the exact order hash,
  delivered-content hash, URI commitment, and delivery time—not a mutable URL
  or an unstructured success flag.
- **EIP-712 replay separation.** Verdict domains bind both chain id and escrow
  contract, preventing a signature from being reused on another chain or
  contract.
- **Strict signature checks.** Verification requires the configured validator,
  canonical low-`s` ECDSA signatures, valid timing, matching evidence, and—when
  used for release—an approving verdict ([workflow/](arc_agent_pay/workflow/)).

## Escrow integrity

- **Receiver-safe funding.** The payer signs EIP-3009
  `receiveWithAuthorization`, which requires the escrow itself to submit the
  authorization. A third-party relayer cannot front-run it and strand funds
  without creating the corresponding order state.
- **Funding binds the complete order.** The EIP-3009 authorization nonce is the
  work-order hash, so changing the provider, validator, amount, deadline, task,
  or any other term invalidates the payer's signature.
- **Immutable settlement terms.** The Arc USDC address is immutable at deploy,
  and the stored state is keyed by the hash of every order field. Release and
  refund amounts and recipients come from that same committed order.
- **No privileged drain.** The contract has no owner, fee, upgrade mechanism,
  or administrative withdrawal.
- **Validator liveness is not required.** A signed rejection refunds
  immediately; otherwise the payer can refund after the committed timeout.
- **Checks-effects-interactions.** Terminal state is written before USDC leaves
  the escrow, and global non-reentrancy protects every external entry point
  ([ValidationEscrow.vy](contracts/ValidationEscrow.vy)).

## Identity, reputation, and validation honesty

ERC-8004 reputation and validation are only meaningful when they come from a party **other than the agent**. This SDK exposes the plumbing: identity reads/writes, reputation reads/writes, validation request/response, and reputation-gated spending. It does not magically make self-attestation independent.

In production, keep roles separate:

| Role | What it does |
|------|--------------|
| Agent | Does work, pays, holds an identity |
| Seller / provider | Sells the data/API/LLM the agent buys |
| Reputation giver | Leaves feedback after transacting |
| Validator | Independently verifies the agent's work |

## Supply chain & code quality

- **CI security gates:** dependency vulnerability audit (`pip-audit`) and secret scanning (`gitleaks`) run in GitHub Actions ([.github/workflows/ci.yml](.github/workflows/ci.yml)).
- **Release provenance:** tagged releases publish through PyPI Trusted Publishing with GitHub OIDC, avoiding long-lived PyPI API tokens and producing provenance attestations ([.github/workflows/release.yml](.github/workflows/release.yml)).
- **Tests and lint:** SDK tests run on Python 3.11 and 3.12; `ruff` lint is enforced in CI.

## Known limitations

- **Testnet only.** This SDK is not audited and should not be pointed at mainnet funds.
- **BudgetGuard is pre-signature software enforcement.** It prevents the SDK from signing over-budget payments. It cannot constrain someone who directly controls the same EOA private key outside the SDK.
- **No trustless vault yet.** A contract-enforced spend cap would require holding funds in a contract wallet or vault and compatibility with the x402/EIP-3009 path. That design is intentionally not shipped until the Arc USDC/EIP-1271 compatibility question is verified.
- **Escrow is deployed on testnet but not audited.** The Vyper source, local EVM
  tests, and a low-value Arc Testnet lifecycle exercise cover funding, release,
  rejection refund, and timeout refund. The deployment evidence is public, but
  no production or mainnet escrow guarantee is claimed.
- **Escrow verdict signatures are EOA-only today.** The contract verifies
  canonical ECDSA signatures with `ecrecover`; EIP-1271 contract-wallet
  validators are not yet supported.
- **Reputation is only as good as the ecosystem.** Reputation/validation are useful when independent parties provide them; self-attestation only proves the plumbing works.

## Reporting a vulnerability

Please report security issues privately through the feedback channel at [agentpay.bond](https://agentpay.bond), or contact the maintainer directly if you already have contact details. Do not include private keys or seed phrases in any report.
