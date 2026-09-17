#pragma version ==0.4.3
#pragma optimize gas
#pragma nonreentrancy on

"""
@title AgentPay Assurance provider warranty bond
@notice Providers pre-fund Arc USDC balances. A trusted MVP resolver may issue
        one full payment-sized refund from a provider's balance to a buyer.
@dev This contract does NOT validate GenLayer verdicts or original x402 payments.
     During the hackathon MVP, an off-chain trusted resolver must verify both
     before calling refund. Trustless GenLayer-to-Arc verification is out of scope.
"""


interface IERC20:
    def transferFrom(_from: address, _to: address, _amount: uint256) -> bool: nonpayable
    def transfer(_to: address, _amount: uint256) -> bool: nonpayable


event BondDeposited:
    provider: indexed(address)
    amount: uint256


event WarrantyRefunded:
    dispute_id: indexed(bytes32)
    payment_id_hash: indexed(bytes32)
    provider: indexed(address)
    buyer: address
    payment_amount: uint256


USDC: immutable(address)
RESOLVER: immutable(address)

provider_bond_balance: HashMap[address, uint256]
processed_disputes: public(HashMap[bytes32, bool])
refunded_payments: public(HashMap[bytes32, bool])


@deploy
def __init__(_usdc: address, _resolver: address):
    assert _usdc != empty(address), "zero USDC"
    assert _resolver != empty(address), "zero resolver"
    USDC = _usdc
    RESOLVER = _resolver


@external
@view
def usdc() -> address:
    return USDC


@external
@view
def resolver() -> address:
    return RESOLVER


@external
@view
def bond_balance(provider: address) -> uint256:
    return self.provider_bond_balance[provider]


@external
def deposit(amount: uint256):
    assert amount > 0, "zero amount"

    # Effects precede the fixed Arc USDC call. Any false return or revert rolls
    # back this credit, and the global nonreentrancy guard protects the update.
    self.provider_bond_balance[msg.sender] += amount
    assert extcall IERC20(USDC).transferFrom(msg.sender, self, amount), "transferFrom failed"
    log BondDeposited(provider=msg.sender, amount=amount)


@external
def refund(
    dispute_id: bytes32,
    payment_id_hash: bytes32,
    provider: address,
    buyer: address,
    payment_amount: uint256,
):
    # The resolver is trusted to have verified the finalized GenLayer verdict
    # and the original x402 payment. This contract checks only authorization,
    # replay safety, input sanity, and available provider coverage.
    assert msg.sender == RESOLVER, "not resolver"
    assert dispute_id != empty(bytes32), "zero dispute"
    assert payment_id_hash != empty(bytes32), "zero payment"
    assert not self.processed_disputes[dispute_id], "dispute processed"
    assert not self.refunded_payments[payment_id_hash], "payment refunded"
    assert provider != empty(address), "zero provider"
    assert buyer != empty(address), "zero buyer"
    assert payment_amount > 0, "zero amount"
    assert self.provider_bond_balance[provider] >= payment_amount, "insufficient bond"

    # Checks-effects-interactions. A failed token transfer reverts every effect.
    self.processed_disputes[dispute_id] = True
    self.refunded_payments[payment_id_hash] = True
    self.provider_bond_balance[provider] -= payment_amount
    assert extcall IERC20(USDC).transfer(buyer, payment_amount), "transfer failed"
    log WarrantyRefunded(
        dispute_id=dispute_id,
        payment_id_hash=payment_id_hash,
        provider=provider,
        buyer=buyer,
        payment_amount=payment_amount,
    )
