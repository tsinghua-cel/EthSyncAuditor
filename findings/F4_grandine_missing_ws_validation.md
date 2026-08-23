# F4: Grandine — 完全缺失弱主观性周期验证

**ID**: F4  
**严重性**: 🔴 CRITICAL  
**场景**: `checkpoint_ws_violation`  
**受影响客户端**: Grandine  
**发现日期**: 2026-06-14  
**核验状态**: ✅ 已通过本地代码核验（代码直读 + 全代码库 grep）

---

## 问题描述

Grandine 的 checkpoint sync 实现在从远端节点拉取 finalized block/state 时，**完全没有执行弱主观性（Weak Subjectivity）周期检查**。攻击者可以提供任意旧的 finalized checkpoint，Grandine 会无条件接受并从该点开始同步，从而陷入与主链永久分歧的恶意链。

---

## 代码证据

### checkpoint_sync.rs — 直接获取，无验证

**文件**: `code/grandine/fork_choice_control/src/checkpoint_sync.rs:22–62`

```rust
pub async fn load_finalized_from_remote<P: Preset>(
    config: &Config,
    client: &Client,
    url: &RedactingUrl,
) -> Result<FinalizedCheckpoint<P>> {
    info_with_peers!("performing checkpoint sync from {url}…");

    let mut block = fetch_block(config, client, url, BlockId::Finalized).await?
        .ok_or(Error::NoFinalizedBlock)?;
    // ... 对 epoch boundary 做对齐处理 ...
    let state = fetch_state(config, client, url, StateId::Slot(slot)).await?
        .ok_or(Error::MissingPostState { block_root })?;

    Ok(FinalizedCheckpoint { block, state })
    // ← 直接返回，无任何 WS 周期检查
}
```

函数获取 finalized block 和 state 后直接返回，**没有**：
1. 计算 WS 周期（`ws_period`）
2. 验证 `checkpoint_epoch + ws_period >= current_epoch`
3. 拒绝超出 WS 窗口的 checkpoint

### 全代码库 grep 确认零实现

```bash
$ grep -r "weak_subjectivity|ws_period|WEAK_SUBJECTIVITY|WeakSubjectivity" code/grandine/ --include="*.rs" | grep -v "test"
# → 零匹配
```

`min_validator_withdrawability_delay` 在 Grandine 中**只用于**状态转换中的 validator 退出逻辑（`electra/block_processing.rs`、`mutators.rs`），从未用于 checkpoint 合法性判断。

---

## 对照：其他客户端的实现

| 客户端 | WS 周期计算 | 策略 |
|---|---|---|
| **Teku** | ✅ 完整实现 `WeakSubjectivityCalculator.java:61–121` | 三级策略：lenient/moderate/strict（最后 `System.exit(2)`） |
| **Prysm** | ✅ `beacon-chain/sync/checkpoint/` | 检查 `checkpoint_epoch + wsPeriod >= currentEpoch` |
| **Lighthouse** | ✅ `beacon_node/network/src/sync/backfill_sync/` | WS 验证集成于 backfill sync |
| **Lodestar** | ⚠️ 部分（信任 provider） | `checkIfCheckpointSyncedAndValidate()` 存在但依赖外部提供 |
| **Grandine** | ❌ **零实现** | 无任何 WS 相关代码 |

---

## 攻击场景

1. 攻击者运行一个 beacon API 服务，返回若干年前的 finalized checkpoint（合法签名，但早于 WS 窗口）
2. 用户将该 URL 配置为 Grandine 的 `--checkpoint-sync-url`
3. Grandine 接受该 checkpoint，从历史点开始同步
4. 节点的 validator 对攻击者控制的链投票 → **双重投票（slash）** 或 **永远在错误链上**

---

## 修复建议

实现 WS 周期计算，参考 Teku 的 `WeakSubjectivityCalculator`：
- 从 checkpoint state 中读取 `active_validator_count` 和 `total_validator_balance`
- 计算 `ws_period`，验证 `checkpoint.epoch + ws_period >= current_epoch`
- 不满足时拒绝 checkpoint 并返回错误
