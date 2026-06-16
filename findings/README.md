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
| [F6](F6_lodestar_accepted_equals_synced.md) | Lodestar ACCEPTED 状态被误判为 SYNCED | 🔴 CRITICAL | Lodestar | multi_client_el_analysis | 已核验 |
| [F7](F7_lodestar_no_exchange_capabilities.md) | Lodestar 不调用 Engine API exchangeCapabilities | 🔴 CRITICAL | Lodestar | multi_client_el_analysis | 已核验 |
| [F8](F8_lighthouse_no_subnet_rotation.md) | Lighthouse Attestation 子网订阅不随 Epoch 轮换 | 🟠 MAJOR | Lighthouse | multi_client_gs_analysis | 已核验 |
| [F9](F9_grandine_unbounded_delay_maps.md) | Grandine 三个无界延迟队列（内存 DoS） | 🟠 MAJOR | Grandine | multi_client_gs_analysis | 已核验 |
| [F10](F10_payload_status_lost_on_restart.md) | Payload 验证状态重启后丢失（3个客户端） | 🟠 MAJOR | Grandine, Lodestar, Prysm | multi_client_db_analysis | 已核验 |

## 说明

- **已核验**：文件路径存在、关键词在指定行号附近命中、代码逻辑已人工阅读确认。
- 来源报告位于 `results/scenario_*.md`、`results/deep_dive_*.md` 和 `results/multi_client_*.md`。
- 审核日期：2026-06-13（F1-F3）、2026-06-14（F4-F5）、2026-06-16（F6-F10）。

## 本次审核否决的报告声明

| 报告来源 | 声明 | 否决原因 |
|---|---|---|
| deep_dive_prysm | "零 LVH → 所有祖先被作废，整棵子树删除" | 代码有 `firstInvalid = node` RESET 保护，实际只删除 head block |
| multi_client_eb_analysis | EB-1/EB-2/EB-3 有"关键差异"标注 | 报告本身结论：所有5个客户端均符合规范，差异为实现风格而非行为差异，不构成 finding |
| multi_client_fc13_analysis | FC-1 "timing vulnerability" | 报告自身结论：所有客户端正确重置 boost，无可利用的时序漏洞 |

## 客户端风险快速参考

| 客户端 | CRITICAL 数 | MAJOR 数 | 主要风险 |
|---|---|---|---|
| **Lodestar** | 3 (F1, F6, F7) | 1 (F10) | Engine API 状态处理、签名顺序、重启 |
| **Grandine** | 2 (F4, F2) | 3 (F9, F10, F3) | WS 验证缺失、内存 DoS、状态持久化 |
| **Prysm** | 2 (F1, F2) | 2 (F5, F10) | 签名顺序、递归、状态持久化 |
| **Lighthouse** | 1 (F2) | 2 (F3, F8) | Optimistic 深度、子网轮换 |
| **Teku** | 0 | 1 (F3) | INVALID 级联语义 |


