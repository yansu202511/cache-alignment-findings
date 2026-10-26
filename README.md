# The Context-Compaction Cache Stampede

> **Why LLM agents silently re-prefill tens of thousands of tokens on every context compression — and the one-line fix that restores KV-cache hit rate from 10% to 99%.**

A field-verified finding + reproduction kit for a failure mode that affects **any LLM agent that does background context compaction on top of prefix/prompt caching**.

---

## TL;DR

Production LLM agents shrink long conversations by **compacting** (summarizing) old context. Inference servers (vLLM, SGLang, oMLX, Anthropic/OpenAI prompt caching) speed these agents up with **prefix / KV caching**, which only hits when a request **shares a byte-identical prefix** with a previously-cached one.

We measured a real agent where **the compaction step rebuilt the message list** (dropping the previous-archive checkpoint from the request). That single divergence broke the shared prefix at the *first message*, and the cache hit rate collapsed:

| | Before (buggy) | After (aligned) |
|---|---|---|
| KV-cache hit rate on compression | **10.1%** | **99.6%** |
| Cached prefix tokens | 8,192 | 144,165 |
| Tokens re-prefilled | 72,986 | **627** |
| Time-to-first-token | **206.9 s** | **3.1 s** |

**Root cause:** compaction mutated the request's message list, so it no longer matched the normal-request prefix the server had cached.

**Fix:** make the compaction request carry a message list that is **token-identical to the normal request prefix**; extract the old archive for lossless re-splicing *without* removing it from the request. Cache hit restored. Independently reproduced on a second, unrelated session (98.5% hit).

**The rule worth remembering:**

> **Any operation that reshapes an agent's context (compaction, pruning, rewriting, reordering) is a cache killer unless you keep the reshaped request's prefix token-identical to the normal request.**

## Why this matters

Context compaction is how long-running agents (Claude Code, Cursor, custom agent frameworks, RAG+tool loops) avoid blowing their context window. Prefix caching is how those same agents get low latency and low cost. These two features are usually developed and tuned **independently** — and the interaction between them is a silent, expensive footgun:

- The agent gets **10×–70× slower** exactly on the compaction turn, and it looks like "the model is just slow," not "we threw away the cache."
- The cost scales with context size: a 100k-token context re-prefilled from scratch is minutes and real money.
- It's invisible unless you instrument cache-hit rate per request. Most stacks don't.

## What is and is not claimed

- **Not claimed:** the invention of prefix/KV/prompt caching (well documented: vLLM Automatic Prefix Caching, Anthropic prompt caching).
- **Not claimed:** that "changing a prompt breaks a cache" is unknown in principle.
- **Claimed:** to our knowledge, the first **public, quantified, independently-reproduced** writeup that (a) names agent **context compaction** as a concrete cache-stampede trigger, (b) measures the collapse and the fix with **dual-source, field-verified numbers**, and (c) provides a **detect → localize-divergence → fix → re-verify** method plus a diagnostic you can run.

If prior work exists that we missed, open an issue with a link and we will cite it — being right about the history matters more than the headline.

## The finding, in one mechanism

Prefix caches match on the **longest common prefix of the serialized request** (system header + tool schemas + message list). A normal turn looks like:

```
[ system header ][ tools ][ archive_checkpoint ][ turn_{n-1} ][ turn_n ]  ← cached
```

A naive compaction request drops `archive_checkpoint` to "keep the model window small":

```
[ system header ][ tools ][ turn_{n-1}'][ turn_n' ]  ← diverges at the FIRST message
```

The server can only reuse the header/tool prefix (here, ~8,192 of ~81,178 tokens) and must **re-prefill the entire 72,986-token tail**. That re-prefill is the 206-second stall.

Keeping the checkpoint in the request (and still splicing the old archive losslessly into the *output*) restores the shared prefix:

```
[ system header ][ tools ][ archive_checkpoint ][ turn_{n-1} ][ turn_n ]  ← ~99% reused
```

## Contents

```
cache-alignment-findings/
├── README.md                  ← you are here
├── FINDING.md                 ← full technical writeup + dual-source evidence table
├── METHOD.md                  ← reusable detect→localize→fix→verify procedure
├── evidence/
│   ├── report-1-first-repro.md
│   ├── report-2-independent-repro.md
│   └── omlx-cache-log-excerpts.log   ← raw server-side cache counters (redacted)
└── demo/
    └── prefix_divergence_demo.py     ← runnable, model-free prefix-divergence simulation
```

## Quickstart (no GPU, no API key)

The `demo/` reproduces the *mechanism* — prefix divergence and its cost — deterministically, without any model:

```bash
python3 demo/prefix_divergence_demo.py
```

It prints, for both the buggy and fixed message-list construction, the longest-common-prefix length and the resulting "re-prefill" token count, mirroring the production numbers above.

## Reproducing in the wild

`METHOD.md` gives the exact instrumentation: log per-request `cached_tokens` / `cache_read_input_tokens` / time-to-first-token, then compare a normal request vs a compaction request on the same session. Any inference stack exposing cache-hit counters will show the same collapse if it reshapes the message list.

## Evidence integrity

Two independent observation channels agree field-by-field (see `FINDING.md`):
- **Server-side** cache counters (`cached=118898 suffix=1759`)
- **Client-side** usage reporting (`cacheReadTokens=118898 inputTokens=1759`)

Two independent sessions (one new, unrelated session with a different system header) both land at 98–99% after the fix — ruling out a single-session fluke.

## License / citation

Released for the benefit of the agent-infra community. If this saves you a 200-second stall, a citation or a link back is appreciated.

*Measured on a local OpenAI-compatible inference server (oMLX) serving a 600B-class MoE with MTP speculative decoding. Model/provider names are incidental — the finding is model-agnostic and holds for any prefix-caching server.*
