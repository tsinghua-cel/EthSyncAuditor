# Ethereum CL Multi-Client Network Anomaly Scenarios

**Purpose**: This document defines 18 concrete abnormal scenarios for Ethereum
consensus-layer multi-client analysis. For each scenario, analysts should search
the source code of all five clients (Prysm/Go, Lighthouse/Rust, Grandine/Rust,
Teku/Java, Lodestar/TypeScript) and identify how each client handles the
situation, with emphasis on cases where behavior differs.

**Client source paths**:
- `code/prysm/`
- `code/lighthouse/`
- `code/grandine/`
- `code/teku/`
- `code/lodestar/`

**Analysis requirements for each scenario**:
1. Identify the relevant code path in each client (file + function + line range)
2. Describe what each client does when the scenario triggers
3. Flag any client that lacks handling or has a different behavior from the majority
4. Assess whether the divergence can cause: (a) consensus split, (b) liveness
   failure, (c) validator slashing, or (d) resource exhaustion
5. All evidence must cite real file paths and line numbers from the local source

---

## Category 1: Fork Choice Divergence

### FC-1: Proposer Boost Timing Skew

**Trigger**: A block arrives between 0 and 4 seconds into the current slot
(the proposer boost window). Due to local clock differences across nodes
(±200 ms is normal), some nodes apply proposer boost to this block and others
do not.

**Protocol reference**: `is_proposer_boost_applicable` depends on whether
`arrival_time < slot_start + SECONDS_PER_SLOT / INTERVALS_PER_SLOT`.
`INTERVALS_PER_SLOT = 3`, so the window is the first 4 seconds of each slot.

**Questions to answer**:
- How does each client compute `arrival_time` for a gossip block?
- Is the wall-clock timestamp measured at gossip receive, at validation start,
  or at import?
- Does any client apply proposer boost for blocks arriving after the window
  (delayed due to validation queue)?
- What is each client's clock synchronization strategy (NTP, custom)?

**Risk**: Different fork choice weights → different canonical heads → network
split on the 4-second boundary.

---

### FC-2: Unrealized Justification Processing Order

**Trigger**: A block arrives that, when processed, would advance the justified
checkpoint. Some clients apply "unrealized" justification eagerly (as soon as
enough attestations are seen), others lazily (only after calling `get_head()`).

**Protocol reference**: EIP-3675 / consensus spec section on
`store.unrealized_justified_checkpoint` and `store.unrealized_finalized_checkpoint`.
`get_head()` calls `update_checkpoints()` which may raise the justified checkpoint
before returning the canonical head.

**Questions to answer**:
- Does each client maintain `unrealized_justified_checkpoint` separately from
  `justified_checkpoint`?
- When does `update_checkpoints()` / its equivalent get called relative to
  `on_block()`?
- Can two clients simultaneously disagree on `store.justified_checkpoint` even
  though they have seen the same blocks?
- Is there a race between block import and `get_head()` that could produce
  transient disagreement?

**Risk**: Clients with different justified checkpoints reject attestations from
each other → persistent fork.

---

### FC-3: Equivocating Proposer — Block Arrival Order

**Trigger**: A proposer double-signs and two different valid blocks for the same
slot propagate across the network. Different nodes receive them in different
orders. The first-seen block becomes the "preferred" head for fork choice.

**Questions to answer**:
- Does each client's `on_block()` path apply a "first-seen wins" rule or a
  deterministic tie-break rule for equivocating blocks at the same slot?
- When both blocks have equal weight, how does each client break the tie
  (root lexicographic order? arrival time? random?)?
- Does any client update fork choice weights differently after seeing both blocks?
- Are equivocation proofs (slashing evidence) generated and broadcast
  immediately, or deferred?

**Risk**: Different canonical heads → attesters on different forks → chain split.

---

### FC-4: INVALID Cascade Invalidates Justified Checkpoint

**Trigger**: EL returns `INVALID` for a payload, and the `latestValidHash`
points to a block BEFORE the current `store.justified_checkpoint.root`. The
cascade of invalidation therefore renders the justified checkpoint invalid.

