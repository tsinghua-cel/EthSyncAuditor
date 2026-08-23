# F3: EL INVALID 级联方向跨客户端不一致

**ID**: F3  
**严重性**: 🟠 MAJOR  
**场景**: `el_invalid_cascade`  
**受影响客户端**: 全部（实现各异）  
**发现日期**: 2026-06-13  
**核验状态**: ✅ Prysm/Lighthouse/Teku 核心路径已通过本地代码核验

---

## 问题描述

当 `engine_newPayload` 返回 `INVALID` 时，各客户端对 fork choice 树的级联作废方式**方向和粒度各不相同**。这意味着在同一个 INVALID payload 事件下，不同客户端可能会保留或丢弃不同的区块，产生不同的 fork choice head。

---

## 各客户端实现对比

### Prysm — UP 遍历 + 递归删除子树

**数据结构**：双向链表树（`Node` with parent/children 指针）

**文件**：`beacon-chain/forkchoice/doubly-linked-tree/optimistic_sync.go`

```go
// L10: setOptimisticToInvalid — 从 invalid block 向上找到 LVH 所在祖先
// L50: removeNode — 从父节点的 children slice 中移除
// L78: removeNodeAndChildren — 递归作废所有后代，物理删除 nodeByRoot/nodeByPayload
```

**特点**：**物理删除**（从 map 中 delete），不可恢复。LVH 精确匹配，无 LVH 时向上走到 finalized 边界。

---

### Lighthouse — UP 遍历 + FORWARD 全扫描（双 pass）

**数据结构**：ProtoArray（`Vec<Node>` + `HashMap<Hash256, usize>`）

**文件**：`consensus/proto_array/src/proto_array.rs:439`  
**函数**：`propagate_execution_payload_invalidation`

```rust
// Step 1 (UP pass, lines ~450-570):
// 从 head_block_root 向上沿 parent 链遍历
// 遇到之前标记为 Valid 的节点 → 触发 ValidExecutionStatusBecameInvalid 错误

// Step 2 (FORWARD pass, lines ~577-600):
// 从 latest_valid_ancestor_index+1 向前扫描所有 ProtoArray 节点
// 凡是 parent 在 invalidated_indices 中的 → 标记为 Invalid
```

**唯一特殊行为**：`process_invalid_execution_payload()` 检查 justified checkpoint 是否变为 invalid — 若是，触发 **`ShutdownReason::Failure`（客户端关闭）**。

**文件**：`beacon_node/beacon_chain/src/beacon_chain.rs:5849`

---

### Teku — UP 遍历 + markDescendantsAsInvalid

**数据结构**：ProtoArray（Java，`List<VotingNode>` + `Map<Bytes32, Integer>`）

**文件**：`storage/src/main/java/tech/pegasys/teku/storage/protoarray/ProtoArray.java:322`

```java
// markNodeInvalid/markParentChainInvalid:
// 向上走找到 LVH 对应节点 → 标记 INVALID
// → markDescendantsAsInvalid(index) 向下作废后代
// LVH 无效时抛 FatalServiceFailureException
```

---

### Lodestar — ProtoArray 标记（无物理删除）

标记为 `Invalid`，保留在树中，不物理删除。

### Grandine — FORWARD 遍历（不走 UP）

不走 UP 向祖先方向，直接 FORWARD 遍历后代。可能在 LVH 指向错误祖先时行为与其他客户端最不同。

---

## 差异总结

| 客户端 | 方向 | 物理删除 | Valid→Invalid 检测 | justified-invalid 处理 |
|---|---|---|---|---|
| Prysm | UP + 递归DOWN | ✅ 物理删除 | ❌ | ❌ 无 |
| Lighthouse | UP + FORWARD全扫 | ❌ 状态标记 | ✅ 报错 | ✅ **关闭客户端** |
| Teku | UP + markDescendants | ❌ 状态标记 | ❌ | ✅ FatalException |
| Lodestar | ProtoArray标记 | ❌ 状态标记 | ❌ | ❌ 无 |
| Grandine | FORWARD only | ❌ 状态标记 | ❌ | ❌ 无 |

---

## 风险场景

**若 EL 返回错误的 `latestValidHash`（指向过早的祖先）**：

- Prysm 会**物理删除**一批本来有效的区块（不可恢复），然后从错误祖先重建
- Grandine 因为只走 FORWARD 不走 UP，可能与其他客户端作废不同范围的区块
- Lighthouse 是唯一一个在 justified checkpoint 被作废时会**主动关闭自身**的客户端

→ 不同客户端的 fork choice head 可能在此事件后出现**永久分歧**

---

## 注意事项

本发现描述的是**实现差异**，不等于某个客户端有 bug。每种实现在正常 EL 行为下都能正确工作。但在 EL bug 或攻击场景下，差异化行为可能导致网络分裂。建议核心开发者对照 Engine API 规范（`execution-api` spec）明确各步骤的行为要求。
