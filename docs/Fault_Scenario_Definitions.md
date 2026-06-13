# Fault Scenario Definitions

本文档定义了 EthSyncAuditor Phase 2.5 场景扫描所使用的 8 个故障场景。

每个场景对应以太坊共识客户端在实际运行中可能遭遇的异常情况。Phase 2.5
在每个 workflow 收敛后立即扫描与之相关的场景，判断各客户端的 LSG 是否已
覆盖该场景的处理逻辑，并将结果标注回 LSG transition 的 `scenario_ids` 字段。

---

## 场景分类概览

| ID | 名称 | 触发条件 | 关联 Workflow | 严重性 |
|---|---|---|---|---|
| `sync_stall` | 同步停滞 | 同步无进展超过 N slot | initial_sync, regular_sync | MAJOR |
| `reorg_during_sync` | 同步中 Reorg | 跨 epoch 边界的链重组 | initial_sync, regular_sync | MAJOR |
| `el_invalid_cascade` | EL INVALID 级联错误 | INVALID + 错误 latestValidHash | execute_layer_relation | CRITICAL |
| `el_syncing_stuck` | EL 持续 SYNCING 锁定 | EL 长期返回 SYNCING | execute_layer_relation, regular_sync | CRITICAL |
| `gossip_flood_ddos` | Gossip DDoS | 恶意 peer 大量发送 gossip 消息 | regular_sync | MAJOR |
| `missing_parent_flood` | 孤儿区块洪泛 | 大量 parent 未知的区块涌入 | regular_sync | MAJOR |
| `slashing_sign_order` | 签名与 Slashing 检查顺序 | 签名操作与 slashing DB 检查的时序关系 | attestation_generate, block_generate | CRITICAL |
| `checkpoint_ws_violation` | Checkpoint 弱主观性违规 | 使用超出 WS 窗口的 checkpoint | checkpoint_sync | CRITICAL |

---

## 场景详细定义

### sync_stall — 同步停滞

**触发条件**：同步进程中长时间（N slot）无新区块确认，节点无法前进。

**风险**：节点卡在过期分叉，错过最终确定性，长期偏离主链。

**期望处理**：
- 检测停滞（超时或无进展计数器）
- 轮换或惩罚当前同步 peer
- 重置同步链或回退到更保守的 sync target

**定向搜索查询**：
```
stall detection sync no progress timeout
sync chain reset backoff retry peer rotation
lastFetchedSlot checkProgress batchTimeout stalled
peer rotation replacement on sync failure
```

**已知客户端差异**（历史运行结果）：grandine 历史上缺少明确的停滞检测机制。

---

### reorg_during_sync — 同步中 Reorg

**触发条件**：initial_sync 过程中发生链重组，尤其是跨越 epoch 边界的深度 reorg。

**风险**：
- sync target 不一致，客户端继续向错误的分叉下载
- epoch 边界状态（justification/finalization）不一致
- 未清理的请求缓存导致后续 batch 包含过期区块

**期望处理**：
- 检测到 finalized checkpoint 变化
- 清理请求缓存（pending batches/blocks）
- 重置 fork choice 到新的 finalized 点
- 更新 sync target

**定向搜索查询**：
```
reorg during sync reset fork choice finalization
finalized checkpoint changed mid-sync clear caches
epoch boundary reorg justified finalized update
handleReorg onReorg resetSyncChain clearRequestCaches
```

**关注点**：epoch 边界处的 reorg 尤其危险，因为 justification/finalization
计算依赖于上一 epoch 结束时的状态。若 reorg 跨越 epoch 边界，需要正确回滚
`process_epoch` 的副作用（见 `docs/Epoch_Boundary_Investigation.md`）。

---

### el_invalid_cascade — EL INVALID 级联错误

**触发条件**：`engine_newPayload` 返回 `INVALID`，且 `latestValidHash` 指向
错误的祖先块（而非真正最后一个有效块）。

**风险**：
- CL 根据错误的 `latestValidHash` 级联作废一批本来有效的区块
- 导致节点从错误祖先开始重新构建链
- 可被恶意或有 bug 的 EL 利用，造成与诚实多数的共识分叉

**期望处理**：
- 正确解析 `latestValidHash`，仅作废 `latestValidHash` 之后的区块
- 不能因为 `latestValidHash` 无效就无限向前追溯
- 级联作废后需正确更新 fork choice head

**定向搜索查询**：
```
latestValidHash INVALID cascade invalidate descendants rollback
removeInvalidBlockAndState SetOptimisticToInvalid
invalid payload latest valid hash verification ancestor
fork choice rollback on invalid execution payload
```

---

### el_syncing_stuck — EL 持续 SYNCING 锁定

**触发条件**：EL 长期返回 `SYNCING` 状态，CL 因此进入永久 optimistic 同步状态。

**风险**：
- optimistic head 可被攻击者控制（发送 optimistic 区块无须 EL 验证）
- 若 optimistic 深度无上限，攻击者可无限累积未验证的 optimistic 链
- 节点可能在 `SYNCING` 状态下继续发布 attestation，导致对错误链投票

**期望处理**：
- 设置 optimistic 深度上限（如超过 N 个区块则停止导入）
- EL 超时后断连并报警
- 不允许在 optimistic 深度超限时继续生产 block/attestation

