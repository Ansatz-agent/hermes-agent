# Object Context V1.1 / V1.2

Object Context is a bundled, opt-in object-virtualization Context Engine, not a
replacement for conversation summarization. It supports two explicitly selected
schedulers:

- large, reliably bounded structured objects are stored exactly;
- every new Delta is sent raw successfully at least once before it is eligible;
- `economic` preserves the V1.1 immediate-next-request economic scheduler;
- `amortized_batch` enables the V1.2 bounded Hot Tail and pending-batch
  scheduler, with either the default dynamic W/Q policy or a fixed-count
  comparison policy;
- a winning batch is atomically replaced with stable versioned Cards;
- the agent can load one known immutable reference with `retrieve_object`;
- the exact result is itself a normal inference Delta: it is sent raw once and
  later becomes a compact same-reference receipt only if its scheduler flushes;
- Hermes' existing whole-history summarizer continues to run independently and
  consumes the same Card-rendered recent context that the normal provider sees.

The persisted user/assistant/tool trace remains raw and authoritative. Cards
are request-time projections and are never written over the transcript.

Object Context requires a host path that exposes and accepts the complete
provider message view. It is therefore supported by the normal Chat
Completions, Anthropic Messages and `codex_responses` loops, but not by
`codex_app_server`, whose historical thread is opaque and server-owned. If that
combination is configured, Hermes logs the incompatibility and explicitly
falls back to the built-in whole-history compressor instead of reporting
nonexistent Raw-exposure, cache-prefix or W/Q state.

## Enable and configure

Select the engine through `ansatz plugins`, or edit `~/.hermes/config.yaml`:

```yaml
context:
  engine: object_context
  object_context:
    enabled: true
    scheduler: economic
    hot_tail_max_inferences: 4
    hot_tail_max_tokens: 12800
    amortized_cache_read_weight: 0.10
    batch_policy: dynamic
    fixed_batch_size: 4
    min_raw_exposures: 1
    economic_min_net_saving_tokens: 1000
    economic_min_net_saving_usd: null
    economic_cache_read_ratio_fallback: 0.10
    economic_cache_write_ratio_fallback: 1.00
    emergency_context_ratio: 0.90
    object_prefilter_min_tokens: 256
    min_absolute_saving_tokens: 128
    min_relative_saving_ratio: 0.25
    card_summary_enabled: false
    summary_max_tokens: 64
    wm_grace_deltas: 20
    recent_retrieval_active_deltas: 20
    retrieval_max_tokens_ratio: 0.50
```

No user-facing environment variable is used. Returning to the built-in engine
requires only `context.engine: compressor`.

The classic interactive CLI also exposes a profile-scoped configuration
command:

```text
/object_context
/object_context stats
/object_context monitor
/object_context on
/object_context off
/object_context set scheduler amortized_batch
/object_context set batch_policy fixed
/object_context set fixed_batch_size 4
/object_context set hot_tail_max_inferences 4
/object_context set hot_tail_max_tokens 12800
/object_context set amortized_cache_read_weight 0.10
/object_context set economic_min_net_saving_tokens 1500
/object_context set emergency_context_ratio 0.92
/object_context set card_summary_enabled true
/object_context reset economic_min_net_saving_tokens
/object_context reset all
/object_context help
```

The command validates only the settings listed below and persists them to the
active profile's `config.yaml`. It never hot-swaps the Context Engine or tool
schema inside a live conversation; restart the CLI after a change. `set` does
not implicitly enable Object Context, and `reset` removes explicit overrides so
the merged code defaults apply again. `/object_context` status shows the
effective scheduler, its V1.1/V1.2 label, the effective Hot Tail limits, and the
fixed derived pending limits.

`/object_context stats` is read-only and reports two scopes. The primary
evaluation scope measures only persisted user/assistant/tool conversation
records before and after Card projection. It excludes the system prompt, tool
schemas, ephemeral prefills, and request-only memory/plugin/MoA injections.
The diagnostic assembled-message scope includes the system/prefill message
view but still excludes tool schemas, which are a separate provider request
parameter. Both deliberately exclude one-time per-Delta Card-build metrics,
avoiding double counting; neither is a provider-billing total. Retrieved
payload is reported separately and is already reflected in the rendered
projection.

