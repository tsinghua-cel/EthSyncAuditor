# F10: Payload 验证状态在重启后丢失（3个客户端）

**ID**: F10  
**严重性**: 🟠 MAJOR  
**场景**: DB-2 (optimistic payload status 持久化)  
**受影响客户端**: Grandine、Lodestar、Prysm（Lighthouse 实现正确）  
**发现日期**: 2026-06-16  
**核验状态**: ✅ 已通过本地代码核验

---

## 问题描述

节点崩溃重启后，三个客户端无法恢复此前各区块的 payload 验证状态（VALID/INVALID/OPTIMISTIC）。之前已由 EL 验证为 VALID 的区块，重启后全部变回 OPTIMISTIC，需要重新向 EL 发起 `engine_newPayload` 验证。若 EL 已对这些旧区块的执行状态完成剪枝，则这些区块将永远无法完成验证。

---

## 各客户端代码证据

### Grandine — PayloadStatus 刻意排除 SSZ 序列化

**文件**: `code/grandine/types/src/nonstandard.rs:272–278`

```rust
#[derive(Clone, Copy, PartialEq, Eq, Debug, Serialize)]  // ← 仅 Serialize（serde），无 Encode/Decode（SSZ）
#[serde(rename_all = "lowercase")]
pub enum PayloadStatus {
    Valid,
    Invalid,
    Optimistic,
}
```

`PayloadStatus` 没有 `Encode`/`Decode` derive，不会写入 SSZ 数据库。重启后所有区块的状态从内存消失。

### Lodestar — 重启时全部区块重置为 Syncing

**文件**: `code/lodestar/packages/beacon-node/src/chain/forkChoice/index.ts:142`

```typescript
executionStatus: blockHeader.slot === GENESIS_SLOT
    ? ExecutionStatus.Valid
    : ExecutionStatus.Syncing,  // ← 所有非创世区块强制设为 Syncing
```

Fork choice 在启动时从 checkpoint state 重建，所有非创世区块（100%）从 `Syncing` 开始，不管重启前的状态。

### Prysm — fork choice 完全内存态，无序列化

**文件**: `code/prysm/beacon-chain/forkchoice/doubly-linked-tree/types.go:63`

```go
type Node struct {
    optimistic bool  // ← 仅内存，无持久化
    // ...
}
```

重启时 (`store.go:100`) 所有节点以 `optimistic: true` 初始化，无恢复机制。

---

## 对照：Lighthouse 的完整持久化

**文件**: `code/lighthouse/consensus/proto_array/src/proto_array_fork_choice.rs:34–49`

```rust
#[derive(Clone, Copy, Debug, PartialEq, Encode, Decode, Serialize, Deserialize)]
pub enum ExecutionStatus {
    Valid(ExecutionBlockHash),
    Invalid(ExecutionBlockHash),
    Optimistic(ExecutionBlockHash),
    Irrelevant(bool),
}
```

`Encode`/`Decode` 确保序列化到磁盘。重启策略（`OnlyWithInvalidPayload`，默认）：
- 无 INVALID 区块 → VALID 状态保留，不需重新验证
- 有 INVALID 区块 → 全部重置为 Optimistic（安全兜底）

---

## 风险

**正常重启**（EL 在线且保留全部执行状态）：影响较小，重新验证开销有限。

**高风险场景**：
1. EL 重置/剪枝（如换用新 EL 或 EL 清库）后 CL 重启
2. 崩溃后处于非最终确定性状态（大量 optimistic 区块）
3. 上述情况下，之前已验证的数百个区块永久卡在 OPTIMISTIC 状态

→ Fork choice 无法推进这些区块之后的区块，节点停滞。
