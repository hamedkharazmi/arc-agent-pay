#pragma version ==0.4.3
#pragma nonreentrancy on

"""Test-only token with the EIP-3009 receiver-safety and nonce semantics."""

balances: public(HashMap[address, uint256])
authorization_state: public(HashMap[address, HashMap[bytes32, bool]])

SECP256K1_HALF_N: constant(uint256) = 57896044618658097711785492504343953926418782139537452191302581570759080747168
DOMAIN_TYPE_HASH: constant(bytes32) = 0x8b73c3c69bb8fe3d512ecc4cf759cc79239f7b179b0ffacaa9a75d522b39400f
DOMAIN_NAME_HASH: constant(bytes32) = 0xd6aca1be9729c13d677335161321649cccae6a591554772516700f986f942eaa
DOMAIN_VERSION_HASH: constant(bytes32) = 0xad7c5bef027816a800da1736444fb58a807ef4c9603b7848673f7e3a68eb14a5
RECEIVE_TYPE_HASH: constant(bytes32) = 0xd099cc98ef71107a616c4f0f941f04c322d8e254fe26b3c6668db87aae413de8

DOMAIN_SEPARATOR: immutable(bytes32)


@deploy
def __init__():
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
def mint(to: address, amount: uint256):
    self.balances[to] += amount


@external
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
):
    # EIP-3009's receive variant prevents a relayer from front-running the payee.
    assert msg.sender == _to, "caller must be payee"
    assert block.timestamp > _valid_after, "authorization not active"
    assert block.timestamp < _valid_before, "authorization expired"
    assert not self.authorization_state[_from][_nonce], "authorization used"
    assert self.balances[_from] >= _value, "insufficient balance"
    assert _v == 27 or _v == 28, "bad signature v"
    assert convert(_s, uint256) > 0 and convert(_s, uint256) <= SECP256K1_HALF_N, "bad signature s"
    authorization_hash: bytes32 = keccak256(
        abi_encode(
            RECEIVE_TYPE_HASH,
            _from,
            _to,
            _value,
            _valid_after,
            _valid_before,
            _nonce,
        )
    )
    digest: bytes32 = keccak256(
        concat(b"\x19\x01", DOMAIN_SEPARATOR, authorization_hash)
    )
    assert ecrecover(digest, _v, _r, _s) == _from, "invalid signature"
    self.authorization_state[_from][_nonce] = True
    self.balances[_from] -= _value
    self.balances[_to] += _value


@external
def transfer(_to: address, _value: uint256) -> bool:
    assert self.balances[msg.sender] >= _value, "insufficient balance"
    self.balances[msg.sender] -= _value
    self.balances[_to] += _value
    return True