`/object_context monitor` opens a private live page on an unguessable
loopback-only URL. Every browser refresh rebuilds the page from current
persisted telemetry, so the command does not need to be entered again while
the owning CLI process remains alive. It also writes a mode-0600,
self-contained HTML snapshot as a fallback. Sessions with neither projection
nor scheduler-decision evidence remain selectable with zero saved tokens while
their normal provider-request token, cache, latency, and turn series remain
visible. A V1.2 session that has only durable `wait` decisions is already
labelled `OC`, even before its first Card projection, because that recorded
scheduler activity is direct Object Context evidence rather than a guessed
configuration state.

Persisted telemetry roots are retained even if their
SessionDB row is no longer in the normal visible listing. The workspace
provides global KPIs, a searchable session/run list and comparison table, and
selects the current conversation by default. Stored Hermes session titles are
the primary labels; stable conversation-root IDs remain visible underneath and
both are searchable. Every identity carries an `OC` or `No OC` tag, and the
left sidebar groups matching sessions by that tag. `OC` means the logical
conversation has persisted Object Context projection or scheduler-decision
evidence; the monitor does not guess an unrecorded historical configuration
state. Search continues to filter both groups at once and omits groups with no
matching runs.

Selecting a run shows 36 charts. The original four groups of four cover
assembled-message token savings, provider-reported prompt-cache hits, provider
API-request latency, and provider-reported tokens spent. One additional chart
isolates conversation-only savings. A separate six-chart V1.1
economic-decision group shows gross removed tokens, Card/receipt footprint,
cache rewrite penalty, known summary cost, normal immediate net, and emergency
immediate net. Normal and emergency values are never combined, and legacy
sessions show V1.1 economic metrics as unavailable instead of fabricated
zeroes.

The independent 13-chart V1.2 group shows Hot Tail raw tokens and
`RAW_UNSEEN` overflow; Pending bucket count, raw tokens, and compression gain;
projected waiting loss `W`; shared rewrite cost `Q`; signed `W - Q` crossing
margin; amortized-crossing, bucket-cap, and token-cap flags; and the V1.1
summary-free full-render immediate flag/net as explicitly counterfactual
comparison telemetry. That counterfactual uses V1.1 route pricing and both
configured thresholds, but never invokes the summary model. Hot,
Pending, `W`, and `Q` are per-decision scheduler-state snapshots. In
particular, `W` and `Q` are threshold inputs for the crossing decision, not
realized token benefit, and the monitor never sums them into savings totals.
V1.2 decisions remain scheduler/version-labelled telemetry; enabling V1.2 does
not relabel or reinterpret historical V1.1 rows. The original savings group
uses per-project, cumulative-project, per-turn, and
cumulative-turn dynamics; the other original groups use request/turn dynamics
because one turn can contain several inferences, including a retrieval
continuation. When Object Context is off, the original savings project/turn
curves are zero-valued and aligned to those universal requests. Its toggle
continues to use `tokens_saved / raw_context_tokens`, preserving historical
sessions and the assembled-message diagnostic.

The extra conversation-only chart has its own token/rate toggle and uses
`conversation_tokens_saved / raw_conversation_tokens`. Projections created
before these scoped metrics existed remain visible in all original charts; the
extra chart alone explains that scoped telemetry is unavailable instead of
silently relabeling the old system-inclusive denominator.

The cache group similarly switches all four request/turn charts between hit
rate and cache-read tokens. A request hit rate is
`cache_read_tokens / prompt_tokens`, where canonical `prompt_tokens` is
`uncached_input + cache_read + cache_write`; cumulative and turn rates divide
summed cache-read tokens by summed prompt tokens, so they are token-weighted.
The run table and global/session KPI rows use the exact SessionDB aggregate even
when a historical per-request curve is unavailable. Cache values come from the
provider response: a zero can mean a measured miss or a provider/proxy that
returned no cache hit details, while a non-zero value is a reported hit.

