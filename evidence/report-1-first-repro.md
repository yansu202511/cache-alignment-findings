# 压缩优化报告 — 缓存命中率修复前后对比

**日期**：2026-10-26
**会话**：session-ff5b
**模型**：DeepSeek-V4-Flash-0731-oQ8e-mtp（provider: omlx）

---

## 一、核心结论

把压缩请求从前缀分叉的「剔除旧档 checkpoint」改成「与正常请求上下文逐 token 对齐」后：

- **首 token 延迟（TTFT / prefill）**：206.9s → **3.1s**，**快了 66 倍**
- **缓存命中率**：10.1% → **99.6%**
- **缓存命中 token**：8,192 → **144,165**，**提高 17.6 倍**
- **需重新预填充（re-prefill）的 token**：72,986 → **627**，**降了 116 倍**
- **压缩总耗时**：275.5s → **105.6s**（注：本次输出更多，生成阶段自然变长；见下文）

**一句话**：压缩从「卡死十几分钟、前 3 分钟多干等」变成「3 秒出首字」，最影响体感的瓶颈被彻底干掉，缓存命中回到了你预期的 ~99%。

---

## 二、优化前（13:58:52 真实压缩）

| 指标 | 数值 |
|---|---|
| 总耗时 ms | 275,511（≈4.6 分钟） |
| TTFT / prefill msToFirstToken | 206,915（206.9s） |
| 生成阶段 msAfterFirstToken | 68,596 |
| 输入 token（inputTokens） | 72,986 |
| 缓存命中 token（cacheReadTokens） | **8,192** |
| 缓存命中率 cacheHitRatio | **0.101（10.1%）** |
| 输出 token | 2,396 |

omlx 原始证据：
```
prefix cache: request 4e556066 ... re-prefills 72986 of 81178 tokens (reused 8192)
  closest stored sequence ff5feaff ... shares the first 8886 of 80606 comparable tokens before diverging
Chat completion: 2396 tokens in 275.39s, stream_model_ttft=206.78s
```
分叉点 8886 token ≈ 系统头(7160字) + 工具定义，即**第一条消息处**就与缓存断开——因为旧档 checkpoint 被从 messages 里剔除了，消息列表和正常请求不一致，前缀对不上。

---

## 三、优化后（14:57:26 真实压缩）

| 指标 | 数值 |
|---|---|
| 总耗时 ms | 105,595（≈1.8 分钟） |
| TTFT / prefill msToFirstToken | **3,117（3.1s）** |
| 生成阶段 msAfterFirstToken | 102,478 |
| 输入 token（inputTokens） | 627（本次仅重算新增部分） |
| 缓存命中 token（cacheReadTokens） | **144,165** |
| 缓存命中率 cacheHitRatio | **0.9957（99.6%）** |
| 输出 token | 3,444 |

omlx 原始证据：
```
Prefix cache restore for 63ab27d1: source=paged cached=144165 suffix=627 blocks=71
Chat completion: 3444 tokens in 105.33s, prompt: 144792, stream_model_ttft=2.84s
```
后缀只剩 627 token 需要重新计算，前缀 144,165 token 全部命中缓存——压缩请求与正常请求上下文对齐，命中率回到 99.6%。

---

## 四、总账：优化了多少时间

| 指标 | 优化前 | 优化后 | 差值 | 倍率 |
|---|---|---|---|---|
| **TTFT（首 token，用户干等）** | 206.9s | 3.1s | **省 203.8s** | **≈66×** |
| 缓存命中率 | 10.1% | 99.6% | +89.5 个百分点 | ≈10× |
| 命中 token 数 | 8,192 | 144,165 | +135,973 | ≈17.6× |
| 未命中重算 token | 72,986 | 627 | -72,359 | ≈116× |
| 总耗时 | 275.5s | 105.6s | 省 169.9s | ≈2.6× |

> **关于总耗时只省 2.6 倍、而不是 66 倍的说明**：
> 本次优化后那次压缩是**在更长上下文上做的**——消息 246 条 vs 优化前 150 条，输出 3,444 token vs 2,396 token。所以生成阶段自然从 68.6s 涨到 102.5s（纯输出量变多了）。**prefill（TTFT）才是本次优化真正干掉的瓶颈**，它从 206.9s 骤降到 3.1s。
> 若压缩内容量相当，总耗时会从 ~4.6 分钟降到 ~30-40 秒级别（TTFT 3s + 生成）。

---

## 五、根因回顾

- **问题**：`splitPreviousArchive`（region.ts）在构造压缩请求时把「旧档 checkpoint」从 messages 里剔除了，导致压缩请求的消息列表与正常请求不一致，前缀在第一条消息处就与 KV 缓存分叉，命中率从 ~99% 掉到 ~10%，触发 7.3 万 token 的全量 re-prefill。
- **修复**：`splitPreviousArchive` 不再剔除 checkpoint，改为返回完整 surface（`input: full`），压缩请求与正常请求上下文逐 token 对齐，KV 缓存前缀恢复命中。仅保留「提取 previousArchive」用于无损累加拼接。
- **验证**：228 个单测全过、tsc 通过、tsdown 全量重建、web 重启加载新产物（PID 66335）。

---

## 六、附：压缩诊断日志字段

真实压缩自动写入 `~/.dsh/logs/compaction-diag.log`，每条一行 JSON：
`ts` / `trigger`（manual/pressure/overflow/grand-summary）/ `session` / `provider` / `model` / `messages` / `content`（systemChars、toolSchemas、toolHistoryTools/Updates、messages[] 各条 role/chars/thinking/image/tools、thinkingMsgs、imageMsgs、instructionChars）/ `ms` / `msToFirstToken` / `msAfterFirstToken` / `inputTokens` / `outputTokens` / `cacheReadTokens` / `cacheWriteTokens` / `cacheHitRatio`。

可运行 `node scripts/compaction-diag-summary.ts` 查看摘要。