# Finding: Agent Context Compaction Collapses Prefix/KV Cache Hit Rate

**Status:** field-verified in production, independently reproduced twice.
**Date measured:** 2026-10-26.
**Stack:** a long-running LLM agent (context compaction with lossless archive splicing) over a local OpenAI-compatible inference server using paged automatic prefix caching, serving a 600B-class MoE with MTP speculative decoding. Model/provider names are incidental; the finding is model-agnostic.

---

## 1. Claim (precise)

When an agent performs **context compaction** (summarizing/pruning older context) and that compaction **rebuilds the request message list** — e.g. by removing the previous compaction archive ("checkpoint") from the messages sent to the model — the resulting request **diverges from the cached normal-request prefix at the first message**. A prefix/KV cache that matches on the longest common serialized prefix can then reuse only the system header + tool schemas, and must **re-prefill the entire remaining prompt** (here: ~73k tokens), producing a multi-minute, multi-dollar stall that is invisible without per-request cache instrumentation.

Keeping the compaction request's message list **token-identical to the normal request prefix** (while still extracting the old archive for lossless output splicing) restores the shared prefix and returns the cache hit rate from **~10% to ~99%**.

## 2. What is NOT claimed

- Prefix/KV/prompt caching is not new (vLLM Automatic Prefix Caching; Anthropic/OpenAI prompt caching).
- "Changing a prompt can miss a cache" is not, in principle, unknown.

The contribution is **naming compaction as a concrete, common cache-stampede trigger, quantifying the collapse and the fix with dual-source field data, reproducing it independently, and giving a reusable method + runnable demo.**

## 3. Mechanism

Prefix caches reuse `cached = LCP(serialized(request), serialized(last_cached))`.

```
normal cached request:
  [ system header | tool schemas | ARCHIVE | turn_{n-1} | turn_n ]
compaction request (buggy: ARCHIVE stripped):
  [ system header | tool schemas | turn_{n-1}' | turn_n' ]
                                    ^ divergence at first message
=> cached = only header+tools; re-prefill = everything after
```

The archive is normally the *first content message* (it is the replacement user message a prior compaction committed). Stripping it shifts every subsequent message left by one slot, so position-sensitive prefix matching breaks immediately.

## 4. The fix

Do **not** mutate the message list sent for compaction. Extract the previous archive body for lossless re-splicing into the *output*, but keep the full surface (including the checkpoint message) in the *request*, so the compaction request shares the normal request's prefix token-for-token.

Risk considered and dismissed: the model re-reading the archive it already sees every normal turn introduces **no new risk** — the archive was already in the model's context on normal turns; we simply stop deleting it from the compaction request. The historical "archive echo / matryoshka bloat" concern is handled by stripping archived-checkpoint echoes from the model's *output* before splicing, orthogonal to this change.

## 5. Evidence (dual-source, field-verified)

### 5.1 Compression is a cache outlier on the same session

Server-side (`omlx` scheduler prefix-cache counters), same session, minutes apart:

```
13:57:10  normal req ff5feaff : cached=80226  suffix=382   → 99.5% hit
13:58:52  compress  4e556066 : cached=8192   suffix=72986  → 10.1% hit
          "re-prefills 72986 of 81178 tokens (reused 8192);
           closest stored sequence ff5feaff shares the first 8886
           of 80606 comparable tokens before diverging"
```

Divergence at ~8,886 tokens = system header (≈7,160 chars) + tool schemas → **first message**.

### 5.2 Before → after (same fix, second compression)

| Metric | Before (13:58:52) | After (14:57:26) |
|---|---|---|
| Cache hit rate | 10.1% | 99.6% |
| Cached tokens | 8,192 | 144,165 |
| Re-prefilled tokens | 72,986 | 627 |
| Time-to-first-token | 206.9 s | 3.1 s |

Server log after fix:
```
14:55:41 compress 63ab27d1 : cached=144165 suffix=627 blocks=71
14:57:26 Chat completion: 3444 tokens in 105.33s, prompt: 144792, ttft=2.84s
```

### 5.3 Independent reproduction (different session)

```
15:06:58 compress df7ed738 : cached=118898 suffix=1759 blocks=59   (session-68a8)
15:08:54 Chat completion: 3168 tokens in 143.40s, prompt: 120657, ttft=32.98s
```
Hit rate 98.5%. Different session, different context scale, different system-header length (7,160 → 7,175 chars) → rules out a single-session fluke.

### 5.4 Client/server cross-check (no single-source bias)

For the second reproduction, the client-side usage report and the server-side cache counters agree **field-by-field**:

| Field | Server (`omlx`) | Client (agent usage) | Match |
|---|---|---|---|
| cached prefix tokens | `cached=118898` | `cacheReadTokens=118898` | ✅ |
| re-prefilled tokens | `suffix=1759` | `inputTokens=1759` | ✅ |
| total prompt | `prompt: 120657` | `118898 + 1759 = 120657` | ✅ |
| TTFT | 32.98 s | 33.18 s | ✅ |
| total time | 143.40 s | 143.59 s | ✅ |

## 6. Honest caveats

- After the fix, TTFT is not always ~3 s: it scales with the *residual* (uncached suffix). On the second reproduction the suffix was 1,759 tokens and the output longer, so TTFT was ~33 s — still an order of magnitude better than a 73k re-prefill, and a *resolution* signal that the instrumentation is real, not fixed to a number.
- The absolute token counts are workload-specific; the **ratio** (hit rate collapse and restoration) is the portable result.
- See README for the prior-work disclaimer: if a public quantified writeup of this exact interaction predates us, we will cite it.