**Protocol reference**: Optimistic sync spec section "Forkchoice-Updated
Execution Optimistic Sync" — if the justified checkpoint root becomes invalid,
the node is in an unrecoverable state.

**Questions to answer**:
- Does each client detect when the justified checkpoint block is in the
  invalidated set?
- What action does each client take: shutdown, enter degraded mode, attempt
  to revert to a prior justified checkpoint, or continue silently?
- Is the check performed synchronously during `SetOptimisticToInvalid`, or
  asynchronously?
- What happens if `latestValidHash` is zero/empty in this scenario?

**Risk**: Client shutdown (Lighthouse confirmed), silent incorrect operation
(others), or chain split between clients that shut down vs those that continue.

---

## Category 2: Epoch Boundary Edge Cases

*Note*: Epoch boundary scenarios are the highest-priority investigation area.
Focus especially on `process_epoch`, `process_slots`, justification, and
attestation validation across the epoch transition.

### EB-1: Epoch-Boundary Block and process_epoch Execution Order

**Trigger**: A block arrives whose `slot` satisfies `slot % SLOTS_PER_EPOCH == 0`
(the first slot of a new epoch). Per the spec, `process_slots` must call
`process_epoch` before `process_block` for this block. If any client applies
a shortcut that skips or defers `process_epoch`, the resulting `state_root`
will differ from the spec.

**Questions to answer**:
- In each client's block import path, where is `process_slots` called relative
  to `process_block`? Is there a fast path that skips `process_epoch`?
- Is `process_epoch` called exactly once per epoch boundary slot, or can it
  be called multiple times (or zero times)?
- Are there caching optimizations (e.g., pre-computing the epoch transition
  for the "expected" next block) that could produce a different state if an
  unexpected block arrives at that slot?
- Does each client correctly handle the case where the epoch-boundary block
  is the first block of a new fork (i.e., its parent's `state_root` is an
  epoch-start state)?

**Risk**: `state_root` mismatch → block is VALID for some clients, INVALID
for others → immediate chain split.

---

### EB-2: Empty Slot Accumulation Across Multiple Epochs

**Trigger**: No block is proposed for N consecutive epochs (e.g., 5 epochs =
160 slots) due to network partition or validator outage. When the first block
finally arrives, `process_slots` must execute the epoch transitions for all
missed epochs, including: inactivity leak scoring, `process_rewards_and_penalties`,
`process_registry_updates`, effective balance updates, committee cache rollover.

**Questions to answer**:
- Is there a maximum number of empty slots each client will process in a single
  `process_slots` call? Is there a timeout or chunk limit?
- Does each client produce bitwise-identical results for inactivity leak
  calculation across N empty epochs? Look for any floating-point operations,
  integer division rounding, or branch conditions that could differ.
- How does each client handle `process_registry_updates` when many validators
  are being activated/exited simultaneously (churn limit interactions)?
- Is there a DoS risk where an attacker triggers long `process_slots` chains
  by withholding blocks?

**Risk**: State root divergence during catch-up → chain split; CPU exhaustion
DoS on node restart.

---

### EB-3: Cross-Epoch-Boundary Attestation Target Validity

**Trigger**: An attestation is produced in the last slot of epoch K (`slot = K*32 + 31`)
with `target.epoch = K`. Due to variable network latency, this attestation
may arrive at a receiving node after the node has already advanced to epoch K+1.

**Protocol reference**: `validate_attestation` checks:
- `attestation.data.target.epoch == get_current_epoch(state) OR get_previous_epoch(state)`
- `attestation.data.slot + MIN_ATTESTATION_INCLUSION_DELAY <= state.slot`

**Questions to answer**:
- Does each client correctly accept "previous epoch" attestations (`target.epoch = K`)
  when the current epoch is `K+1`?
- Is there a timing window where a node has locally advanced to epoch `K+1`
  but the attestation's `target.epoch = K` is rejected as "too old"?
- For gossip validation of attestations, does each client use a clock-based
  cutoff or a slot-based cutoff?
- Are "previous epoch" attestations included in block production by all clients?