**定向搜索查询**：
```
optimistic depth limit exceeded threshold max
optimistic sync stuck indefinitely timeout disconnected
IsOptimisticBlock optimistic head depth limit
EL syncing halt stop import optimistic chain exceeds
```

---

### gossip_flood_ddos — Gossip DDoS

**触发条件**：恶意 peer 大量发送 gossip 消息（区块、attestation、blob sidecar
等），超出正常网络流量数量级。

**风险**：
- CPU 耗尽（BLS 验证、状态转换等高计算量操作）
- 消息队列满，导致正常区块/attestation 被丢弃
- 节点因处理 gossip 消息而无法及时完成 validator 职责（错过 proposal/attestation 窗口）

**期望处理**：
- Gossip 消息队列有界（有最大容量）
- 对 invalid gossip 消息的来源 peer 进行惩罚/封禁
- 速率限制（rate limiting）防止单 peer 占用过多带宽
- 对不同类型消息（block/attestation/blob）分队列处理

**定向搜索查询**：
```
rate limit gossip message processing queue bounded
peer score penalty invalid gossip flood reject
message queue max size bounded throttle
gossip validation throttle rate limit peer ban
```

---

### missing_parent_flood — 孤儿区块洪泛

**触发条件**：收到大量 parent 未知的区块（orphan blocks），可能来自攻击者
故意发送无法追溯的区块或因网络分区产生的合法孤儿块。

**风险**：
- orphan 队列无界导致内存耗尽（OOM）
- 大量 BeaconBlocksByRoot 请求涌向 peer，消耗网络带宽
- 正常区块可能因队列满而无法被处理

**期望处理**：
- orphan block 缓存有大小上限（LRU 或 FIFO 淘汰）
- 对于发送大量孤儿块的 peer 进行惩罚
- 对 parent 请求进行去重和速率控制

**定向搜索查询**：
```
orphan block unknown parent queue bounded max
missing parent request by root limit cache evict
pending block cache max size orphan pool
parent not found request peers limit flood
```

---

### slashing_sign_order — 签名与 Slashing 检查顺序

**触发条件**：验证者执行签名操作（attestation 或 block proposal）时，
slashing protection 数据库的检查和写入操作与签名操作之间的时序关系。

**风险**：若签名发生在 slashing DB 写入之前，在以下情况下可能产生双重签名：
- 两个实例并发运行（竞态条件）
- 签名后、写入 DB 前发生崩溃，重启后重试

理论上，正确的顺序应当是：
1. 读 slashing DB，检查是否 slashable
2. **先写 DB（记录即将签名的 epoch/slot）**
3. 再执行 BLS 签名

反之则存在崩溃后双重签名的窗口。

**定向搜索查询**：
```
slashing protection check before sign attestation block
maySign checkAndInsert slashingDB BLS signature order
sign then record slashing database crash recovery
double sign protection signing root DB write
```

**历史案例**：Prysm 在某些实现版本中曾存在签名顺序问题（见历史复核报告
audit_4 VULN-1 方向），值得重点核验。

---

### checkpoint_ws_violation — Checkpoint 弱主观性违规

**触发条件**：进行 checkpoint sync 时，使用了超出弱主观性（Weak Subjectivity）
窗口的 checkpoint，即 checkpoint 的 epoch 距当前 epoch 过远。

**风险**：
- 节点从恶意链 bootstrap，无法通过后续 PoS 规则感知到真正的最终确定性
- 攻击者可构造一条看似有效但偏离主链的历史，欺骗新加入节点
- 节点的 validator 在虚假链上投票，导致 slashing

**期望处理**：
- 在使用 checkpoint 前验证其是否在 WS 窗口内
  `checkpoint_epoch > current_epoch - ws_period` 才允许使用
- 若验证失败：拒绝 checkpoint、上报 peer（若来自 peer）、打印警告
- WS 窗口大小应与 SLOTS_PER_EPOCH 和 MIN_VALIDATOR_WITHDRAWABILITY_DELAY 对齐

**定向搜索查询**：
```
weak subjectivity period validation checkpoint epoch boundary
WSCheckpoint isWithinWSPeriod validateAnchor too old
checkpoint outside weak subjectivity window fail ban
weak subjectivity check failure peer report error
```

---

## 扩展规划

当前 8 个场景侧重于同步安全和共识层关键路径。未来可按以下方向扩展：

| 优先级 | 候选场景 | 关联 Workflow |
|---|---|---|
| 高 | `double_vote_attempt` — 双重投票尝试 | attestation_generate |
| 高 | `blob_unavailable` — Blob 不可用（Deneb） | regular_sync |
| 中 | `peer_ban_evasion` — 封禁 peer 重连绕过 | initial_sync, regular_sync |
| 中 | `el_wrong_latesvalidhash` — 错误 LVH 细化 | execute_layer_relation |
| 低 | `attestation_pool_flood` — Attestation 池洪泛 | regular_sync |

**场景自动发现**：参见 `emergent-diff` 分支，该分支实现了通过跨客户端代码
不对称性自动发现场景候选的机制，计划在现有场景体系成熟后合并。
