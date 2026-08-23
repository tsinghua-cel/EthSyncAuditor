# Logical Synchronization Graph (LSG) Schema Specification

Version: 1  
Generated at: 2026-03-08T07:58:08Z

This document defines a **common, machine-readable schema** for Logical Synchronization Graphs (LSGs)
that all client agents (Lighthouse, Prysm, Teku, Grandine, etc.) MUST use when describing
high-level workflows for comparison.

The corresponding on-disk representation is a single YAML file per client, typically named:

- `LSG_<ClientName>_Sync.yml` (or more generally `LSG_<ClientName>.yml`)

All clients MUST adhere to the structure and naming rules in this spec so that LSGs can be
compared automatically.

---

## 1. Top-Level YAML Structure

```yaml
version: <int>          # Schema version. For this spec: 1
client: <string>        # Client identifier, e.g. "prysm", "lighthouse", "teku", "grandine"
generated_at: <string>  # RFC3339 timestamp; informational only, ignored for diff/comparison

guards:                 # Global guard vocabulary (Σ)
  - name: <GuardName>
    category: <string>      # e.g. network|time|validation|mode|exec|validator|state
    description: <string>   # Human-readable meaning

actions:                # Global action vocabulary (Γ)
  - name: <ActionName>
    category: <string>      # e.g. network|sync|block|peer|schedule|exec|validator
    description: <string>   # Human-readable meaning

workflows:              # Set of LSGs (G_i), one per business workflow
  - id: <WorkflowId>        # Stable identifier shared across clients
    name: <string>          # Human-readable name
    description: <string>
    mode: <string>          # Short tag, e.g. InitialSync, RegularSync
    initial_state: <StateId>
    states:
      - id: <StateId>       # Unique within this workflow, recommended: prefix.phase
        label: <string>     # Human-readable label
        category: <string>  # e.g. init|peer_select|request|wait|validate|import|peer|recover|progress|terminal
        transitions:
          - guard: <GuardName|TRUE>
            actions: [<ActionName>, ...]
            next_state: <StateId>
            evidence:               # OPTIONAL (recommended in concrete client files)
              file: <string>        # Relative path to source file, e.g. beacon-chain/sync/service.go
              function: <string>    # Function or method name, e.g. "(s *Service) runInitialSync"
              lines: [<int>, <int>] # [start_line, end_line], 1-based inclusive
```

### 1.1. Required vs Optional Fields

- `version`, `client`, `guards`, `actions`, `workflows` are REQUIRED.  
- `generated_at` is OPTIONAL but RECOMMENDED; comparison tools MUST ignore differences in this field.  
- Within `workflows[*].states[*].transitions[*]`, `evidence` is OPTIONAL and may be omitted when
  the mapping to code is not yet stabilized.

---

## 2. Global Guard Vocabulary (guards)

`guards` define the **conditions** that govern state transitions (Σ).  
All agents SHOULD reuse the same guard names wherever possible so workflows
can be compared across different clients.

Example guards (non-exhaustive, but RECOMMENDED baseline set):

