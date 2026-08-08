# Changelog

All notable changes to `arc-agent-pay` are documented here.

## 0.2.0 — 2026-08-08

### Added

- Durable rolling-window spending controls: daily total, hourly payment count,
  and per-provider daily limits, backed by memory or SQLite ledgers.
- Validation-gated workflow models that bind work orders, delivery evidence,
  and validator verdicts with deterministic hashes and EIP-712 signatures.
- EIP-3009 `receiveWithAuthorization` funding signatures bound to the complete
  work-order hash.
- `EscrowClient` bindings for funding, approved release, signed-rejection
  refund, timeout refund, and status reads.
- An ownerless Vyper escrow contract with immutable settlement terms, packaged
  ABI, deployment tooling, and executable local-EVM state-machine tests.
- A public, unaudited Arc Testnet deployment with successful low-value release
  and rejection-refund evidence.

### Security

- Funding uses the complete work-order hash as its EIP-3009 nonce, preventing a
  valid payer signature from being reused with altered provider, validator,
  amount, deadline, task, or refund terms.
- Funding uses receiver-safe `receiveWithAuthorization`, preventing a third
  party from front-running the escrow transaction and stranding funds.
- Verdict verification enforces domain separation, canonical low-`s`
  signatures, independent workflow roles, complete delivery bindings, and
  validity windows.

## 0.1.0 — 2026-08-07

- Initial public SDK release with x402/EIP-3009 payments, service discovery,
  budget enforcement, ERC-8004 identity integrations, agent workflows, MCP,
  and observability extras.
