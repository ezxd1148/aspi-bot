# Confession Bot: EC2 Long-Polling → Cloudflare Worker Migration Design

## 1. Context / Why This Migration

- Current system: Python script on AWS EC2, long-polling Telegram every 60s, LLM moderation, manual approval, JSON files for state (`pending_ids`, `processed_ids`).
- Reason for migration: AWS running out (cost/runway), not because of any AWS-specific dependency — EC2 was just “simple hosting,” nothing in the pipeline is architecturally tied to AWS.
- Target: fully Cloudflare — Workers (compute) + R2 (storage), triggered by Tally webhook instead of polling.
- Submission intake: Tally form → webhook → Worker.
- Storage: R2, used for both pending-state tracking and processed/approved confession records.

—

## 2. What Changed Structurally: Polling → Webhook

| Aspect | Old (long-polling) | New (webhook) |
|—|—|—|
| Trigger | Script pulls full Tally list every 60s | Tally pushes one submission per event |
| Concurrency | Single loop, one cycle at a time (+ manual threading) | Every webhook = independent, concurrent Worker invocation |
| Duplicate source | Re-fetching the same list across cycles | Tally’s own delivery retries |
| State needed | Diff “what’s new” out of a full list each cycle | Track in-flight status of a single known ID |

Two old state files were re-evaluated against this new model:

- **`processed_ids`** — existed purely to diff “already seen” items out of a repeatedly-refetched full list. With webhooks delivering one submission per event, there is no list to diff. **Confirmed dead — do not port over.**
- **`pending_ids`** — existed to coordinate a queue across slow 60s polling cycles + threading. That original job is also dead. However, a **new and different problem** surfaces on webhooks (see §3) that requires a similarly-named but functionally distinct mechanism. Not a straight port — a redesign.

- **Daily dual-reset race** (local storage reset vs. Tally-side reset sometimes landing on opposite sides of a 60s poll window, causing old messages to resend) — this was purely a polling-interval artifact. **Confirmed dead**, does not exist in a webhook model. Explicitly do not carry this logic forward.

—

## 3. Time-Based Problems — Full Comparison

| # | Problem | Trigger | Timescale | Root cause | Status |
|—|—|—|—|—|—|
| 1 | Tally retry → duplicate webhook delivery | Worker doesn’t return 2xx within Tally’s window | **10 seconds** | Sync moderation call blocks the HTTP response | Confirmed via Tally docs |
| 2 | Concurrent invocations racing on same submission ID | Retry from #1 fires while original invocation still mid-LLM-call | Overlapping, duration = however long moderation takes | R2 has no built-in atomic check-then-write across two separate calls | Solved by design in §5 |
| 3 | Worker CPU-time budget | Every invocation | 10ms active compute (free tier) | Only counts active compute, not time spent awaiting I/O (e.g. LLM fetch calls) | Resolved — not a real constraint given moderation is I/O-bound |
| 4 | DeepSeek slow-under-load | High traffic / server congestion | Degrades gradually; connection force-closed at ~10 min if never scheduled | No hard rate limit → no clean 429, just silent slowness | Open — depends on fallback chain design (§7, not yet built) |
| 5 | OpenRouter / NIM rate limiting | Burst of submissions | 20 RPM (OpenRouter, hard cap regardless of credits) / ~40 RPM (NIM, soft/traffic-dependent) | Hard per-minute caps, fails fast with 429 | Open — depends on fallback chain design (§7, not yet built) |
| 6 | Old daily dual-reset race | Local reset firing before Tally-side reset | Within a 60s poll window | Two independent reset schedules, no ordering guarantee | Dead — polling-specific, explicitly not ported |

**Key relationship:** #1 and #2 are coupled with #4/#5. If the primary moderation API is slow or rate-limited and a fallback cascade takes too long, you’re more likely to blow the 10s Tally window (→ #1), which increases the odds of the concurrent-invocation race (→ #2). These are not independent risks — the fallback chain design directly affects how often the race condition and duplicate-retry path actually get exercised.

—

## 4. Verified External Constraints (not assumptions — checked)