Every chart has a one-click CSV download containing its currently displayed
complete point series and identities, and the selected-session header has one
combined CSV download for all 36 charts, including every toggle mode. Here,
one project is one request-time Object Context projection, one request is one
successful provider response, and one turn is one real user turn
(all requests/projections carrying that turn identity are summed). Original
saved-token values use the assembled request-message estimate; the additional
conversation chart and `Conversation-only Saved` KPI use persisted history.
Spent-token values are canonical provider prompt plus output usage, and time is
provider API-request latency; neither depends on Object Context. The webpage
abbreviates Token labels with base-1000
`K`/`M`/`B` units regardless of browser locale; CSV downloads retain the raw,
unscaled numeric values.

The monitor is scoped to the user-facing main conversation. Background-review
agents may intentionally reuse the parent's Object Context store and
conversation ID, but their projections are excluded whenever durable main
request turn IDs are available; `background_review` provider usage is also
excluded from the auxiliary ledger, and retrieval totals are restricted to the
retained main turns. Raw telemetry remains in its source databases for audit.
Legacy sessions without request/turn identity retain their historical
projection charts because those rows cannot be classified safely after the
fact.

The dashboard is an offline HTML/CSS/JavaScript/SVG file under the active
profile's `logs/object-context-monitor/` directory. Universal request events are
stored content-free in `state.db` in the same transaction as normal token
accounting. For sessions created before that schema, the monitor can recover
exact numeric request rows still present in the redacted `agent.log`; otherwise
it shows the exact aggregate token/request totals and labels partial curve
coverage instead of inventing per-turn values. The HTML contains stored session
titles plus event identities, timestamps, and numeric telemetry—never prompt
text, messages, Cards, stored objects, or retrieved payloads. Re-run the command
to refresh the snapshot. If the browser cannot be launched, the CLI prints the
exact local file path. A resumed conversation can be monitored immediately,
before its first new model turn, without forcing lazy agent initialization.

When V1 is active and has avoided tokens, the classic CLI status bar also
shows a compact live indicator using the conversation-only scope. Medium-width
terminals show the latest value,
for example `V1↓80K`; widths of at least 110 cells add its estimated
reduction (`V1↓80K 60%`) and the active-conversation cumulative value
(`Σ↓720K`). Below 76 cells the indicator is hidden to preserve the one-line
layout. The repaint path reads an in-memory snapshot only; persisted totals are
restored once when a session starts or resumes.

An active V1 engine remains visible as `V1↓0` in a fresh conversation or when
its projections have not avoided any tokens yet. This distinguishes a valid
zero-savings result from a disabled engine or missing status-bar integration.

| Setting | Meaning |
|---|---|
| `enabled` | Additional activation gate. `/object_context on` sets this true and selects the engine; `off` clears it. |
| `scheduler` | `economic` keeps the V1.1 immediate-next-request policy (default); `amortized_batch` selects the V1.2 bounded Hot Tail/pending policy. |
| `hot_tail_max_inferences` | V1.2 Hot Tail inference-bucket limit (h), default `4`. Pending derives a non-configurable `2h` limit. |
| `hot_tail_max_tokens` | V1.2 Hot Tail raw-token limit (L_hot), default `12800`. Pending derives a non-configurable `2L_hot` limit. |
| `amortized_cache_read_weight` | V1.2 repeated-read weight used in amortized crossing, default `0.10`. It is a cost weight, not saved tokens by itself. |
| `batch_policy` | V1.2 ordinary batching policy: `dynamic` (default W/Q crossing followed by flush-all) or `fixed` (wait for N eligible Pending Deltas, then select the oldest N). It is inert under `scheduler: economic`. |
| `fixed_batch_size` | Positive eligible-Pending-Delta count N used by `batch_policy: fixed`, default `4`. This counts Deltas, not Cards or inference buckets. |
| `min_raw_exposures` | Successful raw provider requests required before a Delta may project; clamped to at least one. |
| `economic_min_net_saving_tokens` | V1.1 normal immediate net-saving gate in uncached-input-equivalent tokens. Under V1.2 it cannot trigger a flush. |
| `economic_min_net_saving_usd` | Optional additional V1.1 USD gate when technical pricing is known; `null` disables it. Under V1.2 it cannot trigger a flush. |
| `economic_cache_read_ratio_fallback` | Cache-read weight used for V1.1 scoring when route pricing is unavailable (and for its labelled V1.2 comparison field). |
| `economic_cache_write_ratio_fallback` | Cache-write weight used for V1.1 scoring when route pricing is unavailable (and for its labelled V1.2 comparison field). |
| `emergency_context_ratio` | Separate request-viability pressure threshold; emergency projection is not normal savings. |
| `object_prefilter_min_tokens` | Cheap minimum object size before registration and Card work. |
| `min_absolute_saving_tokens` | Minimum raw-minus-Card token saving required to publish a Card. |
| `min_relative_saving_ratio` | Minimum proportional saving required in addition to the absolute gate. |
| `card_summary_enabled` | Enables winner-only model summaries only on the V1.1 economic planner: its normal path and the emergency path reused by either scheduler (default `false`). V1.2 amortized-crossing and capacity batches ignore this setting and are always deterministic and summary-free; V1.2 emergency inherits the shared emergency path and therefore still honors it. |
| `summary_max_tokens` | Hard output limit when an eligible economic/emergency Card path generates a type-specific semantic summary. |
| `wm_grace_deltas` | Delta-distance grace window before an unreferenced object becomes evictable. |
| `recent_retrieval_active_deltas` | Delta-distance window during which a retrieved object remains active. |
| `retrieval_max_tokens_ratio` | Largest full object that may be mounted relative to model context; retrieval is never truncated. |

