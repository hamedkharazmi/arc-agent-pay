#pragma version ==0.4.3
#pragma optimize gas
#pragma nonreentrancy on

"""
@title ArcAgentPay validation-gated USDC escrow
@notice Holds one fixed Arc USDC amount per work-order hash. A signed validator
        verdict releases it to the provider or refunds it to the payer. If the
        validator never responds, the payer can refund after the fixed timeout.
@dev Funding uses EIP-3009 receiveWithAuthorization so a third party cannot
     front-run the authorization and strand tokens at this contract.
"""


interface IEIP3009:
    def receiveWithAuthorization(
        _from: address,
        _to: address,
        _value: uint256,
        _valid_after: uint256,
        _valid_before: uint256,
        _nonce: bytes32,
        _v: uint8,
        _r: bytes32,
        _s: bytes32,
    ): nonpayable

    def transfer(_to: address, _value: uint256) -> bool: nonpayable


struct WorkOrder:
    escrow: address
    payer: address
    provider: address
    validator: address
    asset: address
    amount: uint256
    chain_id: uint256
    delivery_deadline: uint256
    refund_after: uint256
    task_hash: bytes32
    nonce: bytes32


struct DeliveryEvidence:
    order_hash: bytes32
    evidence_hash: bytes32
    uri_hash: bytes32
    delivered_at: uint256


struct ValidationVerdict:
    order_hash: bytes32
    evidence_hash: bytes32
    delivery_hash: bytes32
    approved: bool
    score: uint8
    reason_hash: bytes32
    issued_at: uint256
    valid_until: uint256


event OrderFunded:
    order_hash: indexed(bytes32)
    payer: indexed(address)
    provider: indexed(address)
    amount: uint256


event OrderReleased:
    order_hash: indexed(bytes32)
    provider: indexed(address)
    amount: uint256
    validator: address


event OrderRefunded:
    order_hash: indexed(bytes32)
    payer: indexed(address)
    amount: uint256
    rejected: bool


STATUS_NONE: constant(uint8) = 0
STATUS_FUNDED: constant(uint8) = 1
STATUS_RELEASED: constant(uint8) = 2
STATUS_REFUNDED: constant(uint8) = 3

SECP256K1_HALF_N: constant(uint256) = 57896044618658097711785492504343953926418782139537452191302581570759080747168

DOMAIN_TYPE_HASH: constant(bytes32) = 0x8b73c3c69bb8fe3d512ecc4cf759cc79239f7b179b0ffacaa9a75d522b39400f
DOMAIN_NAME_HASH: constant(bytes32) = 0xd1a93799728a9db540d3dc5e0ea7d01f1a77ca3667968b95c5ab5d92b753382d
DOMAIN_VERSION_HASH: constant(bytes32) = 0xc89efdaa54c0f20c7adf612882df0950f5a951637e0307cdcb4c672f298b8bc6
WORK_ORDER_TYPE_HASH: constant(bytes32) = 0xe516b1db205ed57770b2ae907829d79583841c3baae5a2127df94080e0e30a05
DELIVERY_TYPE_HASH: constant(bytes32) = 0x84f98375871fe2b15813ad8b117d0657e285a7f3d8582fd0f246c8538c3b7f29
VERDICT_TYPE_HASH: constant(bytes32) = 0xe998a47898c2f1915c4f3d42d84da33070b3036f4854a9f5fb12e87bc92fafeb

ASSET: immutable(address)
DOMAIN_SEPARATOR: immutable(bytes32)

status: public(HashMap[bytes32, uint8])


@deploy
def __init__(_asset: address):
    assert _asset != empty(address), "zero asset"
    ASSET = _asset
    DOMAIN_SEPARATOR = keccak256(
        abi_encode(
            DOMAIN_TYPE_HASH,
            DOMAIN_NAME_HASH,
            DOMAIN_VERSION_HASH,
            chain.id,
            self,
        )
    )


@external
@view
def asset() -> address:
    return ASSET


@external
@view
def domain_separator() -> bytes32:
    return DOMAIN_SEPARATOR


@internal
@pure
def _hash_order(order: WorkOrder) -> bytes32:
    return keccak256(
        abi_encode(
            WORK_ORDER_TYPE_HASH,
            order.escrow,
            order.payer,
            order.provider,
            order.validator,
            order.asset,
            order.amount,
            order.chain_id,
            order.delivery_deadline,
            order.refund_after,
            order.task_hash,
            order.nonce,
        )
    )


@external
@pure
def hash_order(order: WorkOrder) -> bytes32:
    return self._hash_order(order)


@internal
@pure
def _hash_delivery(delivery: DeliveryEvidence) -> bytes32:
    return keccak256(
        abi_encode(
            DELIVERY_TYPE_HASH,
            delivery.order_hash,
            delivery.evidence_hash,
            delivery.uri_hash,
            delivery.delivered_at,
        )
    )


@external
@pure
def hash_delivery(delivery: DeliveryEvidence) -> bytes32:
    return self._hash_delivery(delivery)