**Tally webhook retry policy:**
- If the Worker doesn’t return a successful (2xx) status within a **10-second window**, Tally retries delivery of the same submission.
- Tally can sign webhook requests (SHA256) for verification — deferred, see §8.

**Cloudflare Workers (free tier):**
- 10ms of *active* CPU time per invocation. Time spent awaiting network I/O (e.g. `fetch` to an LLM API) does **not** count against this.
- 100,000 requests/day cap.
- Every invocation is isolated and concurrent by default — no shared process/memory between invocations. A slow invocation does NOT block other incoming webhooks; Cloudflare simply spins up a separate invocation per request.

**R2 conditional writes (verified, not assumed):**
- R2 supports atomic conditional writes via `onlyIf` in the Workers API (`bucket.put(key, value, { onlyIf: condition })`), and standard S3-compatible conditional headers (`If-Match`, `If-None-Match`, etc.) on the S3-compatible endpoint.
- If the precondition fails, the write is aborted atomically and R2 returns `null` (or a precondition error) rather than silently overwriting.
- This is the primitive that makes the lock design in §5 possible — R2 itself arbitrates the race, not application-level coordination.

**LLM provider limits (third-party-reported, verify against your own account dashboards before relying on them):**
- **OpenRouter (free tier):** 20 requests/minute hard cap (does not increase with credits). Daily cap: 50/day unfunded, 1,000/day once $10+ credits purchased at any point (permanent upgrade).
- **Nvidia NIM (free/dev tier):** Not officially credit-based; throttling is traffic/model-dependent. Community-observed baseline ~40 RPM. NVIDIA’s own terms define serving real end-users as “production use,” which technically calls for NVIDIA AI Enterprise rather than the free dev tier — worth checking given the bot serves 300+ real users.
- **DeepSeek (paid Pro):** No hard RPM cap — it does not reject with 429s at a fixed threshold. Instead, under load it “tries its best,” meaning response latency degrades rather than failing fast. If a request sits fully unscheduled for too long, the server closes the connection (~10 minutes per most recent docs). Practical implication: the bottleneck under load isn’t DeepSeek rejecting you — it’s your own Worker’s execution/response deadline running out while waiting on a slow DeepSeek response.

—

## 5. Pending-Lock Redesign (solves #1 + #2)

### Why the naive approach fails
A simple two-step “check if ID exists, then write” (`GET` then `PUT`) is not safe: two concurrent invocations (original delivery + Tally retry) can both execute the `GET` and both see “not found” *before either one completes the `PUT`* — the classic TOCTOU race. This requires an atomic primitive, not a faster check.

### Why “process-shared locks” (mutex/semaphore) don’t apply here
That model assumes shared memory/process space so racers can literally block on each other. Worker invocations don’t share memory and may run on different edge nodes — there’s no shared runtime to hold a lock in. The correct model is **distributed compare-and-set against a shared external store**, where the store itself (R2) is the sole arbiter of who wins, not any coordination between the two invocations.

### The ID must come from Tally, not be self-assigned
Critical correction made mid-design: the lock key must be **Tally’s own `submissionId`**, extracted from the webhook payload immediately on arrival, before any moderation/LLM logic runs. (Confirmed: Tally generates and sends this ID with every delivery of a given submission, including retries — same ID on the retry as the original.) An ID generated by your own system *after* processing begins is structurally too late to arbitrate a race that starts at the moment of arrival.

### Lock state machine — `pending/{submissionId}` in R2

| State | Meaning | Set by | Retry behavior when hit |
|—|—|—|—|
| *(absent)* | Never claimed | — | Claimable — proceed to process |
| `processing` | Actively being moderated right now | Winning invocation, via atomic `bucket.put(pending/{id}, “processing”, { onlyIf: absent })` | Silent no-op, return 200 |
| `done` | Successfully moderated + posted | Winning invocation, on success | Silent no-op, return 200 |
| `failed_id` | Moderation pipeline errored; needs human review | Winning invocation, on unrecoverable failure (see §6) | Silent no-op, return 200 (only resolved by admin action, not by further retries) |

