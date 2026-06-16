# F6: Lodestar — ACCEPTED 载荷状态被误判为 SYNCED/VALID

**ID**: F6  
**严重性**: 🔴 CRITICAL  
**场景**: EL-3 (`ACCEPTED` vs `SYNCING` 状态处理)  
**受影响客户端**: Lodestar  
**发现日期**: 2026-06-16  
**核验状态**: ✅ 已通过本地代码核验

---

## 问题描述

Lodestar 将 Engine API 的 `ACCEPTED` 响应映射为 `ExecutionEngineState.SYNCED`，与 `VALID` 完全等同。

**规范语义**：`ACCEPTED` 表示 EL 已完成语法验证但**尚未执行**该载荷；`SYNCED` 表示 EL 已完全验证并执行。两者本质不同，Lodestar 将其混同为同一状态。

---

## 代码证据

**文件**: `code/lodestar/packages/beacon-node/src/execution/engine/utils.ts:163–183`

```typescript
function getExecutionEngineStateForPayloadStatus(
    payloadStatus: ExecutionPayloadStatus
): ExecutionEngineState {
    switch (payloadStatus) {
        case ExecutionPayloadStatus.ACCEPTED:       // ← 与 VALID 走同一分支
        case ExecutionPayloadStatus.VALID:
        case ExecutionPayloadStatus.UNSAFE_OPTIMISTIC_STATUS:
            return ExecutionEngineState.SYNCED;     // ACCEPTED = SYNCED = VALID!

        case ExecutionPayloadStatus.SYNCING:        // ← SYNCING 走另一分支
        case ExecutionPayloadStatus.INVALID:
        case ExecutionPayloadStatus.INVALID_BLOCK_HASH:
        case ExecutionPayloadStatus.ELERROR:
            return ExecutionEngineState.SYNCING;
    }
}
```

---

## 安全影响

当 EL 返回 `ACCEPTED` 时：
1. Lodestar 将 `ExecutionEngineState` 设为 `SYNCED`（完全验证）而非 `SYNCING`（乐观）
2. 该区块不会被标记为 optimistic，不计入乐观深度
3. Lodestar 的 validator 会对该**未执行**的区块产生 attestation 和 block proposal
4. 其他客户端（Lighthouse 正确区分 ACCEPTED 与 SYNCING）将该区块视为 optimistic

**跨客户端影响**：EL 频繁返回 `ACCEPTED`（如负载较高时）时，Lodestar 节点认为链已完全验证，其余客户端认为链处于 optimistic 状态，导致不同节点的 fork choice 权重计算不一致。

---

## 对照：Lighthouse 的正确实现

**文件**: `code/lighthouse/beacon_node/execution_layer/src/payload_status.rs:10–101`

```rust
pub enum PayloadStatus {
    Valid,
    Accepted,   // ← 独立 enum variant，与 Valid/Syncing 分开
    Syncing,
    Invalid { validation_error: String },
    ...
}
```

Lighthouse 是五个客户端中**唯一正确区分 ACCEPTED 与 SYNCING** 的实现。两者均作为非致命状态处理（继续导入，区块标记为 optimistic），但保持语义独立。