```yaml
guards:
  # Network / input
  - name: RespRecv
    category: network
    description: Response received from a peer (range, checkpoint, blob, etc.).
  - name: RespInvalid
    category: network
    description: Response or payload structurally or cryptographically invalid.
  - name: GossipRecvBlock
    category: network
    description: New block received via gossip.
  - name: GossipRecvAttestation
    category: network
    description: New attestation received via gossip.
  - name: PeerDisconnected
    category: network
    description: Peer disconnected during or before a request/response.
  - name: NewPeerAvailable
    category: network
    description: At least one eligible peer is available for selection.

  # Time / retry
  - name: TimeoutExpired
    category: time
    description: Hard timeout for a pending network or execution request has fired.
  - name: BackoffExpired
    category: time
    description: Previously scheduled backoff delay has elapsed.

  # Validation / state
  - name: MissingParent
    category: validation
    description: Block references a parent that is not yet known/imported.
  - name: AlreadyKnown
    category: validation
    description: Block or attestation already known or imported.
  - name: ForkChoiceReject
    category: validation
    description: Fork-choice rule rejects importing the candidate block.
  - name: ReachedTargetSlot
    category: state
    description: Local sync slot has reached or exceeded the current target slot.

  # Mode / configuration
  - name: ModeIsInitialSync
    category: mode
    description: Node is in initial sync mode (pre-regular sync, including backfill).
  - name: ModeIsRegularSync
    category: mode
    description: Node is in regular gossip-based sync mode.
  - name: ModeIsCheckpointSync
    category: mode
    description: Node is in checkpoint-based bootstrap sync mode.
  - name: ModeIsOptimistic
    category: mode
    description: Node performs optimistic execution payload import.

  # Execution-layer related
  - name: ExecutionClientSyncing
    category: exec
    description: Execution client is not fully synced; payloads may be imported optimistically.
  - name: ExecutionValidationSucceeded
    category: exec
    description: Execution client reports successful validation of an execution payload.
  - name: ExecutionValidationFailed
    category: exec
    description: Execution client reports failed validation of an execution payload.

  # Validator / duties
  - name: HasProposerDuty
    category: validator
    description: Local validator has a block proposal duty in this slot.
  - name: HasAttesterDuty
    category: validator
    description: Local validator has an attestation duty in this slot.
  - name: SelectedAsAggregator
    category: validator
    description: Local validator has been selected as an aggregator for a committee.
```

Agents MAY extend this list with client-specific guards, but SHOULD prefer reusing
names from this baseline when the semantics match.

---

## 3. Global Action Vocabulary (actions)

`actions` define the **effects** of transitions (Γ).  
All agents SHOULD reuse the same action names wherever possible.

Example actions (non-exhaustive, but RECOMMENDED baseline set):

```yaml
actions:
  # Network
  - name: SendRangeRequest
    category: network
    description: Send BeaconBlocksByRange (or equivalent) request to a peer.
  - name: SendCheckpointRequest
    category: network
    description: Request blocks or states around a finalized checkpoint.
  - name: SendStatus
    category: network
    description: Send status/handshake message to peer.
  - name: SendGoodbye
    category: network
    description: Send a goodbye message before disconnecting.
  - name: DisconnectPeer
    category: network
    description: Disconnect and optionally blacklist a peer.
  - name: SubscribeGossip
    category: network
    description: Subscribe to gossip topics required for regular sync.

  # Sync control
  - name: UpdateSyncTarget
    category: sync
    description: Update internal sync target slot or root from peer information.
  - name: BuildPeerQueue
    category: sync
    description: Build or refresh priority queue of sync peers.
  - name: EnterRegularSync
    category: sync
    description: Switch internal state machine into regular sync mode.

  # Block / batch processing
  - name: ValidateBatch
    category: block
    description: Validate a batch of blocks from a range or checkpoint response.
  - name: ApplyBatch
    category: block
    description: Apply a batch of blocks and advance state/fork-choice.
  - name: StoreBlock
    category: block
    description: Persist block without necessarily making it canonical head.
  - name: ValidateBlock
    category: block
    description: Validate an individual block against consensus rules.
  - name: ApplyBlock
    category: block
    description: Apply a single block and update state.
  - name: UpdateForkChoice
    category: block
    description: Update fork-choice structures and canonical head.
  - name: MarkBlockInvalid
    category: block
    description: Mark block(s) as invalid in local store.
  - name: RequestParents
    category: block
    description: Request parent blocks needed to process a child.

  # Peer management
  - name: PenalizePeer
    category: peer
    description: Decrease peer reputation score due to misbehavior or timeout.
  - name: UpdatePeerScore
    category: peer
    description: Recompute or persist updated peer score.
  - name: SelectNextPeer
    category: peer
    description: Select next peer from queue according to scoring heuristics.

  # Scheduling / retry
  - name: ScheduleTimeout
    category: schedule
    description: Schedule hard timeout for a request or operation.
  - name: ScheduleBackoff
    category: schedule
    description: Schedule a backoff delay before retrying an operation.

  # Execution / optimistic
  - name: ApplyOptimisticBlock
    category: exec
    description: Apply block with execution payload without waiting for full EL validation.
  - name: MarkPayloadPending
    category: exec
    description: Record payload awaiting execution-layer validation.
  - name: TriggerExecutionValidation
    category: exec
    description: Ask execution client to validate one or more pending payloads.
  - name: RollbackToSafeHead
    category: exec
    description: Roll back canonical head to last safe (finalized/justified) point.
  - name: MarkPayloadInvalid
    category: exec
    description: Mark execution payload as invalid and avoid re-import.

  # Validator / attestation / block production
  - name: FetchDuties
    category: validator
    description: Query beacon node for validator duties for upcoming slots/epochs.
  - name: BuildAttestation
    category: validator
    description: Construct attestation using committee assignment and head state.
  - name: SignAttestation
    category: validator
    description: Sign attestation with validator key.
  - name: PublishAttestation
    category: validator
    description: Submit attestation to beacon node for gossip/broadcast.
  - name: BuildBlock
    category: validator
    description: Construct block (with execution payload if applicable) for a slot.
  - name: SignBlock
    category: validator
    description: Sign block with proposer key.
  - name: PublishBlock
    category: validator
    description: Submit signed block to beacon node for gossip and import.
  - name: ComputeAggregate
    category: validator
    description: Aggregate attestations or sync committee messages.
  - name: SignAggregate
    category: validator
    description: Sign aggregated attestation or sync aggregate.
  - name: PublishAggregate
    category: validator
    description: Submit signed aggregate to beacon node.
```