**Risk**: Attestations from epoch K rejected by some nodes when epoch K+1 starts
→ reduced attestation inclusion rate → slower finality.

---

### EB-4: Sync Committee Period Rotation Overlap

**Trigger**: At the start of a new `SYNC_COMMITTEE_PERIOD` (every 256 epochs),
the active sync committee changes. Sync committee messages from the LAST slot
of the old period and from the FIRST slot of the new period may arrive in any
order due to network delay.

**Protocol reference**: `compute_next_sync_committee()` is called during
`process_sync_committee_updates()` inside `process_epoch`. The new committee
becomes active at `period_start * EPOCHS_PER_SYNC_COMMITTEE_PERIOD`.

**Questions to answer**:
- Does each client correctly identify which sync committee period applies for
  a sync committee message based on the message's `slot`?
- Is there a race where a client that has not yet processed the epoch-boundary
  block tries to validate a sync committee message using the OLD committee?
- How does each client's sync committee cache handle the transition — is it
  updated atomically with the state transition?
- What happens if sync committee messages for both the old and new period
  arrive simultaneously?

**Risk**: Sync aggregate not included in block → lower sync committee reward →
liveness degradation; in extreme cases, different clients accept/reject the
same aggregate.

---

## Category 3: P2P / Gossip Layer Anomalies

### GS-1: Blob Sidecar Arrives Before Corresponding Block

**Trigger**: Post-Deneb, `blob_sidecar` and `beacon_block` propagate on separate
gossip topics with no ordering guarantee. A blob sidecar for block root R may
arrive 0.5–2 seconds before the block itself.

**Protocol reference**: The beacon node must validate blob sidecars against
the block's `kzg_commitments`. Without the block, only the KZG proof can be
partially validated.

**Questions to answer**:
- Does each client buffer blob sidecars that arrive before their corresponding
  block? What is the maximum buffer size and TTL?
- What is each client's timeout for waiting for a block after receiving its
  blob sidecars (or vice versa)?
- If the blob arrives first and the block arrives later, does the combined
  validation happen correctly, or does the blob get discarded and need to be
  re-fetched?
- Are there any race conditions between blob buffering and block import that
  could cause a block to be imported without its blobs being available?

**Risk**: Block imported without blob availability verification → clients
disagree on whether a block satisfies data availability → fork.

---

### GS-2: Attestation Arriving on Wrong Subnet

**Trigger**: After a fork, the attestation subnet assignment may differ between
the two forks if the validator set diverges (due to deposits/exits processed
differently). Attestations from fork A validators may arrive on fork B nodes
on the wrong subnet.

**Questions to answer**:
- Does each client re-validate attestation subnet assignments when processing
  gossip attestations from a peer that is on a different fork?
- Is `compute_subscribed_subnets` in each client purely a function of
  `node_id + epoch`, or does it also depend on the current state?
- After a reorg, does each client re-subscribe to the correct subnets for
  the new canonical fork?

**Risk**: Attestations silently lost across forks → reduced attestation
coverage → slower finality after reorg.

---

### GS-3: Unknown Parent Block Flood (Orphan Amplification)

**Trigger**: An attacker broadcasts blocks with fabricated `parent_root` values
that do not exist in any honest node's database. Each receiving node must
store the block, issue a `BeaconBlocksByRoot` request for the parent, wait,
and eventually give up. Different clients have different limits on how many
such requests they will issue and how long they retain orphaned blocks.

**Questions to answer**:
- What is each client's maximum orphan block pool size (blocks with unknown parent)?
- How long does each client wait for the parent before discarding the orphan?
- Does each client limit how many `BeaconBlocksByRoot` requests it issues per
  peer per slot to prevent amplification?
- Does sending an unknown-parent block cause any peer penalty?

**Risk**: Memory exhaustion from unbounded orphan pool; bandwidth exhaustion
from cascading parent requests.

---

## Category 4: Validator Client Anomalies

### VC-1: Doppelganger Detection Window Race

**Trigger**: An operator restarts their validator. The new instance starts
before the old instance has fully terminated. The two instances may simultaneously
attempt to perform attestation duties in the same epoch.