The ordinary `compression.*` settings continue to govern the independent
whole-history summarizer, including its trigger, protected head/tail, model
route, and summary-failure policy.

## Scheduler semantics

With `scheduler: economic`, V1.1 compares the committed raw request `Q0` with
candidate Card requests for the immediate next provider call. A normal batch
flushes only when that immediate score passes the configured gates. There is no
fixed Hot Tail or pending-bucket trigger in this mode.

With `scheduler: amortized_batch`, V1.2 uses
`h = hot_tail_max_inferences` and `L_hot = hot_tail_max_tokens` as an **OR**
bound. While preparing the next inference, the prospective current boundary
occupies one inference position. A previously raw-seen boundary ages out when

```text
next_success_sequence - eligibility_success_sequence >= h
```

Equivalently, if the seen-plus-fresh distinct success buckets exceed `h`, or if
the Hot Tail raw footprint is strictly greater than `L_hot`, V1.2 moves the
oldest eligible raw-seen bucket to pending and repeats until both dimensions are
within their limits. Equality at the token limit is legal. A `RAW_UNSEEN`
bucket cannot be projected merely to satisfy a bound, so an indivisible unseen
bucket may temporarily overflow the Hot Tail and is reported separately. Once
the content has received its required successful raw exposure, being in the
active user turn gives it no additional or unlimited protection.

Pending has hard, derived OR limits: more than `2h` distinct buckets or more
than `2L_hot` raw tokens. Equality is legal. Strictly exceeding either limit
selects a capacity flush of the entire eligible pending batch before the next
provider request; the scheduler may not choose another wait or silently carry
the eligible raw batch. Raw-exposure and Card-legality checks remain mandatory.
The decision priority is `emergency` → `pending capacity` → `amortized
crossing` → `wait`.

Below the hard cap, the scheduler compares the pending batch's amortized
repeated-read credit with its one-time rewrite/Card cost. The fixed intercept is
`F0 = 0` and is deliberately not configurable. The V1.1 summary-free,
full-render immediate score is recorded in dedicated V1.2 counterfactual fields,
using V1.1 route pricing and gates, but it is **not** a fast
path and never triggers a V1.2 normal flush. This first release does not maintain
a second online shadow scheduler. This keeps the policy decision auditable:
V1.2 crosses on its own amortized condition, or on the explicit
emergency/capacity rules.

`batch_policy: dynamic` is the original V1.2 behavior and remains the default:
an amortized crossing selects the complete eligible Pending pool. With
`batch_policy: fixed`, the same Hot Tail promotion, raw-exposure, Card legality,
retrieval, store, and request-accounting machinery remains active, but an
ordinary decision ignores W/Q as a trigger. It waits until at least
`fixed_batch_size` eligible Pending Deltas exist, then selects exactly the
oldest N in canonical `(success_sequence, delta_id)` order. W/Q and the
full-Pending counterfactual score remain recorded for analysis but cannot
trigger that fixed-policy flush.