Again, agents MAY extend this with client-specific actions, but SHOULD reuse
baseline names when semantics align.

---

## 4. Workflows (business-level LSGs)

Each entry in `workflows` represents one high-level **business workflow**.
For cross-client comparison, the following seven `id` values are RESERVED and SHOULD
be implemented by all client agents:

1. `initial_sync`  
2. `regular_sync`  
3. `checkpoint_sync`  
4. `attestation_generate`  
5. `block_generate`  
6. `aggregate`  
7. `execute_layer_relation`  

### 4.1. Common fields

For each workflow:

```yaml
workflows:
  - id: <one-of-the-7-ids>
    name: <HumanReadableName>
    description: <Longer description of the workflow>
    mode: <ShortModeName>            # e.g. InitialSync, RegularSync, etc.
    initial_state: <StateId>
    states:
      - id: <StateId>
        label: <string>
        category: <string>
        transitions:
          - guard: <GuardName|TRUE>
            actions: [<ActionName>, ...]
            next_state: <StateId>
            evidence:        # OPTIONAL
              file: <string>
              function: <string>
              lines: [<int>, <int>]
```

### 4.2. State and Transition Naming

- `StateId` SHOULD be namespaced by workflow prefix for clarity, e.g. `initial.peer_select`, `regular.idle`.  
- `category` is free-form but SHOULD use a small, shared vocabulary where possible, such as:
  - `init`, `peer_select`, `request`, `wait`, `validate`, `import`, `recover`, `peer`, `progress`, `idle`, `receive`, `build`, `sign`, `publish`, `compute`, `error`, `terminal`.  
- `guard` MUST either be a known `GuardName` from `guards` or the special literal `TRUE` for
  unconditional transitions.  
- `actions` MUST be drawn from the `actions` list; it MAY be empty (`[]`).

### 4.3. Evidence (Optional, Client-Specific)

`evidence` allows mapping a transition back to concrete implementation code.

- It is OPTIONAL in this schema: early versions of LSGs may omit it.  
- When present, all three fields SHOULD be provided:
  - `file`: relative path from the repo root to the implementing file.  
  - `function`: function or method name implementing this transition.  
  - `lines`: a 2-element array `[start, end]` (1-based, inclusive) giving an approximate
    line range that implements the transition.

Example transition with evidence:

```yaml
    transitions:
      - guard: RespRecv
        actions: [ValidateBatch]
        next_state: initial.import_batch
        evidence:
          file: beacon-chain/sync/initial-sync/service.go
          function: "(s *Service) handleRangeResponse"
          lines: [120, 210]
```

Comparison tools MAY ignore `evidence` when comparing high-level structures,
using it only to backtrack to source code for debugging or further analysis.

### 4.4. Workflow Business Semantics

Below is a detailed description of each reserved workflow's business meaning, the Ethereum
protocol interactions it involves, the typical guards and actions it uses, and implementation
hints across the five reference clients. Agents MUST use this as the authoritative reference
when discovering vocabulary (Phase 1) and extracting state machines (Phase 2).

#### 4.4.1. `initial_sync` — Initial Synchronization

When a beacon node starts for the first time (or after a long offline period), it must download
blocks from genesis (or from a recent finalized epoch) up to the current chain head. The node
uses the `BeaconBlocksByRange` RPC (and `BlobSidecarsByRange` post-Deneb) to request large
batches of sequential blocks from peers selected by their advertised `head_slot`. Requests are
pipelined across multiple peers for throughput. Each batch is validated (BLS signatures,
state transitions, consensus rules), applied to the local beacon state, and the fork-choice
view is advanced. The loop continues until the local head reaches the network target slot,
at which point the node switches to `regular_sync`.

**Typical guards**: `NewPeerAvailable`, `RespRecv`, `RespInvalid`, `TimeoutExpired`,
`PeerDisconnected`, `MissingParent`, `ReachedTargetSlot`, `ModeIsInitialSync`,
`ForkChoiceReject`, `AlreadyKnown`

**Typical actions**: `BuildPeerQueue`, `SelectNextPeer`, `SendRangeRequest`, `ScheduleTimeout`,
`ValidateBatch`, `ApplyBatch`, `UpdateForkChoice`, `PenalizePeer`, `DisconnectPeer`,
`UpdateSyncTarget`, `EnterRegularSync`

**Implementation hints**:
- Prysm (Go): `beacon-chain/sync/initial-sync/` — `blocksFetcher`, FSM in `*Service`.
- Lighthouse (Rust): `beacon_node/network/src/sync/range_sync/` — `RangeSync`, `SyncingChain`.
- Grandine (Rust): `fork_choice_control/` — integrated sync manager with range requests.
- Teku (Java): `beacon/sync/` — `ForwardSync`, `SyncManager`, `PeerSync`.
- Lodestar (TypeScript): `packages/beacon-node/src/sync/range/` — `RangeSync`, `SyncChain`.

#### 4.4.2. `regular_sync` — Regular (Gossip) Synchronization

After initial sync completes, the node maintains chain head by subscribing to GossipSub
topics: `beacon_block`, `beacon_attestation` (per-subnet), `beacon_aggregate_and_proof`,
`voluntary_exit`, `proposer_slashing`, `attester_slashing`, `blob_sidecar` (post-Deneb).
Every 12-second slot, the node receives a new block via gossip, performs initial validity
checks (slot, proposer, signature), then runs full consensus validation and imports it.
Attestations update fork-choice weights. If the node detects it has fallen multiple
slots/epochs behind, it falls back to range-based `initial_sync`.

**Typical guards**: `GossipRecvBlock`, `GossipRecvAttestation`, `RespRecv`, `RespInvalid`,
`MissingParent`, `AlreadyKnown`, `ForkChoiceReject`, `TimeoutExpired`, `ModeIsRegularSync`

**Typical actions**: `SubscribeGossip`, `ValidateBlock`, `ApplyBlock`, `UpdateForkChoice`,
`StoreBlock`, `RequestParents`, `PenalizePeer`, `UpdatePeerScore`, `ScheduleTimeout`

