# ERC-8183 compatibility

ArcAgentPay supports the current ERC-8183 **draft** through an explicit adapter. This does
not change, wrap, or relabel the already-deployed `ValidationEscrow` contract: that contract
uses a different state machine and is not ERC-8183 compliant.

Source reviewed: [ERC-8183 draft](https://eips.ethereum.org/EIPS/eip-8183), 2026-08-09,
pinned to [ethereum/ERCs revision `a078cab`](https://github.com/ethereum/ERCs/blob/a078cab5cc8e9581c15f76c091ed96eed28f02f7/ERCS/erc-8183.md).
The SDK profile string is `draft-reference-a078cab`.

## Mapping

| ArcAgentPay workflow | ERC-8183 |
| --- | --- |
| payer | client (`msg.sender` at creation) |
| provider | provider |
| validator | evaluator |
| `refund_after` | `expiredAt` |
| amount in token base units | budget |
| `urn:arc-agent-pay:work-order:<order_hash>` | description |
| `DeliveryEvidence.delivery_hash` | deliverable |
| full `ValidationVerdict` EIP-712 struct hash | completion/rejection reason |

The full verdict commitment is intentional. Mapping only `reason_hash` would fail to bind
the evaluator's decision, score, delivery, and validity window.

## Which ABI is packaged

The draft prose and its Solidity reference implementation currently disagree:

| Operation | Draft prose | Published reference contract |
| --- | --- | --- |
| `setBudget` authorization | client or provider | provider only |
| `fund` | includes `expectedBudget` | `fund(jobId, optParams)` |
| `setProvider` hook payload | optional `optParams` | no `optParams` argument |
| zero-budget job | prose says budget must be non-zero | reference permits it |

`Erc8183Client` targets the published Solidity reference contract ABI. Its `fund` method
requires `expected_budget` and checks the latest job before sending, but that client-side
check cannot provide the atomic front-running protection described by the prose. Do not use
the reference profile for adversarial funding until the deployed contract's exact ABI and
behavior have been reviewed.

The constructor accepts injected Web3 contract objects so deployments with extensions can
be integrated without changing the partner-neutral models. A deployment whose function
signatures differ needs a separate ABI profile; a402's profile will be added only after its
contract documentation and addresses are verified.

## Differences from ValidationEscrow

ArcAgentPay's original escrow precommits all terms in a signed `WorkOrder`, accepts relayed
EIP-3009 funding, and resolves a signed evaluator verdict without requiring the evaluator to
submit the transaction. Its on-chain states are `Funded`, `Released`, and `Refunded`.

ERC-8183 instead has `Open`, `Funded`, `Submitted`, `Completed`, `Rejected`, and `Expired`,
uses caller roles for each transition, and funds through ERC-20 allowance/`transferFrom` in
the reference implementation. Hooks and ERC-2771 are optional extensions. These differences
are why the SDK provides an adapter/client instead of claiming contract-level compatibility.

## Minimal flow

```python
from arc_agent_pay.workflow import Erc8183Client, deliverable_commitment

# Use the role's account for each transition. This example shows client actions.
jobs = Erc8183Client(contract_address, account=client_account, rpc=rpc_url)
created = jobs.create_job(
    provider=provider,
    evaluator=evaluator,
    expired_at=expired_at,
    description="ipfs://job-brief",
)

# The reference contract requires the provider to set the budget.
# provider_jobs.set_budget(created.job_id, 100_000)
jobs.approve_payment(100_000)
jobs.fund(created.job_id, expected_budget=100_000)

# provider_jobs.submit(created.job_id, deliverable_commitment(delivery))
# evaluator_jobs.complete(created.job_id, verdict_commitment(verdict))
```