Note: retries hitting `failed_id` are treated the same as `processing` (silent no-op) — the distinct state exists for the admin’s error-log context, not for different control flow. Flagged as worth re-confirming once actually building, since it was decided quickly.

### Request flow

1. Webhook lands → extract Tally `submissionId` immediately, before any moderation logic.
2. Attempt `bucket.put(pending/{id}, “processing”, { onlyIf: absent })`.
3. **Write fails** (key already exists, in any state) → this is a duplicate/retry of an already-claimed submission → return 200, stop. No further processing.
4. **Write succeeds** → this invocation owns the submission → proceed:
   - Run moderation (through fallback chain, §7 — not yet designed).
   - **On success:** post per moderation verdict → `bucket.put(pending/{id}, “done”)` → return 200.
   - **On unrecoverable failure:** see §6.

—

## 6. Failure Path — Moderation Pipeline Errors (“Shape A”)

Scope: this covers the case where the LLM moderation call(s) genuinely fail to produce a verdict (API down, all fallbacks exhausted, timeout) — **not** a mid-execution crash with no error signal at all (that’s a separate, unresolved edge case — see §8).

Explicitly rejected approach: auto-posting the submission publicly “regardless” of moderation outcome. This would silently bypass the manual-approval invariant that is a deliberate, load-bearing part of the original design (LLM filter → manual approval → post), not an incidental detail. An unmoderated post that’s visually indistinguishable from an approved one defeats the purpose of having moderation at all.

### Agreed behavior on unrecoverable moderation failure:
1. Transition the lock: `bucket.put(pending/{id}, “failed_id”)`, with a short error log attached (reason for failure — e.g. which providers failed, what error).
2. Push a flagged message to a separate **admin-only Telegram channel/DM** — explicitly not the public confession channel, and explicitly distinguishable from the normal “LLM-approved, awaiting human confirmation” queue.
3. Return 200 to Tally (so it doesn’t keep retrying — the failure has been captured and handed to a human, not left in limbo).

This ensures: nothing gets lost, nothing gets posted unmoderated, and a human has a clear, labeled action item instead of silently missing a failed submission.

—

## 7. Fallback Chain — NOT YET DESIGNED (next step)

Explicitly agreed to add **3 more free-tier AI fallbacks** beyond the current OpenRouter → NIM → DeepSeek Pro chain, since the moderation task (comparing submission text against `PROMPT.md`) doesn’t require a high-end model — it’s a comparison/classification task, not open-ended generation.

Still open, do not build the fallback chain until these are decided:
- Exact provider order and which specific free-tier providers to add (3 more, TBD).
- What triggers a fallback to the next provider: a 429 specifically? A timeout threshold (and if so, what threshold)? Both?
- Whether the full fallback cascade needs to complete *within* Tally’s 10-second window, or whether you’re accepting that a full cascade might itself exceed 10s and deliberately lean on the §5 lock design to absorb the resulting retry safely.
- Whether `ctx.waitUntil()` is used — i.e., whether the Worker returns 200 immediately and continues moderation asynchronously in the background, vs. holding the response open until moderation completes. This decision changes the relationship between step 4 in §5 and the 10-second deadline entirely, and hasn’t been decided yet.

—

## 8. Explicitly Deferred / Not Yet Addressed

- **Security / webhook authenticity verification** — deliberately set aside for now to focus on features first. Flagged as a “day one, not day two” concern in earlier discussion (webhook URLs are public; anyone who finds the URL can POST fake payloads once security isn’t addressed), but the person has explicitly chosen to sequence this after core functionality. Tally does support signing webhook requests (SHA256) for future verification.
- **Mid-execution crash with no error signal** (Worker killed, uncaught exception, network blip) *before* a moderation result is even reached — distinct from Shape A (clean moderation failure). Not yet designed: whether `pending/{id}` needs a TTL/expiry so a silently-dead invocation doesn’t leave a submission locked in `processing` forever with no admin notification at all.
- **Whether a successful resubmission by the same person for a “failed_id” case should be possible**, and how — currently, `failed_id` retries no-op silently, meaning resolution is admin-only. Not yet revisited in light of the final agreed design.