@internal
@view
def _validate_order(order: WorkOrder):
    assert order.escrow == self, "wrong escrow"
    assert order.asset == ASSET, "wrong asset"
    assert order.chain_id == chain.id, "wrong chain"
    assert order.payer != empty(address), "zero payer"
    assert order.provider != empty(address), "zero provider"
    assert order.validator != empty(address), "zero validator"
    assert order.payer != order.provider, "payer is provider"
    assert order.validator != order.payer and order.validator != order.provider, "validator not independent"
    assert order.payer != self and order.provider != self and order.validator != self, "escrow is party"
    assert order.amount > 0, "zero amount"
    assert order.task_hash != empty(bytes32), "zero task hash"
    assert order.nonce != empty(bytes32), "zero nonce"
    assert order.refund_after > order.delivery_deadline, "bad deadlines"


@internal
@view
def _validate_verdict(
    order: WorkOrder,
    delivery: DeliveryEvidence,
    verdict: ValidationVerdict,
    v: uint8,
    r: bytes32,
    s: bytes32,
) -> bytes32:
    self._validate_order(order)
    order_hash: bytes32 = self._hash_order(order)
    assert self.status[order_hash] == STATUS_FUNDED, "order not funded"
    assert delivery.order_hash == order_hash, "wrong delivery order"
    assert delivery.evidence_hash != empty(bytes32), "zero evidence"
    assert delivery.delivered_at <= order.delivery_deadline, "late delivery"
    assert verdict.order_hash == order_hash, "wrong verdict order"
    assert verdict.evidence_hash == delivery.evidence_hash, "wrong evidence"
    assert verdict.delivery_hash == self._hash_delivery(delivery), "wrong delivery"
    assert verdict.reason_hash != empty(bytes32), "zero reason"
    assert verdict.score <= 100, "bad score"
    assert verdict.issued_at >= delivery.delivered_at, "verdict predates delivery"
    assert verdict.issued_at <= block.timestamp, "future verdict"
    assert verdict.valid_until > block.timestamp, "expired verdict"
    assert verdict.valid_until <= order.refund_after, "verdict past refund"
    assert v == 27 or v == 28, "bad signature v"
    assert convert(s, uint256) > 0 and convert(s, uint256) <= SECP256K1_HALF_N, "bad signature s"

    verdict_hash: bytes32 = keccak256(
        abi_encode(
            VERDICT_TYPE_HASH,
            verdict.order_hash,
            verdict.evidence_hash,
            verdict.delivery_hash,
            verdict.approved,
            verdict.score,
            verdict.reason_hash,
            verdict.issued_at,
            verdict.valid_until,
        )
    )
    digest: bytes32 = keccak256(concat(b"\x19\x01", DOMAIN_SEPARATOR, verdict_hash))
    signer: address = ecrecover(digest, v, r, s)
    assert signer != empty(address) and signer == order.validator, "wrong validator"
    return order_hash


@external
def fund(order: WorkOrder, v: uint8, r: bytes32, s: bytes32) -> bytes32:
    self._validate_order(order)
    assert block.timestamp < order.delivery_deadline, "delivery window closed"
    order_hash: bytes32 = self._hash_order(order)
    assert self.status[order_hash] == STATUS_NONE, "order already exists"

    # Effects precede the trusted Arc USDC call; a revert rolls the state back.
    self.status[order_hash] = STATUS_FUNDED
    extcall IEIP3009(ASSET).receiveWithAuthorization(
        order.payer,
        self,
        order.amount,
        0,
        order.delivery_deadline,
        order_hash,
        v,
        r,
        s,
    )
    log OrderFunded(
        order_hash=order_hash,
        payer=order.payer,
        provider=order.provider,
        amount=order.amount,
    )
    return order_hash


@external
def release(
    order: WorkOrder,
    delivery: DeliveryEvidence,
    verdict: ValidationVerdict,
    v: uint8,
    r: bytes32,
    s: bytes32,
):
    assert verdict.approved, "verdict rejected"
    order_hash: bytes32 = self._validate_verdict(order, delivery, verdict, v, r, s)

    self.status[order_hash] = STATUS_RELEASED
    assert extcall IEIP3009(ASSET).transfer(order.provider, order.amount), "transfer failed"
    log OrderReleased(
        order_hash=order_hash,
        provider=order.provider,
        amount=order.amount,
        validator=order.validator,
    )


@external
def refund_rejected(
    order: WorkOrder,
    delivery: DeliveryEvidence,
    verdict: ValidationVerdict,
    v: uint8,
    r: bytes32,
    s: bytes32,
):
    assert not verdict.approved, "verdict approved"
    order_hash: bytes32 = self._validate_verdict(order, delivery, verdict, v, r, s)

    self.status[order_hash] = STATUS_REFUNDED
    assert extcall IEIP3009(ASSET).transfer(order.payer, order.amount), "transfer failed"
    log OrderRefunded(
        order_hash=order_hash,
        payer=order.payer,
        amount=order.amount,
        rejected=True,
    )


@external
def refund_timeout(order: WorkOrder):
    self._validate_order(order)
    order_hash: bytes32 = self._hash_order(order)
    assert self.status[order_hash] == STATUS_FUNDED, "order not funded"
    assert block.timestamp >= order.refund_after, "refund not available"

    self.status[order_hash] = STATUS_REFUNDED
    assert extcall IEIP3009(ASSET).transfer(order.payer, order.amount), "transfer failed"
    log OrderRefunded(
        order_hash=order_hash,
        payer=order.payer,
        amount=order.amount,
        rejected=False,
    )
