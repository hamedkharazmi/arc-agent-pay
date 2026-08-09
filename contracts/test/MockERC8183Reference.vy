#pragma version ==0.4.3
#pragma nonreentrancy on

"""Test-only kernel matching the ERC-8183 published reference ABI profile."""

interface IERC20:
    def transferFrom(owner: address, to: address, amount: uint256) -> bool: nonpayable
    def transfer(to: address, amount: uint256) -> bool: nonpayable


struct Job:
    id: uint256
    client: address
    provider: address
    evaluator: address
    description: String[512]
    budget: uint256
    expiredAt: uint256
    status: uint8
    hook: address


event JobCreated:
    jobId: indexed(uint256)
    client: indexed(address)
    provider: indexed(address)
    evaluator: address
    expiredAt: uint256
    hook: address

event ProviderSet:
    jobId: indexed(uint256)
    provider: indexed(address)

event BudgetSet:
    jobId: indexed(uint256)
    amount: uint256

event JobFunded:
    jobId: indexed(uint256)
    client: indexed(address)
    amount: uint256

event JobSubmitted:
    jobId: indexed(uint256)
    provider: indexed(address)
    deliverable: bytes32

event JobCompleted:
    jobId: indexed(uint256)
    evaluator: indexed(address)
    reason: bytes32

event JobRejected:
    jobId: indexed(uint256)
    rejector: indexed(address)
    reason: bytes32

event JobExpired:
    jobId: indexed(uint256)

event PaymentReleased:
    jobId: indexed(uint256)
    provider: indexed(address)
    amount: uint256

event Refunded:
    jobId: indexed(uint256)
    client: indexed(address)
    amount: uint256


payment_token: immutable(address)
jobs: HashMap[uint256, Job]
job_counter: uint256


@deploy
def __init__(token: address):
    assert token != empty(address), "zero token"
    payment_token = token


@external
@view
def paymentToken() -> address:
    return payment_token


@external
def createJob(
    provider: address,
    evaluator: address,
    expiredAt: uint256,
    description: String[512],
    hook: address,
) -> uint256:
    assert evaluator != empty(address), "zero evaluator"
    assert expiredAt > block.timestamp, "bad expiry"
    assert hook == empty(address), "hooks unsupported"
    self.job_counter += 1
    job_id: uint256 = self.job_counter
    self.jobs[job_id] = Job(
        id=job_id,
        client=msg.sender,
        provider=provider,
        evaluator=evaluator,
        description=description,
        budget=0,
        expiredAt=expiredAt,
        status=0,
        hook=hook,
    )
    log JobCreated(
        jobId=job_id,
        client=msg.sender,
        provider=provider,
        evaluator=evaluator,
        expiredAt=expiredAt,
        hook=hook,
    )
    return job_id


@external
def setProvider(jobId: uint256, provider: address):
    job: Job = self.jobs[jobId]
    assert job.id != 0, "invalid job"
    assert job.status == 0, "wrong status"
    assert msg.sender == job.client, "unauthorized"
    assert job.provider == empty(address), "provider set"
    assert provider != empty(address), "zero provider"
    self.jobs[jobId].provider = provider
    log ProviderSet(jobId=jobId, provider=provider)


@external
def setBudget(jobId: uint256, amount: uint256, optParams: Bytes[1024]):
    job: Job = self.jobs[jobId]
    assert job.id != 0, "invalid job"
    assert job.status == 0, "wrong status"
    assert msg.sender == job.provider, "unauthorized"
    self.jobs[jobId].budget = amount
    log BudgetSet(jobId=jobId, amount=amount)


@external
def fund(jobId: uint256, optParams: Bytes[1024]):
    job: Job = self.jobs[jobId]
    assert job.id != 0, "invalid job"
    assert job.status == 0, "wrong status"
    assert msg.sender == job.client, "unauthorized"
    assert job.provider != empty(address), "provider not set"
    assert block.timestamp < job.expiredAt, "expired"
    self.jobs[jobId].status = 1
    if job.budget > 0:
        assert extcall IERC20(payment_token).transferFrom(job.client, self, job.budget)
    log JobFunded(jobId=jobId, client=job.client, amount=job.budget)


@external
def submit(jobId: uint256, deliverable: bytes32, optParams: Bytes[1024]):
    job: Job = self.jobs[jobId]
    assert job.id != 0, "invalid job"
    assert job.status == 1 or (job.status == 0 and job.budget == 0), "wrong status"
    assert msg.sender == job.provider, "unauthorized"
    self.jobs[jobId].status = 2
    log JobSubmitted(jobId=jobId, provider=job.provider, deliverable=deliverable)


@external
def complete(jobId: uint256, reason: bytes32, optParams: Bytes[1024]):
    job: Job = self.jobs[jobId]
    assert job.id != 0, "invalid job"
    assert job.status == 2, "wrong status"
    assert msg.sender == job.evaluator, "unauthorized"
    self.jobs[jobId].status = 3
    if job.budget > 0:
        assert extcall IERC20(payment_token).transfer(job.provider, job.budget)
    log JobCompleted(jobId=jobId, evaluator=job.evaluator, reason=reason)
    log PaymentReleased(jobId=jobId, provider=job.provider, amount=job.budget)


@external
def reject(jobId: uint256, reason: bytes32, optParams: Bytes[1024]):
    job: Job = self.jobs[jobId]
    assert job.id != 0, "invalid job"
    previous: uint8 = job.status
    if previous == 0:
        assert msg.sender == job.client, "unauthorized"
    else:
        assert previous == 1 or previous == 2, "wrong status"
        assert msg.sender == job.evaluator, "unauthorized"
    self.jobs[jobId].status = 4
    if (previous == 1 or previous == 2) and job.budget > 0:
        assert extcall IERC20(payment_token).transfer(job.client, job.budget)
        log Refunded(jobId=jobId, client=job.client, amount=job.budget)
    log JobRejected(jobId=jobId, rejector=msg.sender, reason=reason)


@external
def claimRefund(jobId: uint256):
    job: Job = self.jobs[jobId]
    assert job.id != 0, "invalid job"
    assert job.status == 1 or job.status == 2, "wrong status"
    assert block.timestamp >= job.expiredAt, "not expired"
    self.jobs[jobId].status = 5
    if job.budget > 0:
        assert extcall IERC20(payment_token).transfer(job.client, job.budget)
        log Refunded(jobId=jobId, client=job.client, amount=job.budget)
    log JobExpired(jobId=jobId)


@external
@view
def getJob(jobId: uint256) -> Job:
    return self.jobs[jobId]