**Questions to answer**:
- What is each client's doppelganger detection timeout (number of epochs to
  wait before assuming no other instance is running)?
- Does the detection rely on observing one's own attestation on-chain, or
  on a liveness endpoint?
- If slashing protection DB is on shared storage and both instances use the
  same DB, is concurrent access handled safely?
- What is the window between "old instance exits" and "new instance starts
  signing" — is it zero, one slot, or one epoch?

**Risk**: Both instances sign the same slot's attestation → double vote → slash.

---

### VC-2: Remote Signer (Web3Signer) Timeout During Duty Execution

**Trigger**: The validator uses an external signing service (Web3Signer or
similar). During duty execution, the HTTP signing request times out. The client
must decide: skip the duty (miss), retry (risk duplicate), or fail permanently.

**Questions to answer**:
- What is each client's HTTP timeout for remote signing requests?
- On timeout, does each client retry the signing request? If so, what is the
  retry policy and is the slashing protection DB consulted before the retry?
- If the signer eventually responds to the first request and the client has
  already given up, could there be a race between two in-flight signing
  requests for the same duty?
- Is there a mechanism to detect that the remote signer signed successfully
  (response received) before the local timeout fires?

**Risk**: Duplicate signing of same slot/epoch if retry is not idempotent.

---

### VC-3: Aggregator Selection Based on Stale Committee Data

**Trigger**: The validator client calls the beacon node API to fetch committee
data for aggregation. If this call happens within milliseconds of an epoch
boundary, the beacon node may return the committee for the new epoch while
the validator is computing selection proof for the previous epoch's slot.

**Questions to answer**:
- Does each client explicitly bind committee queries to a specific epoch, or
  does it use the "current" epoch at the time of the API response?
- If committee data is fetched at slot N-1 for use at slot N, and an epoch
  transition occurs between fetch and use, is the stale committee data detected
  and refreshed?
- Is `is_aggregator` (the selection proof check) evaluated by the validator
  client locally, or delegated to the beacon API?

**Risk**: Validator incorrectly believes it is (or is not) an aggregator →
missed aggregation duties → reduced aggregate coverage for the epoch.

---

## Category 5: Execution Layer (CL-EL) Interaction Anomalies

### EL-1: Concurrent forkchoiceUpdated Calls — Serialization Guarantee

**Trigger**: Two code paths simultaneously call `engine_forkchoiceUpdated`:
(1) the block import goroutine after a new head is set, and (2) the validator
payload-building goroutine preparing for the next slot's proposal. If the calls
interleave at the EL, the EL may receive them in a different order than the
CL intended.

**Questions to answer**:
- Does each client serialize `engine_forkchoiceUpdated` calls through a single
  queue or mutex? Or can concurrent calls be issued?
- If two concurrent calls are sent, does the client handle a race where EL
  updates its head to the second call's value before the first call completes?
- Is there a case where the payload ID from the first `forkchoiceUpdated` is
  used by `engine_getPayload` even though the second `forkchoiceUpdated` has
  already superseded it?

**Risk**: Block proposal built on wrong parent state → block rejected by EL →
missed proposal.

---

### EL-2: Engine API Version Mismatch After EL Upgrade/Downgrade

**Trigger**: The EL is upgraded or rolled back during operation, changing the
set of supported Engine API methods. The CL's cached capabilities from
`engine_exchangeCapabilities` become stale. The CL calls `engine_newPayloadV3`
but the EL now only supports V2, returning `METHOD_NOT_FOUND`.

**Questions to answer**:
- Does each client cache the results of `engine_exchangeCapabilities` and if
  so, when does it refresh? On every block? On every restart? On error?
- On `METHOD_NOT_FOUND`, does each client automatically retry with a lower
  version, or does it fail the block import?
- If the EL supports V3 but the block is pre-Deneb (should use V2), does each
  client correctly downgrade the API call?

**Risk**: Block import fails on all nodes simultaneously if EL is upgraded →
chain stall; inconsistent behavior if only some nodes detect the version change.

---

### EL-3: ACCEPTED vs SYNCING Payload Status Handling

