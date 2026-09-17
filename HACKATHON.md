# AgentPay Assurance — GenLayer Agent Tank

**Track:** Agentic Commerce Infrastructure

**One-liner:** Chargebacks for x402 agent payments.

[Website](https://agentpay.bond/) · [README](README.md) · [Technical design](docs/assurance.md)

## What is new

AgentPay was already a public Python SDK with x402/EIP-3009 payments on Arc, `PaymentClient`, service discovery, spending controls, hosted ecosystem integration, and earlier validation workflows.

Agent Tank produced **AgentPay Assurance**: frozen SLA terms, sanitized exchange evidence, deterministic identities, independent Arc payment verification, GenLayer semantic adjudication, finalized verdict verification, a provider-funded WarrantyBond, exact cross-chain binding, and a replay-safe trusted settlement relay.

## Why GenLayer

Arc can prove the USDC transfer and enforce the refund. It cannot determine whether arbitrary service content kept a human-readable promise. GenLayer's Intelligent Contract and validator consensus make that semantic decision; AgentPay binds it to the exact Arc payment.

**Arc settles the money. GenLayer judges whether the service kept its promise. AgentPay binds the two.**

## 60-second architecture

```text
x402 payment on Arc → bounded AssuranceEvidence → verified USDC Transfer
→ deterministic dispute → GenLayer finalized SLA verdict
→ exact AssuranceSettlementBinding → trusted MVP relay
→ new USDC transfer from provider-funded WarrantyBond to original buyer
```

The original payment is not reversed. Assurance is a post-payment path and does not use the repository's separate ValidationEscrow flow.

## Live proof

- **GenLayer contract:** [`0x41c5…c53`](https://explorer-studio-dev.genlayer.com/contracts/0x41c5de73bda3379351a2d77648cb77389841dc53) on Studio-dev `61997`
- **Original 1 USDC x402 payment:** [Arc transaction](https://testnet.arcscan.app/tx/0xbd863ffbe75ca3e9357538790b3929acecd27fd8e6bf9f515d0a263a1f135617)
- **Finalized violation verdict:** [GenLayer transaction](https://explorer-studio-dev.genlayer.com/transactions/0xb6d17051ec64f15d6cab6a2809b5d61fabb69ade69a8cbf3490eb9e2901b81c8)
- **WarrantyBond:** `0x74c4b5006136d37134B60F4Ff952E446BC11C901` on Arc Testnet `5042002`
- **1 USDC refund:** [Arc transaction](https://testnet.arcscan.app/tx/0x53159b78f250c515ed796584d8d22ab3bd43c1dfd403638bfe3b4877c29ad970)

The bad paid response mentioned only DOGE while the SLA required BTC, ETH, SOL, fact/analysis separation, a risk/caveat, and ≤ 2000 ms delivery. GenLayer finalized `sla_violated = true`; the buyer returned from 19 to 20 USDC, the provider bond fell from 5 to 4 USDC, and replay checks refused a second settlement.

## Judge verification checklist

- [Payment proof](contracts/deployments/arc-testnet-assurance-demo-payment.json)
- [Frozen dispute and canonical payload](contracts/deployments/arc-testnet-assurance-demo-dispute.json)
- [Finalized GenLayer verdict and bindings](contracts/deployments/arc-testnet-assurance-demo-adjudication.json)
- [Confirmed refund and replay state](contracts/deployments/arc-testnet-assurance-demo-refund.json)
- [GenLayer deployment metadata](genlayer/deployments/studio-dev.json)
- [WarrantyBond deployment metadata](contracts/deployments/arc-testnet-warranty-bond.json)
- [Technical design and reproduction runbook](docs/assurance.md)
