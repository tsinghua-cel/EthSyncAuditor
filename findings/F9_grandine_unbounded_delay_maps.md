# F9: Grandine — 三个无界延迟队列（内存 DoS 向量）

**ID**: F9  
**严重性**: 🟠 MAJOR  
**场景**: GS-3 (孤儿区块洪泛) / GS-1 (Blob 先于 Block 到达)  
**受影响客户端**: Grandine  
**发现日期**: 2026-06-16  
**核验状态**: ✅ 已通过本地代码核验

---

## 问题描述

Grandine 的 `Mutator` 结构体包含三个 `HashMap`，用于缓冲等待处理条件的区块和 blob。这三个 Map **均无容量上限，均无超时/驱逐机制**，持续增长直到 finalization 触发清理为止。

在非最终确定性（non-finality）攻击或网络分区期间，攻击者可以发送大量假造 `parent_root` 的区块，使这些 Map 无限增长，导致 OOM。

---

## 代码证据

**文件**: `code/grandine/fork_choice_control/src/mutator.rs`

```rust
// 三个无界 HashMap（无 capacity 限制，无 TTL）：

// 1. 等待 parent 区块的挂起区块
delayed_until_block: HashMap<H256, DelayedChainLink>,   // L2632

// 2. 等待 blob sidecar 的挂起区块
delayed_until_blobs: HashMap<H256, PendingBlock<P>>,    // L2637

// 3. 等待未来 slot 的挂起区块
delayed_until_slot: HashMap<Slot, DelayedChainLink>,
```

插入代码（无容量检查）：

```rust
fn delay_block_until_blobs(&mut self, beacon_block_root: H256, pending_block: PendingBlock<P>) {
    self.store_mut().delay_block_at_slot(pending_block.block.message().slot(), beacon_block_root);
    self.update_store_snapshot();
    self.delayed_until_blobs.insert(beacon_block_root, pending_block);  // ← 无 capacity 检查
}

fn delay_block_until_parent(&mut self, pending_block: PendingBlock<P>) {
    self.delayed_until_block.insert(..., pending_block);  // ← 无 capacity 检查
}
```

---

## 对照：其他客户端的防护措施

| 客户端 | 最大孤儿数 | 超时/驱逐 | 重试上限 |
|---|---|---|---|
| **Prysm** | 3/slot | 1 epoch | 5次 |
| **Lighthouse** | 200（LruCache） | TTL 驱逐 | 4次 |
| **Teku** | 可配置 | 每 epoch 清理 | N/A |
| **Lodestar** | 100 | 单次超时 | 5次 |
| **Grandine** | **无限 🔴** | **无 🔴** | **无限 🔴** |

---

## 攻击场景

```
攻击者广播 10,000 个区块，每个区块携带随机 parent_root：
  → 每个区块进入 delayed_until_block HashMap
  → 同时对应的 blob sidecar 进入 delayed_until_blobs HashMap
  → 攻击成本：约 ~100KB/区块 → 10K 区块 ≈ 1 GB 内存
  → 无 finality 情况下（非最终确定性攻击期间）：永不清理
```

---

## 注意事项

- 需要 finalization 才能触发清理（`prune_orphans()` 在 finalization 事件上调用）
- 在正常网络（finality 正常）下，孤儿 Map 自然保持较小
- **配合非最终确定性攻击**（如跨 epoch 的网络分区），该 DoS 向量威胁大幅放大