**Implementation hints**:
- Prysm (Go): `beacon-chain/sync/` — gossip handlers `receiveBlock`, `receiveAttestation`; `blockchain.ReceiveBlock()`.
- Lighthouse (Rust): `beacon_node/network/src/sync/` — `NetworkBeaconProcessor` dispatches gossip events.
- Grandine (Rust): `fork_choice_control/` + `eth2_libp2p/` — event loop processing gossip messages.
- Teku (Java): `networking/eth2/src/main/java/.../gossip/` — handlers feed `BlockImporter`, `AttestationManager`.
- Lodestar (TypeScript): `packages/beacon-node/src/chain/` — `BeaconChain` gossip event handlers.

#### 4.4.3. `checkpoint_sync` — Checkpoint (Weak Subjectivity) Synchronization

Checkpoint sync lets a node bootstrap quickly without downloading from genesis. The node
fetches a recent finalized state and block from a trusted source (Beacon API endpoint
`/eth/v2/debug/beacon/states/finalized` or a bundled checkpoint), verifies the state root
against a known weak subjectivity checkpoint, and initializes its local beacon state and
fork-choice from that anchor. It then syncs forward to the current head (via range sync
or gossip). Optionally, the node *backfills* historical blocks from the checkpoint toward
genesis using reverse `BeaconBlocksByRange` requests, validating parent-child chain
integrity without full state transitions.

**Typical guards**: `RespRecv`, `RespInvalid`, `TimeoutExpired`, `ModeIsCheckpointSync`,
`ReachedTargetSlot`, `MissingParent`, `NewPeerAvailable`

**Typical actions**: `SendCheckpointRequest`, `ValidateBatch`, `ApplyBatch`,
`UpdateForkChoice`, `SendRangeRequest`, `ScheduleTimeout`, `EnterRegularSync`,
`UpdateSyncTarget`

**Implementation hints**:
- Prysm (Go): `beacon-chain/sync/checkpoint/` — `--checkpoint-sync-url` flag; `initialsync` handles forward sync.
- Lighthouse (Rust): `beacon_node/src/cli.rs` + `beacon_node/network/src/sync/backfill_sync/` — `BackFillSync`.
- Grandine (Rust): `grandine/src/` — checkpoint initialization path, then standard sync.
- Teku (Java): `beacon/sync/` — `WeakSubjectivitySync` / `CheckpointSync` init pipeline.
- Lodestar (TypeScript): `packages/beacon-node/src/sync/` — `--checkpointSyncUrl`, backfill sync.

#### 4.4.4. `attestation_generate` — Attestation (Vote) Generation

Every slot, a subset of validators is assigned attestation duties. Each attesting validator
queries the beacon node for its committee assignment (`/eth/v1/validator/duties/attester/{epoch}`),
waits until 1/3 into the slot (4 seconds — the prescribed attestation time), fetches the
current attestation data (`/eth/v1/validator/attestation_data`) containing source/target
(Casper FFG) and head (LMD-GHOST) votes, constructs the `Attestation` object with its
committee bit set, signs it with its BLS private key (after slashing-protection checks to
avoid conflicting attestations), and publishes the signed attestation to the appropriate
gossip subnet via the beacon node.

**Typical guards**: `HasAttesterDuty`, `TRUE`, `TimeoutExpired`, `RespRecv`

**Typical actions**: `FetchDuties`, `BuildAttestation`, `SignAttestation`,
`PublishAttestation`, `ScheduleTimeout`

**Implementation hints**:
- Prysm (Go): `validator/client/attest.go` — `submitAttestation()`, `createAttestation()`.
- Lighthouse (Rust): `validator_client/src/attestation_service.rs` — `AttestationService`.
- Grandine (Rust): validator crate — attestation production in the validator event loop.
- Teku (Java): `validator/client/src/main/java/.../duties/` — `AttestationProductionDuty`.
- Lodestar (TypeScript): `packages/validator/src/services/attestation.ts` — `AttestationService`.

#### 4.4.5. `block_generate` — Block Proposal (Production)

