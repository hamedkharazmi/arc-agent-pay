# AgentPay Assurance

AgentPay Assurance is an optional post-payment protection layer for x402 services. It records the terms and delivery evidence that existed at payment time, proves the actual Arc USDC transfer, asks GenLayer to adjudicate the semantic SLA, and—only for a fully bound finalized violation—allows a trusted MVP relay to request a full refund from a provider-funded `WarrantyBond`.

The original x402 payment is not reversed. A refund is a new USDC transfer from the provider's bond.

## Problem

x402 answers “was the provider paid?” It does not answer “did the response keep the provider's promise?” HTTP success, a local payment status, or a caller-supplied transaction hash cannot establish both economic settlement and semantic service delivery.

Assurance separates those questions:

- Arc proves who paid whom, which asset moved, and how much.
- GenLayer judges whether frozen human-readable acceptance criteria were materially violated.
- AgentPay binds both results to the same immutable evidence and dispute.
- WarrantyBond enforces the final full-refund and replay rules on Arc.

## Design

The Assurance path is deliberately post-payment:

1. A provider registers `AssuranceTerms` and funds its WarrantyBond balance.
2. The buyer uses the normal x402 flow; the provider is paid immediately.
3. An optional observer captures the otherwise hidden 402 → authorization → paid retry exchange.
4. AgentPay sanitizes, bounds, hashes, and persists `AssuranceEvidence` separately from the payment journal.
5. `ArcPaymentVerifier` verifies the actual receipt and USDC `Transfer` event.
6. A deterministic dispute ID and bounded adjudication payload are created.
7. The authorized AgentPay operator submits that payload to the GenLayer Intelligent Contract.
8. After protocol finality, AgentPay reads the verdict from finalized contract state and checks every binding.
9. `AssuranceSettlementRelay` revalidates eligibility and submits one exact `WarrantyBond.refund(...)` call.

This is not the repository's ValidationEscrow workflow. ValidationEscrow gates payment release from a prefunded escrow. Assurance pays the provider normally first and can later create a new transfer from a separate provider-funded warranty balance.

## Assurance terms

`Service.assurance` is optional, so existing service catalog JSON remains compatible. When present, `AssuranceTerms` freezes:

| Field | Meaning |
| --- | --- |
| `version` | Terms schema/version identifier |
| `provider_address` | Provider bound to the x402 `payTo` address |
| `warranty_bond_address` | Arc WarrantyBond backing the service |
| `acceptance_criteria` | Non-empty explicit natural-language criteria |
| `max_response_time_ms` | Optional application-observed delivery threshold |
| `dispute_window_seconds` | Time allowed to open a dispute |
| `refund_bps` | `10000` for the full-refund MVP |

This is intentionally not a general SLA DSL.

## Evidence model and sanitization

`AssuranceEvidence` commits to the immutable service/SLA snapshot, request, selected x402 quote, payment commitment, relevant timestamps, paid response, settlement metadata, and transaction reference.

Evidence handling is conservative:

- request and response headers use an allowlist;
- authorization, cookies, API keys, `PAYMENT-SIGNATURE`, and private signing material are excluded;
- sensitive URL and JSON fields are redacted;
- the signed payment payload is represented by a hash, not stored as raw authorization material;
- response and request bodies are bounded, with a canonical representation and digest when needed;
- the GenLayer adjudication payload is capped at 16 KiB and body content at 4 KiB;
- large evidence blobs live in a separate SQLite `AssuranceStore`, not in `Payment`.

The evidence recorder is optional. Without an observer, normal `PaymentClient` behavior is unchanged. Observer failures do not cause a second paid retry.

## Canonical identities and hashes

Canonical JSON uses UTF-8, lexicographically sorted keys, and compact separators. The terms and evidence hashes use Ethereum Keccak-256 over their canonical model representation.

The payment identity is domain-separated:

```text
payment_id_hash = keccak256(
  b"agentpay-assurance:payment:v1" + UTF8(payment_id)
)
```

The dispute identity is deterministic:

```text
dispute_id = keccak256(
  b"agentpay-assurance:dispute:v1"
  + payment_id_hash_bytes32
  + evidence_hash_bytes32
)
```

The GenLayer adjudication input hash is SHA-256 over the exact canonical UTF-8 JSON payload. Ethereum Keccak and SHA-256 are intentionally distinct here; neither is substituted with Python's `hash()` or `sha3_256`.

The final settlement binding is itself canonically hashed and includes the verified Arc payment, finalized GenLayer result, contract and transaction identities, evidence, dispute, SLA terms, and adjudication input.

## Arc payment verification

`ArcPaymentVerifier` does not trust HTTP success, `PaymentStatus.SUCCESS`, or caller-provided buyer/provider/amount fields. It requires:

- connected chain ID `5042002`;
- x402 network `eip155:5042002`;
- Arc Testnet USDC `0x3600000000000000000000000000000000000000`;
- an existing successful transaction receipt and the configured confirmation count;
- x402 `payTo` equal to the frozen SLA provider;
- the expected atomic amount from the selected x402 quote, not the catalog display price;
- exactly one matching USDC `Transfer` emitted by the configured token contract.