Capacity and emergency have priority over either ordinary policy. Capacity
continues to flush the complete eligible Pending pool to restore the bounded
queue, so its actual batch can differ from N and is labelled `capacity` rather
than `fixed`. Emergency continues through the shared V1.1 viability planner
and is labelled `emergency`. Consequently, compare fixed-N behavior using only
`decision_mode: fixed` flush rows; `member_delta_ids` is the authoritative
actual batch and `fixed_batch_size` records N on every V1.2 decision row.

## Deltas, objects, and Cards

A real user message is one `user` Delta. Each successful assistant inference,
including all tool calls and matching tool results caused by it, is one
`inference` Delta. Retries do not duplicate Deltas. All schedulers preserve the
raw-exposure rule. Under `economic`, active-turn and historical Deltas use the
same immediate economic rule and no fixed counter triggers normal projection.
Under `amortized_batch`, raw-seen active-turn Deltas participate in the bounded
Hot Tail and pending crossings described above.

V1 recognizes natural complete boundaries using this precedence:

1. runtime metadata (file, attachment, tool, and artifact type);
2. deterministic parser/schema recognition;
3. conservative deterministic heuristics;
4. keep raw when uncertain.

The V1 object types are `code`, `file_content`, `tool_result`, `log`,
`error_trace`, `structured_data` (JSON/YAML/XML/CSV), `table` (Markdown/HTML),
and `artifact`. Ordinary prose, incomplete fences, ambiguous payloads, system
instructions, generated conversation summaries, Cards, Retrieval Cards, and
synthetic control messages are not object-compressed.

When an object passes both benefit gates, its exact original span becomes:

```text
<OBJECT_CARD>
{"contains":...,"object_ref":"object://obj_<id>@v1",...}
</OBJECT_CARD>
```

`contains` is deterministically extracted by AST/parser/schema logic. `origin`
is also deterministic and bounded: it records the source role and, for tool
results, the tool plus a small operation/target hint when available. It never
copies a complete command, patch, URL, or raw payload.

`card_summary_enabled` is scoped to the V1.1 economic planner. With
`scheduler: economic`, its normal path and emergency path honor the setting.
With `scheduler: amortized_batch`, normal dynamic/fixed and capacity batches
always use deterministic, summary-free Cards regardless of the setting; a
V1.2 emergency delegates to the shared economic emergency path and therefore
inherits the setting. On an eligible path with the setting enabled,
the bounded semantic `summary` describes only supported high-level facts and
does not replace either deterministic field. Summaries are generated only for
the provisional winner, and the exact request is rerendered/rechecked before
commit. On a summary-free path the `summary` key is absent and no Card-summary
model request is made. Card bytes are validated and canonicalized before one
atomic epoch publishes the selected batch. Surrounding prose and multimodal
parts stay unchanged.

The setting affects only Cards created after the restarted process loads the
new configuration. Existing immutable Cards are not rewritten, so use a new
session for a clean summary-on versus summary-off comparison.

## Exact retrieval lifecycle

The only model-facing Working Memory operation is:

```text
retrieve_object(object_ref, reason)
```

It resolves the exact immutable `object://obj_<id>@vN` version from Working
Memory or Cold Archive, verifies SHA-256, and returns the complete object as a
normal tool result at the current prompt suffix. The original historical Card
stays in causal position. The retrieval call and exact result commit as one
normal inference Delta and must be consumed raw by one successful provider
request. A later legal scheduler epoch may replace only the tool-result content with a
minimal `schema_version: 1.1` receipt referring to the same immutable object.
Turn completion may close the authorization/activity lease, but does not decide
raw-versus-receipt visibility or force a projection.

V1 deliberately has no semantic object search, within-object search, line/range
read, partial retrieval, `latest` historical reference, inline rehydration, or
sticky cross-turn mount. Missing, malformed, unauthorized, corrupt, unavailable,
or too-large references return a structured `retrieval_error`; they never
return guessed or truncated content.

## Storage, versioning, activity, and recovery

