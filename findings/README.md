# EthSyncAuditor — 已确认发现索引

本目录保存经本地代码核验后**可直接引用**的发现。每条发现均附有精确的文件/行号证据，已通过自动化脚本验证文件存在性和关键词命中。

## 发现列表

| ID | 标题 | 严重性 | 受影响客户端 | 来源 | 状态 |
|---|---|---|---|---|---|
| [F1](F1_prysm_slashing_sign_order.md) | Prysm 签名先于 Slashing 检查 | 🔴 CRITICAL | Prysm | scenario_slashing_sign_order | 已核验 |
| [F2](F2_optimistic_depth_no_limit.md) | 所有客户端无 Optimistic 深度上限 | 🔴 CRITICAL | Prysm, Lighthouse, Grandine（部分：Teku, Lodestar） | scenario_el_syncing_stuck | 已核验 |
| [F3](F3_el_invalid_cascade_divergence.md) | EL INVALID 级联方向跨客户端不一致 | 🟠 MAJOR | 全部（行为各异） | scenario_el_invalid_cascade | 已核验 |
| [F4](F4_grandine_missing_ws_validation.md) | Grandine 完全缺失弱主观性周期验证 | 🔴 CRITICAL | Grandine | deep_dive_grandine | 已核验 |
| [F5](F5_prysm_fcU_unbounded_recursion.md) | Prysm notifyForkchoiceUpdate 无递归深度限制 | 🟠 MAJOR | Prysm | deep_dive_prysm | 已核验 |

## 说明

- **已核验**：文件路径存在、关键词在指定行号附近命中、代码逻辑已人工阅读确认。
- 来源报告位于 `results/scenario_*.md` 和 `results/deep_dive_*.md`。
- 审核日期：2026-06-13（F1-F3）、2026-06-14（F4-F5）。

## 本次审核否决的报告声明

| 报告来源 | 声明 | 否决原因 |
|---|---|---|
| deep_dive_prysm | "零 LVH → 所有祖先被作废，整棵子树删除" | 代码有 `firstInvalid = node` RESET 保护，实际只删除 head block |

