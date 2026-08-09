#pragma version ==0.4.3
#pragma nonreentrancy on

"""Test-only ERC-20 subset used by ERC-8183 lifecycle tests."""

balances: public(HashMap[address, uint256])
allowances: public(HashMap[address, HashMap[address, uint256]])


@external
def mint(to: address, amount: uint256):
    self.balances[to] += amount


@external
def approve(spender: address, amount: uint256) -> bool:
    self.allowances[msg.sender][spender] = amount
    return True


@external
def transfer(to: address, amount: uint256) -> bool:
    assert self.balances[msg.sender] >= amount, "insufficient balance"
    self.balances[msg.sender] -= amount
    self.balances[to] += amount
    return True


@external
def transferFrom(owner: address, to: address, amount: uint256) -> bool:
    assert self.allowances[owner][msg.sender] >= amount, "insufficient allowance"
    assert self.balances[owner] >= amount, "insufficient balance"
    self.allowances[owner][msg.sender] -= amount
    self.balances[owner] -= amount
    self.balances[to] += amount
    return True