The profile-scoped store is:

```text
<hermes_home>/context/object_context_v1.sqlite3
```

Physical blobs are SHA-256 deduplicated while logical object identity and
immutable versions remain distinct. Historical Cards contain logical refs, not
the physical path or content hash. Explicit edits create `@v2`, `@v3`, and so
on and can record `supersedes`/`derived_from`; similarity never creates a
version relationship automatically. Resolution is authorized to the current
conversation lineage, including session ids created by whole-history
compression rotation.

Activity roots are refs in the actually rendered prompt, workspace/artifact
metadata, pending tool calls, current leases/recent retrievals, and explicit
pins. Unreferenced objects move by Delta distance through `active` →
`inactive_candidate` → `evictable`; eviction changes their logical location to
Cold Archive without invalidating the ref. An optional archive hook receives a
small promotion event containing the raw-source ref, not the raw payload.

On restart, the engine reconciles missed best-effort observations from the
durable raw trace using stable message identities. Reconciliation is
idempotent and never reconstructs old summarized-away text into the current
prompt.

## Failure and cache guarantees

The common transaction order is exact store → successful raw exposure → local
candidate preparation → scheduler decision → optional winner-only summary on
an eligible economic/emergency path → exact rerender/recheck → local atomic
epoch commit → provider send. For V1.1, the scheduler decision is the
immediate-request economic score. For V1.2, it is the
emergency/capacity/amortized-crossing/wait priority above; normal amortized and
capacity paths skip summary generation, while emergency inherits the shared
economic emergency path. V1.1's immediate score remains comparison telemetry
only under V1.2.

The Card epoch is durably committed locally **before** the provider request is
sent. A local transaction failure rolls back the entire candidate epoch and the
provider receives the previously committed `Q0` view; no partial batch or
dangling Card/receipt is published. If that local commit succeeds but the
provider request subsequently fails, Hermes does not roll the Card epoch back:
the retry reuses the already committed view and exact Card bytes. Store,
pricing, estimator, parser, extractor, summary, Card, renderer, resolver, or
integrity failure otherwise remains fail-open to the last committed view.

Each Delta changes at most once from raw to compressed view. V1.1 decisions
compare `Q0` with candidate `Qc` requests using the previous successful
request's reusable prefix and route cache weights, with no future-request or
retrieval forecast. V1.2 adds only its bounded amortized model; it still makes
no retrieval forecast. A route change discards only the in-memory comparison
baseline. Switching the configured scheduler back to `economic` affects future
decisions after restart: it does not rewrite immutable Cards or historical
scheduler-labelled telemetry.

## Status and metrics

The engine status reports the effective scheduler, V1.2 Hot Tail and derived
pending limits when applicable, Delta/object state counts, physical Working
Memory object count/bytes, retrieval count/overhead, never-retrieved object
count, last batch size, content-free decision/normal/emergency projection
counts, the last decision decomposition, aggregate metric totals, and the last
failure category. It never includes prompt/message/Card/object/retrieval payload
content or a physical store path.

Recorded metrics include assembled-message raw/rendered/saved tokens plus the
separate `raw_conversation_tokens`, `rendered_conversation_tokens`,
`conversation_tokens_saved`, and `conversation_compression_ratio` fields;
Card/receipt tokens; detected/externalized/skipped objects; compression and retrieval
failures, retrieved tokens, repeated retrieval rate, consecutive-user-turn
retrievals, mount duration in turns, Working Memory bytes/counts by state, and
exact-recovery hash pass rate. Card
sufficiency and append-vs-baseline capability are evaluation metrics rather
than facts that the runtime can infer safely. Evaluation telemetry additionally
records compression/retrieval latency, retrieval failure codes, provider cache
read/write tokens and hit ratio, and the number and batch size of necessary
raw-to-Card prompt-prefix rewrite events. Every scheduler-labelled wait, normal
flush, failure fallback, and emergency flush also has one durable content-free
record
with Q0/Qc tokens, reusable-prefix loss, cache weights/source, cache penalty,
known summary cost, immediate net, stable member identities, mode, reason, and
epoch identity. Retrieved-payload tokens remain diagnostic because canonical
provider prompt usage already counts them once.
