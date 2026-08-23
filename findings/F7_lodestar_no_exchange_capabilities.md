# F7: Lodestar — 不调用 Engine API exchangeCapabilities

**ID**: F7  
**严重性**: 🔴 CRITICAL  
**场景**: EL-2 (Engine API 版本协商)  
**受影响客户端**: Lodestar  
**发现日期**: 2026-06-16  
**核验状态**: ✅ 已通过本地代码核验（全文件 grep 零匹配）

---

## 问题描述

Lodestar 从不调用 `engine_exchangeCapabilities`，完全依赖 fork 版本（Deneb→V2、Electra→V3）来选择 Engine API 方法。当 EL 被升级、降级或回滚时，Lodestar 无法感知版本变化，收到 `METHOD_NOT_FOUND` 后**没有任何降级/重试机制**。

---

## 代码证据

**文件**: `code/lodestar/packages/beacon-node/src/execution/engine/http.ts`

```bash
$ grep "exchangeCapabilities" code/lodestar/packages/beacon-node/src/execution/engine/http.ts
# → 零匹配
```

全文件搜索结果：文件中**不存在** `exchangeCapabilities` 的任何调用。方法版本由 fork 名称静态决定：

```typescript
// 版本选择完全基于 fork，无运行时能力协商
if (fork >= ForkName.electra) {
    return this.engineNewPayloadV4(params);
} else if (fork >= ForkName.deneb) {
    return this.engineNewPayloadV3(params);
}
// ... 无 fallback，无 METHOD_NOT_FOUND 处理
```

---

## 对照：各客户端能力协商实现

| 客户端 | 调用 exchangeCapabilities | 基于能力路由 | 失败降级 |
|---|---|---|---|
| **Lodestar** | ❌ **从不调用** | ❌ | ❌ |
| **Prysm** | ✅ 连接时调用 | ❌（fork-based） | ❌ |
| **Grandine** | ✅ 每 10 epochs | ❌ | ❌ |
| **Teku** | ✅ 每 10 epochs | ❌（milestone） | ❌ |
| **Lighthouse** | ✅ 每次连接 + 15min 缓存 | ✅ V3→V2→V1 | ✅ 完整降级链 |

---

## 安全影响

**场景**：EL 从支持 V3 降回 V2（回滚或配置变更）：

1. Lodestar 继续发送 `engine_newPayloadV3`
2. EL 返回 `METHOD_NOT_FOUND`
3. Lodestar **无降级路径**，block import 失败
4. 节点无法验证新区块，停止参与共识

**Lighthouse 对比**：收到 `METHOD_NOT_FOUND` 后自动尝试 V2、V1，返回明确的 `RequiredMethodUnsupported` 错误供运维人员处理。

---

## 修复建议

在启动和重连时调用 `engine_exchangeCapabilities`，缓存支持的方法列表，并在选择 API 版本时优先参考缓存结果而非 fork 名称，参考 Lighthouse 实现。