The canonical buyer is `Transfer.from`. The provider is jointly bound by the SLA snapshot, x402 `payTo`, and `Transfer.to`. The amount comes from the selected quote and matching event. A successful on-chain transfer remains valid evidence even if the later HTTP response was lost; conversely, HTTP success without the transfer is not Assurance-eligible.

## Dispute and adjudication payload

Only one dispute is allowed per payment. `AssuranceStore` provides deterministic serialization, duplicate/replay checks, and persisted dispute/settlement state.

The canonical `AdjudicationPayload` contains only adjudication-relevant frozen data:

- dispute, payment, evidence, and terms hashes;
- acceptance criteria and optional response-time limit;
- service description;
- sanitized method, URL, and bounded request representation;
- paid response status and bounded response representation;
- relevant delivery timing; and
- the buyer's allegation, explicitly labeled untrusted.

It excludes credentials, raw signatures, receipts, full SQLite records, and unnecessary x402 internals.

## GenLayer Intelligent Contract

[`genlayer/contracts/AgentPayAssurance.py`](../genlayer/contracts/AgentPayAssurance.py) is deployed on GenLayer Studio-dev. Only the configured AgentPay operator can create an adjudication; this prevents an arbitrary caller from substituting a payload for a legitimate dispute ID or evidence hash.

The contract:

- validates identifiers and payload size before nondeterministic execution;
- recomputes the SHA-256 input hash from the exact payload it judges;
- allows one `UNRESOLVED → RESOLVED` transition per dispute;
- uses an independent leader and validator assessment rather than accepting one LLM call;
- treats the SLA, request, response, and buyer allegation as untrusted evidence, not instructions;
- reaches consensus on the settlement-relevant `sla_violated` boolean without requiring generated reason prose to match word-for-word; and
- stores the verdict only after accepted consensus, never inside the nondeterministic block.

The reason is bounded explanatory text. It is useful for auditability and UI, but it never controls settlement.

## Finalized verdict binding

The GenLayer adapter does not treat transaction submission or protocol acceptance as final. It waits for protocol `Finalized`, requires successful execution (`FINISHED_WITH_RETURN`), reads the verdict from `LATEST_FINAL`, and verifies:

- GenLayer contract address and adjudication transaction ID;
- `dispute_id`;
- `payment_id_hash`;
- `evidence_hash`;
- `terms_hash`; and
- `adjudication_input_hash`.

Only `sla_violated = true` can become settlement-eligible. A fulfilled-SLA verdict is explicitly non-settleable.

## WarrantyBond

[`contracts/WarrantyBond.vy`](../contracts/WarrantyBond.vy) is a small Arc Testnet contract configured with one USDC token and one trusted resolver. Providers deposit USDC for themselves. A resolver-only full refund:

```text
refund(dispute_id, payment_id_hash, provider, buyer, payment_amount)
```

requires a positive amount, nonzero addresses, sufficient provider balance, an unused dispute ID, and an unrefunded payment hash. It deducts the bond balance before the external token transfer and consumes both replay keys. It does not validate GenLayer or the original x402 payment; those are trusted-relay responsibilities in this MVP.

## Trusted settlement relay

`AssuranceSettlementBinding` combines the original evidence and dispute, independently verified Arc transfer, finalized GenLayer transaction/verdict, and exact cross-system hashes. No caller can override the derived buyer, provider, or refund amount.

Immediately before submission, `AssuranceSettlementRelay` revalidates:

- Arc chain, asset, receipt, buyer, provider, and amount;
- GenLayer finality, execution success, contract, verdict, and every binding;
- non-synthetic evidence;
- local settlement state;
- WarrantyBond resolver, balance, and on-chain replay flags.

The relay sends exactly one transaction. If broadcast outcome is ambiguous, it records that state and does not blind-retry. Once a transaction hash exists, only that transaction is resumed or polled.

Local SQLite state prevents accidental duplicate actions and distinguishes eligible, attempted, pending, confirmed, failed-before-submission, ambiguous, and already-settled outcomes. On-chain WarrantyBond mappings remain the final replay authority.

## Live end-to-end demo

The committed artifacts record a completed run:

