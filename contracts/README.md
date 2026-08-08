# Validation escrow contract

`ValidationEscrow.vy` is the contract-enforced workflow layer for
arc-agent-pay. It is deliberately small and has no owner, upgrade path, fee, or
administrative withdrawal.

## State machine

```text
NONE -- fund(EIP-3009) --> FUNDED -- approved verdict --> RELEASED
                                  -- rejected verdict --> REFUNDED
                                  -- timeout ----------> REFUNDED
```

- The Arc USDC address is immutable per deployment.
- Funding uses EIP-3009 `receiveWithAuthorization`, so only the escrow can
  submit the payer's authorization. This prevents a relayer from front-running
  the signature and transferring tokens without creating the order state.
- The authorization nonce is the complete work-order hash, binding every order
  term—not merely the amount and recipient—to the payer's signature.
- The order fixes the payer, provider, validator, amount, chain, deadlines,
  task hash, and nonce.
- The EIP-712 validator signature commits to the exact order and delivery.
- Validator signatures currently come from EOAs; EIP-1271 contract-wallet
  validators are not yet supported.
- Anyone may relay a release or refund, but the recipient and amount are fixed.
- A valid rejection refunds immediately. Validator downtime cannot trap funds
  because the payer can refund after `refund_after`.

## Build and test

The compiler and test EVM are pinned in `pyproject.toml` / `uv.lock`.

```bash
uv sync
uv run vyper -f abi contracts/ValidationEscrow.vy
uv run pytest tests/contracts/test_validation_escrow.py
```

Regenerate the packaged ABI after a contract change:

```bash
uv run vyper -f abi \
  -o arc_agent_pay/onchain/abi/validation_escrow.json \
  contracts/ValidationEscrow.vy
```

## Arc Testnet deployment

Deployment is intentionally opt-in and refuses any chain other than Arc
Testnet (`5042002`):

```bash
ESCROW_DEPLOYER_PRIVATE_KEY=... \
  uv run python scripts/deploy_validation_escrow.py --confirm-testnet
```

Use a dedicated deployer, then record the script's transaction, block, source
hash, bytecode hash, and runtime-code hash output. Do not reuse an application
seller or payer key as the deployer.

After deployment, exercise one capped release (the default is 0.001 USDC and
the script refuses amounts above 0.01 USDC):

```bash
ESCROW_SMOKE_PAYER_PRIVATE_KEY=... \
ESCROW_SMOKE_VALIDATOR_PRIVATE_KEY=... \
  uv run python scripts/smoke_validation_escrow.py \
    --escrow 0x... \
    --provider 0x... \
    --confirm-testnet-spend
```

The contract is unaudited. Do not deploy it on mainnet or use it with funds you
cannot afford to lose.