**Trigger**: EL returns `ACCEPTED` (not `SYNCING`) from `engine_newPayload`.
Per the Engine API spec, `ACCEPTED` means "block is syntactically valid but
execution has not been attempted yet." Different clients treat `ACCEPTED`
differently: some equate it with `SYNCING` (optimistic import), others treat
it as success.

**Protocol reference**: Engine API spec v1.0.0-alpha.5+, `PayloadStatusV1`:
`ACCEPTED` is distinct from `SYNCING`. The spec says the CL MUST treat it
as optimistic.

**Questions to answer**:
- Does each client distinguish between `ACCEPTED` and `SYNCING` in its
  payload status handling?
- If `ACCEPTED` is returned, does the block get imported as optimistic?
- Does `ACCEPTED` count toward the optimistic depth (if any)?
- Can an EL exploit `ACCEPTED` to keep blocks permanently in "accepted but
  not validated" limbo without ever resolving to VALID or INVALID?

**Risk**: Divergent optimistic depth calculations across clients → different
nodes have different optimistic chain lengths → potential fork when EL finally
resolves the payload status.

---

## Category 6: State Persistence and Recovery

### DB-1: Hot/Cold State Boundary Inconsistency After Crash

**Trigger**: Finalization triggers a state migration from the "hot" in-memory
or recent-block store to the "cold" archival store. If the process crashes
mid-migration (e.g., after writing to cold but before updating the hot/cold
boundary index, or vice versa), the next startup cannot locate states at
certain block heights.

**Questions to answer**:
- Is the hot/cold state migration in each client atomic (single DB transaction)
  or multi-step?
- On restart after a mid-migration crash, does each client detect and recover
  from the inconsistency, or does it fail to start?
- Does each client validate that the hot/cold boundary is consistent with the
  finalized checkpoint on startup?
- What states are absolutely required on startup (finalized state,
  justified state, head state), and what happens if any are missing?

**Risk**: Node fails to restart after crash → manual intervention required;
state reconstruction from genesis in worst case.

---

### DB-2: Optimistic Payload Status Lost on Restart

**Trigger**: A client has imported N optimistic blocks (EL returned SYNCING).
The process restarts. The optimistic payload status is not persisted to disk
(known issue in Grandine, possibly others). On restart, the client does not
know which blocks were optimistic, so it cannot re-issue `engine_newPayload`
for them or correctly mark them.

**Questions to answer**:
- Does each client persist payload status (`VALID`/`SYNCING`/`INVALID`/`OPTIMISTIC`)
  for each imported block to durable storage?
- On restart, does each client re-validate optimistic blocks by calling
  `engine_newPayload` for each unresolved block?
- If the EL has already pruned the corresponding execution payload (e.g.,
  due to deep pruning), can the CL ever resolve the payload status?
- Is there a maximum number of "unresolved optimistic" blocks that can
  accumulate, and what happens when this limit is reached?

**Risk**: Blocks permanently stuck in unverified optimistic state after restart
→ fork choice cannot advance past these blocks → node stall.

---

## Analysis Priority Recommendation

| Priority | Scenario | Rationale |
|---|---|---|
| 🔴 Highest | EB-1, EB-2 | Direct state_root divergence risk at epoch boundary |
| 🔴 Highest | FC-2 | Unrealized justification is a known source of past incidents |
| 🔴 Highest | FC-4 | Directly extends confirmed finding F3 |
| 🟠 High | GS-1 | Post-Deneb blob ordering is new, each client strategy differs |
| 🟠 High | VC-1, VC-2 | Directly extends confirmed finding F1 (slashing risk) |
| 🟠 High | EL-3 | ACCEPTED vs SYNCING extends confirmed finding F2 |
| 🟡 Medium | EB-3, EB-4 | Subtle epoch boundary attestation validation |
| 🟡 Medium | DB-2 | Grandine has confirmed TODO for this exact issue |
| 🟡 Medium | EL-1 | Serialization differs across clients |

---

*This document is an input for automated client code analysis. All cited
protocol references refer to the ethereum/consensus-specs repository.*
