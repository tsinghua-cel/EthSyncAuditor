# F5: Prysm — notifyForkchoiceUpdate 无递归深度限制

**ID**: F5  
**严重性**: 🟠 MAJOR  
**场景**: `el_invalid_cascade`  
**受影响客户端**: Prysm  
**发现日期**: 2026-06-14  
**核验状态**: ✅ 已通过本地代码核验（直接读取递归路径）

> **注**: 早期报告曾描述"零 latestValidHash → 整棵子树删除"，代码核验后该说法**不成立**（代码有 RESET 保护，见附录）。本条记录另一个独立发现：递归本身无深度限制。

---

## 问题描述

Prysm 的 `notifyForkchoiceUpdate` 在遇到 `ErrInvalidPayloadStatus`（EL 返回 INVALID）时，会**递归调用自身**以对新的 head 执行 fcU，且没有任何深度计数器或最大递归限制。

---

## 代码证据

**文件**: `code/prysm/beacon-chain/blockchain/execution_engine.go`

```go
// L38: 函数签名
func (s *Service) notifyForkchoiceUpdate(ctx context.Context, arg *fcuConfig) (*enginev1.PayloadIDBytes, error) {

    // L98: EL 返回 INVALID 路径
    case errors.Is(err, execution.ErrInvalidPayloadStatus):
        // ... 删除 invalid block, 获取新 head r ...
        pid, err := s.notifyForkchoiceUpdate(ctx, &fcuConfig{  // ← L133: 递归调用自身
            headState:  st,
            headRoot:   r,
            headBlock:  b,
            attributes: arg.attributes,
        })
        if err != nil {
            return nil, err  // ← 注释："Returning err because it's recursive here."
        }
```

开发者注释 `// Returning err because it's recursive here.` 明确承认了递归存在。

---

## 触发条件与风险

**递归终止条件**：EL 对新的 head 返回 SUCCESS（不再返回 INVALID）。

**无终止保护**：如果 fork choice 中连续存在 N 个 INVALID 区块，函数会递归 N 层。

| 递归深度 | 触发条件 | 风险 |
|---|---|---|
| 1–3 层 | 普通 EL bug，少量区块被重标为 INVALID | 正常处理，影响可控 |
| 10–50 层 | EL 在大范围 optimistic 链上返回 INVALID | 大量重复 fcU 调用，阻塞主处理 goroutine |
| N 层（理论上） | 攻击者控制 EL，使每次新 head 的 fcU 也返回 INVALID | goroutine 栈累积，CPU 饥饿（Go 动态栈不会溢出，但性能严重下降） |

**注**: Go 的 goroutine 栈是动态增长的，不会像 C 那样 stack overflow，但深度递归会导致内存占用和延迟显著增加，可能阻塞 block 处理。

---

## 对照：Lighthouse 的处理

Lighthouse 在 `propagate_execution_payload_invalidation` 中用 FORWARD pass 批量作废所有后代，**不递归调用自身**，避免了这个问题。

---

## 修复建议

添加递归深度计数器，超过阈值（如 10）后：
1. 记录 ERROR 日志
2. 触发 `allTipsAreInvalid` 降级模式或进入安全关闭

---

## 附录：零 LVH sentinel 说法的核验结果

早期报告称"零 lastValidHash → sentinel `0xff...ff` → 所有祖先被作废"。代码核验证明此说法**不成立**：

```go
// execution_engine.go:33
var defaultLatestValidHash = bytesutil.PadTo([]byte{0xff}, 32)

// optimistic_sync.go: setOptimisticToInvalid
// 当 lastValidHash = 0xff...ff 时：
// 1. UP 循环走到树根 (parent == nil)
// 2. 触发 if firstInvalid.parent == nil: firstInvalid = node  ← RESET
// 3. removeNode(node) → 只删除原始 invalid block 及其后代

// 模拟验证结果：
// After loop: firstInvalid = A (root), parent = None
// RESET: firstInvalid = C (original invalid block)
// → removeNode(C) removes C and its children ONLY
```

零 LVH 的实际行为是：只删除直接 invalid 的 head block，不级联删除祖先。行为与"unknown LVH → only invalidate head"的语义一致，不构成独立 bug。