1. A buyer made a real 1 USDC x402 payment on Arc: [transaction `0xbd86…5617`](https://testnet.arcscan.app/tx/0xbd863ffbe75ca3e9357538790b3929acecd27fd8e6bf9f515d0a263a1f135617).
2. The paid endpoint returned HTTP 200 with `DOGE will definitely outperform everything this week.`
3. The frozen SLA required BTC, ETH, and SOL coverage, facts/analysis separation, a risk/caveat, and delivery within 2000 ms. Observed delivery was 2908 ms.
4. GenLayer transaction [`0xb6d1…81c8`](https://explorer-studio-dev.genlayer.com/transactions/0xb6d17051ec64f15d6cab6a2809b5d61fabb69ade69a8cbf3490eb9e2901b81c8) finalized successfully with `sla_violated = true` and all bindings verified.
5. WarrantyBond sent a new 1 USDC transfer to the original buyer: [transaction `0x5315…d970`](https://testnet.arcscan.app/tx/0x53159b78f250c515ed796584d8d22ab3bd43c1dfd403638bfe3b4877c29ad970).
6. The buyer moved from 19 to 20 USDC, the provider bond moved from 5 to 4 USDC, and a second settlement preflight was refused.

### Live deployments

| Component | Network | Deployment |
| --- | --- | --- |
| AgentPayAssurance | GenLayer Studio-dev (`61997`) | [`0x41c5de73bda3379351a2d77648cb77389841dc53`](https://explorer-studio-dev.genlayer.com/contracts/0x41c5de73bda3379351a2d77648cb77389841dc53) |
| Intelligent Contract deployment | GenLayer Studio-dev (`61997`) | [transaction `0x5afb…6118`](https://explorer-studio-dev.genlayer.com/transactions/0x5afb86c59276e700dc64aeb2039f55ba995252764a557b12e7b0e695090e6118) |
| WarrantyBond | Arc Testnet (`5042002`) | `0x74c4b5006136d37134B60F4Ff952E446BC11C901` · [deployment transaction](https://testnet.arcscan.app/tx/0x6f2cd7f9cc29ee23ca90478087b600439cf49326a51f8096d8a36d7f817d405d) |
| USDC | Arc Testnet (`5042002`) | `0x3600000000000000000000000000000000000000` |

The exact public proof is in [`contracts/deployments/`](../contracts/deployments/) and [`genlayer/deployments/studio-dev.json`](../genlayer/deployments/studio-dev.json).

## Reproduction runbook

These are the real scripts and order used by the live demo. Commands labeled **WRITE** submit testnet transactions and require deliberately funded, separate operator/provider/buyer keys. Never put key values in shell history or repository files. The pinned `genlayer` extra requires Python 3.12 or newer.

```bash
uv sync --group dev --extra all

export ASSURANCE_OPERATOR_PRIVATE_KEY=...  # GenLayer submitter + Arc resolver
export ASSURANCE_PROVIDER_PRIVATE_KEY=...  # x402 provider + bond depositor
export ASSURANCE_BUYER_PRIVATE_KEY=...     # x402 buyer
```

1. **WRITE — GenLayer:** deploy the Intelligent Contract on Studio-dev.

   ```bash
   uv run --extra genlayer python scripts/deploy_genlayer_assurance.py --network studio-dev
   ```

2. **WRITE — Arc:** deploy WarrantyBond and fund the provider warranty.

   ```bash
   uv run python scripts/deploy_warranty_bond.py --confirm-testnet
   uv run python scripts/fund_warranty_bond.py --confirm-testnet-spend
   ```

3. **LOCAL SERVER:** run the deterministic x402 demo provider in one terminal.

   ```bash
   uv run python scripts/run_assurance_demo_service.py
   ```

4. **WRITE — Arc:** make one real x402 payment and persist/verify its evidence.

   ```bash
   uv run python scripts/run_assurance_demo_payment.py --confirm-testnet-payment
   ```

5. **READ-ONLY ON ARC/GENLAYER, LOCAL WRITE:** reverify the payment, create the deterministic dispute, and freeze its canonical payload.

   ```bash
   uv run --extra genlayer python scripts/prepare_assurance_demo_dispute.py
   ```

6. **WRITE — GenLayer:** submit once, wait for protocol finality, and read the verdict from finalized state.

   ```bash
   uv run --extra genlayer python scripts/submit_assurance_demo_adjudication.py
   ```

7. **WRITE — Arc:** submit the eligible refund through the settlement relay.

   ```bash
   uv run --extra genlayer python scripts/run_assurance_demo_refund.py --confirm-testnet-refund
   ```

The submission repository already contains a completed deployment and consumed live demo identifiers. The tools deliberately fail closed, preserve known transaction IDs, and refuse replays; the commands above are a lifecycle runbook, not an instruction to replay the committed demo. Use fresh dedicated testnet wallets and isolated artifacts for a new run. Never delete a known transaction ID to force a retry.

## Trust model and deliberate MVP limits

- The GenLayer → Arc relay is trusted; there is no bridge, light client, or trustless cross-chain proof.
- The authorized AgentPay operator constructs the payload from the local persisted evidence and is also the WarrantyBond resolver.
- Providers pre-fund their own bond; this MVP offers no production solvency guarantee or per-payment reservation.
- Refunds are full only; there are no partial refunds, premiums, yield, or multiple assets/chains.
- Arc Testnet USDC is the only supported settlement asset for Assurance.
- WarrantyBond has no provider withdrawal flow in the MVP.
- There is no autonomous background dispute worker or automatic dispute decision.
- Acceptance criteria are explicit natural language, not a general SLA DSL.
- Evidence sanitization is bounded and best-effort; application developers must still avoid putting secrets in service content.
- Delivery timestamps are application-observed. The demo metric runs from payment authorization to paid-response receipt, includes provider settlement, and excludes the initial 402 and buyer signing.
- Ambiguous transaction broadcasts require manual nonce/receipt/replay reconciliation and are never blind-retried.
- The explanatory GenLayer reason is non-authoritative; only the bound finalized boolean controls eligibility.

These are explicit hackathon boundaries, not hidden production guarantees.
