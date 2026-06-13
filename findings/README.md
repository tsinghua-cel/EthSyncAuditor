# EthSyncAuditor — 已确认发现索引

本目录保存经本地代码核验后**可直接引用**的发现。每条发现均附有精确的文件/行号证据，已通过自动化脚本验证文件存在性和关键词命中。

## 发现列表

| ID | 标题 | 严重性 | 受影响客户端 | 状态 |
|---|---|---|---|---|
| [F1](F1_prysm_slashing_sign_order.md) | Prysm 签名先于 Slashing 检查 | 🔴 CRITICAL | Prysm | 已核验 |
| [F2](F2_optimistic_depth_no_limit.md) | 所有客户端无 Optimistic 深度上限 | 🔴 CRITICAL | Prysm, Lighthouse, Grandine（部分：Teku, Lodestar） | 已核验 |
| [F3](F3_el_invalid_cascade_divergence.md) | EL INVALID 级联方向跨客户端不一致 | 🟠 MAJOR | 全部（行为各异） | 已核验 |

## 说明

- **已核验**：文件路径存在、关键词在指定行号附近命中、代码逻辑已人工阅读确认。
- 来源报告位于 `results/scenario_*.md`。
- 审核过程详见 2026-06-13 会话记录。

## 待审核（下次分析后补充）

占位，下次运行后填入。
