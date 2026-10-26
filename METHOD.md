# Method: detect → localize → fix → verify

A reusable procedure for finding (and preventing) compaction-induced cache
stampedes in **any** agent stack that combines background context reshaping with
prefix/prompt caching.

---

## Step 0 — Instrument per-request cache counters

You cannot fix what you cannot see. For **every** request, log:

- `cache_read_input_tokens` / `cached_tokens` (server-reported prefix hits)
- uncached `input_tokens`
- `cache_write_input_tokens` (if exposed)
- time-to-first-token (prefill proxy)
- a `trigger` tag: `normal` vs `compaction` (and sub-kinds: manual / pressure / overflow)

Derive `cacheHitRatio = cacheRead / (cacheRead + uncachedInput)`.

> In OpenAI-compatible stacks: Anthropic exposes `cache_read_input_tokens`; vLLM
> exposes cached tokens in usage / metrics; oMLX logs `cached=… suffix=…` per
> scheduler restore. If your server exposes none, log serialized-request bytes +
> TTFT and diff successive requests.

## Step 1 — Detect the outlier

Plot `cacheHitRatio` by `trigger`. **Compression turns are cache outliers** when
they fall far below the normal-request line:

```
normal requests       : ~0.95–0.99  (steady, since prefixes overlap)
compaction requests   : ~0.10       (★ the stampede)
```

If compaction requests also show a TTFT spike (tens of seconds to minutes) with a
large uncached-input count, you have the failure mode.

## Step 2 — Localize the divergence point

Compare the serialized request of the **compaction turn** against the
**immediately-preceding normal turn** on the same session, and find the byte
offset of the first difference. The server often reports this directly
(oMLX: `closest stored sequence … shares the first N tokens before diverging`).

Map that offset back to the message list. Common culprits:

- **Dropped/reordered messages** (e.g. previous-archive checkpoint removed to
  "keep the model window small") ← our case; divergence at the first message
- Re-rendered system header (dynamic date/version that changes every turn)
- Tool-schema ordering differences
- Whitespace / serialization differences between the normal path and the
  compaction path (two different builders that drift)

## Step 3 — Fix: align the request prefix

Make the compaction request's **message list token-identical** to the normal
request's prefix. Concretely:

- Do **not** mutate (drop/reorder/re-render) messages that were part of the
  cached prefix, **just because** this is a compaction call.
- Extract anything you need from the prefix **for output post-processing** without
  removing it from the request.
- If you must keep the model's effective window small, prefer provider-native
  mechanisms (cache breakpoints / explicit cache control) over reshaping the list.
- Unify the request builders: the compaction path and the normal path should
  serialize the shared prefix with the **same code**, so they cannot drift.

## Step 4 — Verify with dual sources

Re-measure a real compaction and confirm **both** channels move together:

- server-side cached tokens ≈ the full prefix (not just the header)
- client-side `cacheReadTokens` matches the server's `cached=` **field-by-field**
- hit ratio returns to the normal-request line (~99%)
- TTFT drops to the size of the genuine residual suffix only

Reproduce on a **second, unrelated session** (different context, different
system-header length) to rule out a single-session fluke.

## Step 5 — Guard against regression

The failure recurs silently if a future refactor re-introduces message-list
mutation. Add a test: build the compaction request and the normal request, assert
the compaction request's serialized prefix **equals** the normal request's over
the shared span. Fail the build if it diverges.

---

## The one-line rule

> Keep the reshaped request's prefix **token-identical** to the normal request.
> Extract from the prefix for output; never mutate it for the request.