Each slot, exactly one validator is the designated block proposer. The proposer queries duties
(`/eth/v1/validator/duties/proposer/{epoch}`), then at slot start: (1) calls
`engine_forkchoiceUpdated` with `payloadAttributes` to tell the EL to start building an
execution payload, (2) calls `engine_getPayload` to retrieve the built execution payload
(transactions, withdrawals, etc.), (3) assembles the full beacon block body — attestations,
deposits, proposer/attester slashings, sync committee contributions, the execution payload,
BLS-to-execution changes, and blob KZG commitments (post-Deneb), (4) signs the block with
the proposer's BLS key (with slashing protection — never sign two blocks for the same slot),
and (5) broadcasts the signed block via gossip while importing it locally.

**Typical guards**: `HasProposerDuty`, `TRUE`, `ExecutionValidationSucceeded`,
`ExecutionValidationFailed`, `TimeoutExpired`, `RespRecv`

**Typical actions**: `FetchDuties`, `TriggerExecutionValidation`, `BuildBlock`,
`SignBlock`, `PublishBlock`, `ScheduleTimeout`

**Implementation hints**:
- Prysm (Go): `validator/client/propose.go` — `ProposeBlock()`; beacon-side `proposer.go`.
- Lighthouse (Rust): `validator_client/src/block_service.rs` — `BlockService`; `beacon_node/execution_layer/`.
- Grandine (Rust): `block_producer/` — block assembly + EL payload retrieval.
- Teku (Java): `validator/client/src/.../duties/BlockProductionDuty.java`.
- Lodestar (TypeScript): `packages/validator/src/services/block.ts` — `BlockProposingService`.

#### 4.4.6. `aggregate` — Attestation Aggregation

For each committee in each slot, one or more validators are selected as *aggregators* via a
VRF-based check (`is_aggregator()` on the slot signature). The aggregator subscribes to its
committee's attestation subnet, waits until 2/3 into the slot (8 seconds) to let individual
attestations propagate, collects them, combines them into a single `AggregateAndProof`
(bitwise-OR of aggregation bits + BLS signature combination), signs the aggregate, and
publishes it to the global `beacon_aggregate_and_proof` gossip topic. This reduces the
volume of individual attestations that block producers must process and include.

**Typical guards**: `SelectedAsAggregator`, `TRUE`, `GossipRecvAttestation`,
`TimeoutExpired`

**Typical actions**: `SubscribeGossip`, `ComputeAggregate`, `SignAggregate`,
`PublishAggregate`, `ScheduleTimeout`, `FetchDuties`

**Implementation hints**:
- Prysm (Go): `validator/client/aggregate.go` — `SubmitAggregateAndProof()`.
- Lighthouse (Rust): `validator_client/src/attestation_service.rs` — aggregation interleaved with attestation.
- Grandine (Rust): aggregation pool in the validator event loop.
- Teku (Java): `validator/client/src/.../duties/AggregationDuty.java`.
- Lodestar (TypeScript): `packages/validator/src/services/attestation.ts` — combined attestation + aggregation.

#### 4.4.7. `execute_layer_relation` — Execution Layer Interaction

Post-Merge, the CL drives the EL via the Engine API (authenticated JSON-RPC). Key interactions:

1. **Payload validation** — When the CL receives a new block (gossip or sync), it sends the
   execution payload to the EL via `engine_newPayload`. The EL executes all transactions and
   returns `VALID`, `INVALID`, `SYNCING`, or `ACCEPTED`.
2. **Fork-choice update** — After block import, the CL calls `engine_forkchoiceUpdated` with
   the current head/safe/finalized hashes. This optionally includes `payloadAttributes` to
   trigger building the next block's execution payload.
3. **Optimistic sync** — If the EL is still syncing (`SYNCING` status), the CL may import
   blocks optimistically without full EL validation, marking them as "optimistic". These
   are retroactively validated when the EL catches up.
4. **Invalid payload handling** — If the EL returns `INVALID`, the CL must invalidate the
   block and all its descendants, potentially rolling back the fork-choice head to the last
   valid ancestor.

**Typical guards**: `ExecutionValidationSucceeded`, `ExecutionValidationFailed`,
`ExecutionClientSyncing`, `ModeIsOptimistic`, `TimeoutExpired`, `RespRecv`

