#!/usr/bin/env python3
"""
prefix_divergence_demo.py
=========================
Model-free, deterministic reproduction of the *mechanism* behind the
"context-compaction cache stampede": when an agent's compaction step rebuilds
the request message list (e.g. by dropping the previous-archive checkpoint),
the serialized request no longer shares a prefix with the cached normal request,
so a prefix/KV cache can only reuse the header and must re-prefill the entire tail.

No GPU, no API key, no network. Run:
    python3 prefix_divergence_demo.py

It approximates tokens as 4 chars/token and models prefix-cache reuse as
"length of the longest common prefix with the last cached request".
The absolute numbers are illustrative; the RATIO and the divergence point are
what matter and mirror our production measurements.
"""
from __future__ import annotations

CHARS_PER_TOKEN = 4


def serialize(system_header: str, tool_schemas: str, messages: list[str]) -> str:
    """Approximate how an OpenAI-compatible server serializes a request to a string.

    Real servers tokenize this; the key property for prefix caching is that the
    serialization is *position-sensitive*: the first differing byte ends the
    reusable prefix.
    """
    return system_header + "\x00" + tool_schemas + "\x00" + "\x00".join(messages)


def longest_common_prefix_len(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def cache_report(label: str, cached_req: str, this_req: str) -> dict:
    lcp = longest_common_prefix_len(cached_req, this_req)
    total_chars = len(this_req)
    cached_tokens = lcp // CHARS_PER_TOKEN
    total_tokens = total_chars // CHARS_PER_TOKEN
    reprefill_tokens = (total_chars - lcp) // CHARS_PER_TOKEN
    hit_ratio = (lcp / total_chars) if total_chars else 0.0
    print(f"[{label}]")
    print(f"    total prompt        : {total_tokens:>8,} tokens")
    print(f"    cached (LCP) reused : {cached_tokens:>8,} tokens  (hit {hit_ratio*100:5.1f}%)")
    print(f"    re-prefilled tail   : {reprefill_tokens:>8,} tokens")
    # diverge point in tokens, and which message it lands in
    print()
    return {
        "total_tokens": total_tokens,
        "cached_tokens": cached_tokens,
        "reprefill_tokens": reprefill_tokens,
        "hit_ratio": hit_ratio,
    }


def main() -> None:
    # Build a realistic conversation, scaled to roughly mirror the production
    # case (system header + tool schemas are a SMALL fraction; the accumulated
    # archive is the bulk of the prompt, exactly as in a long-running agent).
    system_header = "SYSTEM_HEADER " + ("instructions to the agent " * 60)     # ~1.5k chars
    tool_schemas = "TOOLS " + ('{"name":"read","input":{}}' * 30)              # ~1k chars
    old_archive = "<compacted-summary>" + ("## accumulated archive body " * 4000) + "</compacted-summary>"  # ~116k chars
    recent_turns = [("USER: " + "question " * 30, "ASSISTANT: " + "answer " * 30) for _ in range(40)]

    normal_msgs = [old_archive]
    for u, a in recent_turns:
        normal_msgs += [u, a]

    # The normal request the server cached on the previous turn:
    cached_normal = serialize(system_header, tool_schemas, normal_msgs)

    # Compaction on the NEXT turn, appending one new exchange:
    new_msgs = normal_msgs + ["USER: new question", "ASSISTANT: new answer"]

    # ---- BUGGY compaction: drop the archive checkpoint from the request ----
    buggy_msgs = new_msgs[1:]  # remove old_archive (the checkpoint)
    buggy_req = serialize(system_header, tool_schemas, buggy_msgs)

    # ---- FIXED compaction: keep the full surface in the request ----
    fixed_msgs = list(new_msgs)  # full surface, token-identical prefix with normal
    fixed_req = serialize(system_header, tool_schemas, fixed_msgs)

    print("=" * 72)
    print("Context-compaction cache stampede — mechanism reproduction")
    print("=" * 72)
    print()

    # Baseline: a NORMAL turn also pays ~0 re-prefill (sanity check)
    normal_next = serialize(system_header, tool_schemas, new_msgs)
    base = cache_report("baseline normal next-turn (reference)", cached_normal, normal_next)

    buggy = cache_report("compaction WITHOUT alignment (buggy)", cached_normal, buggy_req)
    fixed = cache_report("compaction WITH alignment (fixed)", cached_normal, fixed_req)

    print("-" * 72)
    print("Verdict")
    print("-" * 72)
    print(f"  buggy hit ratio : {buggy['hit_ratio']*100:5.1f}%   re-prefill {buggy['reprefill_tokens']:>7,} tokens")
    print(f"  fixed hit ratio : {fixed['hit_ratio']*100:5.1f}%   re-prefill {fixed['reprefill_tokens']:>7,} tokens")
    speedup = buggy["reprefill_tokens"] / max(fixed["reprefill_tokens"], 1)
    print(f"  re-prefill reduction (fixed vs buggy): {speedup:.1f}x fewer tokens re-computed")
    assert buggy["hit_ratio"] < 0.3, "buggy path should show severe cache miss"
    assert fixed["hit_ratio"] > 0.95, "fixed path should show near-full cache hit"
    print()
    print("The buggy path diverges at the FIRST message (the dropped checkpoint),")
    print("so only the system+tool header is reused — exactly what we saw in production:")
    print("  production buggy : cached 8,192 / 81,178   (~10% hit)")
    print("  production fixed : cached 144,165 / 144,792 (~99.6% hit)")


if __name__ == "__main__":
    main()
