#pragma version ==0.4.3
#pragma nonreentrancy on

"""Test-only ERC-20 subset with controllable false-return behavior."""

balances: public(HashMap[address, uint256])
allowances: public(HashMap[address, HashMap[address, uint256]])
fail_transfer: public(bool)
fail_transfer_from: public(bool)


@external
def mint(to: address, amount: uint256):
    self.balances[to] += amount


@external
def approve(spender: address, amount: uint256) -> bool:
    self.allowances[msg.sender][spender] = amount
    return True


@external
def set_failures(transfer_fails: bool, transfer_from_fails: bool):
    self.fail_transfer = transfer_fails
    self.fail_transfer_from = transfer_from_fails


@external
def transfer(to: address, amount: uint256) -> bool:
    if self.fail_transfer:
        return False
    assert self.balances[msg.sender] >= amount, "insufficient balance"
    self.balances[msg.sender] -= amount
    self.balances[to] += amount
    return True


@external
def transferFrom(owner: address, to: address, amount: uint256) -> bool:
    if self.fail_transfer_from:
        return False
    assert self.allowances[owner][msg.sender] >= amount, "insufficient allowance"
    assert self.balances[owner] >= amount, "insufficient balance"
    self.allowances[owner][msg.sender] -= amount
    self.balances[owner] -= amount
    self.balances[to] += amount
    return True
