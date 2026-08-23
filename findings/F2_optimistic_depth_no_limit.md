# F2: 所有客户端无 Optimistic 深度上限

**ID**: F2  
**严重性**: 🔴 CRITICAL  
**场景**: `el_syncing_stuck`  
**受影响客户端**: 全部（程度不同）  
**发现日期**: 2026-06-13  
**核验状态**: ✅ Prysm/Lighthouse 已通过本地代码核验；Teku/Lodestar 已确认有部分防护

---

## 问题描述

当 EL（执行层）长期返回 `SYNCING` 状态时，CL（共识层）将相关区块标记为 optimistic 并继续导入。**没有任何客户端设置了显式的 optimistic 链深度上限**，且 Prysm/Lighthouse/Grandine 在 optimistic head 上**不阻止 validator 操作**（attestation/block proposal）。

---

## 核心代码证据

### Prysm — 仅记日志，不阻止任何操作

**`beacon-chain/blockchain/execution_engine.go`**

```go
// L90: forkchoiceUpdated 收到 SYNCING
case errors.Is(err, execution.ErrAcceptedSyncingPayloadStatus):
    forkchoiceUpdatedOptimisticNodeCount.Inc()  // ← 仅加计数器
    log.WithFields(...).Info("Called fork choice updated with optimistic block")
    return payloadID, nil  // ← 返回 nil（无错误，继续）

// L277: newPayload 收到 SYNCING
if errors.Is(err, execution.ErrAcceptedSyncingPayloadStatus) {
    newPayloadOptimisticNodeCount.Inc()  // ← 仅加计数器
    log.WithFields(logFields).Info("Called new payload with optimistic block")
    return false, nil  // ← 继续导入
}
```

无深度计数，无导入阻止，无 validator 动作阻止。

### Lighthouse — 直接忽略 SYNCING

**`beacon_node/beacon_chain/src/beacon_chain.rs:6294–6297`**

```rust
// The specification doesn't define what to do for a syncing response.
// There's nothing to be done for a syncing response. If the block is already
// `SYNCING` in fork choice, there's nothing to do.
PayloadStatus::Syncing => Ok(()),
```

同样无深度限制，`is_optimistic_or_invalid_head()` 提供查询能力但**不被 validator API 用于阻止投票**。

---

## 各客户端防护对比

| 客户端 | Optimistic 深度限制 | 阻止区块导入 | 阻止 Validator 操作 | 综合 |
|---|---|---|---|---|
| **Prysm** | ❌ 无 | ❌ | ❌ | 🔴 无防护 |
| **Lighthouse** | ❌ 无 | ❌ | ❌ | 🔴 无防护 |
| **Grandine** | ❌ 无 | ❌ | ❌ | 🔴 无防护 |
| **Teku** | ⚠️ 间接（`canOptimisticallyImport`，约 128 slot） | ✅ | ⚠️ head 排除 | 🟡 部分防护 |
| **Lodestar** | ❌ 无 | ❌ | ✅ `NodeIsSyncing` 阻止 | 🟢 最佳（validator 侧） |

**代码引用**：
- Teku `ForkChoice.java:561` — `canOptimisticallyImport()` 检查
- Lodestar validator 侧 `NodeIsSyncing` 状态阻止 duty 执行（待追加代码路径确认）

---

## 风险场景

1. **攻击者持续发送 optimistic 区块**：无深度上限时，可无限累积未经 EL 验证的 optimistic 链。Prysm/Lighthouse/Grandine 的 validator 在此期间继续对 optimistic head 投票 → **对可能错误的链投票**。

2. **EL 软件 bug 导致持续 SYNCING**：正常操作下 EL 长时间未同步完成，CL 节点不感知深度，持续 optimistic 状态 → **finality 无法推进**。

3. **客户端行为不一致**：Teku 在约 128 slot 后阻止导入，而 Prysm/Lighthouse 继续导入 → **同一网络中不同客户端的 optimistic head 深度不同** → 潜在的共识视图分裂。

---

## 修复建议

1. 增加显式 optimistic 深度计数器（推荐上限：64–128 区块）
2. 超过上限后：停止导入新的 optimistic 区块，同时阻止 validator 产生 attestation/proposal
3. 参考 Teku 的 `canOptimisticallyImport()` 和 Lodestar 的 `NodeIsSyncing` 机制合并实现
