# F1: Prysm — 签名先于 Slashing 检查

**ID**: F1  
**严重性**: 🔴 CRITICAL  
**场景**: `slashing_sign_order`  
**受影响客户端**: Prysm（其余4个客户端顺序正确）  
**发现日期**: 2026-06-13  
**核验状态**: ✅ 已通过本地代码核验

---

## 问题描述

Prysm 在 attestation 生产和 block proposal 流程中，**先执行 BLS 签名，再执行 slashing 保护数据库检查**。

正确顺序应为：CHECK → WRITE → SIGN

Prysm 实际顺序：SIGN → CHECK（检查此时已无法阻止签名）

---

## 代码证据

### Attestation 流程（`validator/client/attest.go`）

```go
// L94: 先签名
sig, _, err := v.signAtt(ctx, pubKey, data, slot)

// L104–117: 用已生成的签名构建 IndexedAttestation
indexedAtt = &ethpb.IndexedAttestation{
    AttestingIndices: []uint64{uint64(duty.ValidatorIndex)},
    Data:             data,
    Signature:        sig,   // ← 签名已嵌入
}

// L130: 再检查（此时签名已存在于内存中）
if err := v.db.SlashableAttestationCheck(ctx, indexedAtt, pubKey, signingRoot, ...)
```

| 操作 | 行号 |
|---|---|
| `v.signAtt()` — BLS 签名 | L94 |
| 构建 `IndexedAttestation` with sig | L104–L117 |
| `v.db.SlashableAttestationCheck()` | L130 |

**崩溃窗口**：进程在 L94~L130 之间崩溃 → 签名在内存中已完成但 DB 无记录 → 重启后重复签名同一 epoch

### Block Proposal 流程（`validator/client/propose.go`）

```go
// L104: 先签名
sig, signingRoot, err := v.signBlock(ctx, pubKey, epoch, slot, wb)

// L113: 构建带签名的区块
blk, err := blocks.BuildSignedBeaconBlock(wb, sig)

// L119: 再检查
if err := v.db.SlashableProposalCheck(ctx, pubKey, blk, signingRoot, ...)
```

| 操作 | 行号 |
|---|---|
| `v.signBlock()` | L104 |
| `blocks.BuildSignedBeaconBlock(wb, sig)` | L113 |
| `v.db.SlashableProposalCheck()` | L119 |

> **注**：`propose.go:53` 存在 per-pubkey 锁，防止**同进程内**并发重复签名。但该锁对跨进程（双实例运行）和进程崩溃后重启均无保护。

---

## 对照：其他客户端的正确实现

### Teku (`SlashingProtectedSigner.java:59–76`)
```java
// CHECK+WRITE FIRST（async chain，签名操作在 .thenCompose 中）
return slashingProtector
    .maySignAttestation(...)            // ← CHECK+WRITE
    .thenCompose(__ -> delegate.signAttestationData(...));  // ← SIGN
```
`LocalSlashingProtector.handleResult()` 在返回 `true` 前**先写磁盘**，确保原子性。

### Lodestar (`validatorStore.ts:473, 525`)
```typescript
await this.slashingProtection.checkAndInsertBlockProposal(pubkey, {...}); // CHECK+WRITE
// 以上抛异常则不执行以下
signature: await this.getSignature(pubkey, signingRoot, ...);             // SIGN
```

### Grandine (`signer.rs:364–402`)
```rust
let mut protector = slashing_protector.lock().await;
// 批量 validate_and_store 全部通过后再批量签名
protector.validate_and_store_own_attestations(beacon_state, attestations)?;
// ... 所有验证通过后执行签名
```

### Lighthouse
通过 `doppelganger_checked_signing_method()` 封装，signing 前强制 slashing check 完成。

---

## 风险场景

| 触发 | 结果 |
|---|---|
| 进程崩溃（L94 之后，L130 之前） | 同一 slot 被重复签名 → **32 ETH 完全罚没** |
| 双实例运行（配置错误/热备切换） | 两个实例并发签名同一 epoch → **double vote / double proposal** |

---

## 修复建议

将 `SlashableAttestationCheck` 和 `SlashableProposalCheck` 移至签名操作之前执行，参考 Teku 的包装器模式或 Lodestar 的 `checkAndInsert` 模式。
