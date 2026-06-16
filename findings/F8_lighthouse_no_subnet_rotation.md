# F8: Lighthouse — Attestation 子网订阅不随 Epoch 轮换

**ID**: F8  
**严重性**: 🟠 MAJOR  
**场景**: GS-2 (attestation 子网分配)  
**受影响客户端**: Lighthouse  
**发现日期**: 2026-06-16  
**核验状态**: ✅ 已通过本地代码核验 + 对照其他客户端

---

## 问题描述

以太坊规范要求节点使用 `compute_subscribed_subnets(node_id, epoch)` 计算订阅的 attestation 子网，该函数每 `EPOCHS_PER_SUBNET_SUBSCRIPTION`（=256 epoch）轮换一次。

Lighthouse 的实现中**完全缺失 epoch 参数**，仅使用 `node_id` 静态计算子网，导致子网订阅在节点生命周期内**永不轮换**。

---

## 代码证据

**文件**: `code/lighthouse/consensus/types/src/subnet_id.rs:120–122`

```rust
// Lighthouse 的子网计算函数签名 — 没有 epoch 参数
(0..subnets_per_node)
    .map(move |idx| SubnetId::new(
        (node_id_prefix + idx as u64) % attestation_subnet_count
    ))
```

全代码库搜索确认：
```bash
$ grep -r "compute_subscribed_subnets\|EPOCHS_PER_SUBNET_SUBSCRIPTION" code/lighthouse/ --include="*.rs"
# → 零匹配
```

---

## 对照：其他客户端的正确实现

**Prysm** (`beacon-chain/p2p/subnets.go:510–542`) 完整实现规范中的 `compute_subscribed_subnets`：

```go
// def compute_subscribed_subnets(node_id: NodeID, epoch: Epoch):
//   node_offset = node_id % EPOCHS_PER_SUBNET_SUBSCRIPTION
//   permutation_seed = hash(uint_to_bytes(uint64(
//       (epoch + node_offset) // EPOCHS_PER_SUBNET_SUBSCRIPTION
//   )))
```

**Lodestar** 在 `chainConfig` 中正确定义 `EPOCHS_PER_SUBNET_SUBSCRIPTION: 256`，并在子网选择时使用当前 epoch。

---

## 影响

1. **短期**（< 256 epoch）：无影响，静态分配与规范结果一致
2. **长期**（> 256 epoch，约 27.3 天后）：Lighthouse 节点的子网订阅与规范脱轨，其他客户端已按规范轮换到新子网
3. **网络影响**：由于 Lighthouse 约占以太坊主网 35%+ 的验证者份额，大量节点永不轮换会导致：
   - 旧子网流量过载（Lighthouse 节点仍在旧子网上）
   - 新子网覆盖不足（缺少 Lighthouse 节点）
   - 全网 attestation 传播效率下降

---

## 注意事项

Lighthouse 的 **validator client** 仍会根据具体 duty 订阅正确的 attestation 子网（短期订阅，1–2 epoch），这部分不受影响。受影响的是 beacon node 的**长期后台订阅**（long-lived subscriptions），用于维持 gossip mesh 的健康覆盖。