**Typical actions**: `TriggerExecutionValidation`, `ApplyOptimisticBlock`,
`MarkPayloadPending`, `MarkPayloadInvalid`, `RollbackToSafeHead`, `UpdateForkChoice`,
`ScheduleTimeout`

**Implementation hints**:
- Prysm (Go): `beacon-chain/execution/` — `ExecutionEngine`; `beacon-chain/blockchain/` — `notifyNewPayload()`, `notifyForkchoiceUpdate()`.
- Lighthouse (Rust): `beacon_node/execution_layer/src/` — `ExecutionLayer`, `notify_new_payload()`, `notify_forkchoice_updated()`.
- Grandine (Rust): `execution_engine/` — Engine API client; `fork_choice_control/` — EL response integration.
- Teku (Java): `ethereum/executionlayer/src/` — `ExecutionLayerManager`; `ethereum/statetransition/` — optimistic status.
- Lodestar (TypeScript): `packages/beacon-node/src/execution/engine/` — `ExecutionEngineHttp`; `packages/beacon-node/src/chain/` — `verifyBlocksExecutionPayload`.

---

## 5. Example: Minimal Initial Sync Workflow Skeleton

This is a **minimal**, schematic example of `initial_sync` in the standard format:

```yaml
version: 1
client: example-client
guards:
  - name: RespRecv
    category: network
    description: Response received from a peer.
  - name: TimeoutExpired
    category: time
    description: Timeout fired.
  - name: NewPeerAvailable
    category: network
    description: At least one peer is available.
  - name: ReachedTargetSlot
    category: state
    description: Reached target slot.

actions:
  - name: SendRangeRequest
    category: network
    description: Request a range of blocks.
  - name: ScheduleTimeout
    category: schedule
    description: Schedule a timeout.
  - name: ApplyBatch
    category: block
    description: Apply a batch of blocks.
  - name: UpdateForkChoice
    category: block
    description: Update fork-choice.

workflows:
  - id: initial_sync
    name: "Initial Synchronization"
    description: "Bootstrap from empty state to target slot."
    mode: InitialSync
    initial_state: initial.peer_select
    states:
      - id: initial.peer_select
        label: "Pick sync peer"
        category: peer_select
        transitions:
          - guard: NewPeerAvailable
            actions: []
            next_state: initial.request_range
      - id: initial.request_range
        label: "Request block range"
        category: request
        transitions:
          - guard: TRUE
            actions: [SendRangeRequest, ScheduleTimeout]
            next_state: initial.wait_response
      - id: initial.wait_response
        label: "Wait for response"
        category: wait
        transitions:
          - guard: RespRecv
            actions: [ApplyBatch, UpdateForkChoice]
            next_state: initial.check_progress
          - guard: TimeoutExpired
            actions: []
            next_state: initial.peer_select
      - id: initial.check_progress
        label: "Check progress"
        category: progress
        transitions:
          - guard: ReachedTargetSlot
            actions: []
            next_state: initial.done
          - guard: TRUE
            actions: []
            next_state: initial.peer_select
      - id: initial.done
        label: "Initial sync complete"
        category: terminal
        transitions: []
```

---

## 6. Usage Guidelines for Agents

1. **One schema, many clients**: all client agents MUST follow this spec so LSGs can be
   compared mechanically across implementations.  
2. **Shared vocabulary first**: before defining a new `guard` or `action`, agents SHOULD
   check whether an equivalent semantic already exists in this document and reuse its name.  
3. **Stable workflow IDs**: the seven reserved `workflows[*].id` values MUST NOT be changed
   or repurposed; client-specific workflows, if needed, should use additional IDs.  
4. **Evidence as refinement**: agents MAY first publish LSGs without `evidence` and
   incrementally add `evidence` entries as code mapping becomes precise.  
5. **Comparison tools**: tooling that compares LSGs across clients SHOULD treat:
   - `version`, `client`, `guards`, `actions`, `workflows[*].id`, `states`, `transitions`
     as comparison-relevant;  
   - `generated_at` and `evidence` as informational (ignored for structural diff).
