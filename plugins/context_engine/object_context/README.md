# Object Context V1

Object Context implements **Context Compression Strategy V1** as a bundled,
opt-in Context Engine. It is an object-virtualization layer, not a replacement
for conversation summarization:

- large, reliably bounded structured objects are stored exactly;
- their historical span is replaced in-place by a stable versioned Card after
  the containing Delta leaves the configurable Hot Tail;
- the agent can load one known immutable reference with `retrieve_object`;
- the exact result stays mounted only for the current real user turn, then its
  tool result renders as a compact Retrieval Card;
- Hermes' existing whole-history summarizer continues to run independently and
  consumes the same Card-rendered recent context that the normal provider sees.

The persisted user/assistant/tool trace remains raw and authoritative. Cards
are request-time projections and are never written over the transcript.

## Enable and configure

Select the engine through `hermes plugins`, or edit `~/.hermes/config.yaml`:

```yaml
context:
  engine: object_context
  object_context:
    hot_tail_max_deltas: 8
    hot_tail_token_budget_ratio: 0.25
    context_soft_limit_ratio: 0.75
    object_prefilter_min_tokens: 256
    min_absolute_saving_tokens: 128
    min_relative_saving_ratio: 0.25
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
/object_context set hot_tail_max_deltas 4
/object_context reset hot_tail_max_deltas
/object_context reset all
/object_context help
```

The command validates only the settings listed below and persists them to the
active profile's `config.yaml`. It never hot-swaps the Context Engine or tool
schema inside a live conversation; restart the CLI after a change. `set` does
not implicitly enable V1, and `reset` removes explicit overrides so the merged
code defaults apply again.

`/object_context stats` is read-only and reports the most recent request
projection plus cumulative request-projection savings for the active
conversation. These values deliberately exclude one-time per-Delta Card-build
metrics, avoiding double counting. They are rough conversation-message token
estimates, not provider-billing totals; retrieved payload is reported
separately and is already reflected in the rendered projection.

`/object_context monitor` writes a private, self-contained HTML snapshot for
every conversation root with Object Context V1 projection telemetry and opens
it in the default browser. Its experiment-tracking workspace provides global
KPIs, a searchable session/run list and comparison table, and selects the
current conversation by default. Stored Hermes session titles are the primary
labels; stable conversation-root IDs remain visible underneath and both are
searchable. Selecting a run switches the same four groups of four charts:
tokens saved, provider-reported prompt-cache hits, Object Context projection
time, and rendered-context tokens spent. Projection groups show per-project,
cumulative-project, per-turn, and cumulative-turn dynamics. Cache charts use
model requests instead of projections because one turn can contain several
inferences, including a retrieval continuation. The saved-token group has one
toggle that switches all four charts between absolute tokens avoided and
relative savings percentage (`tokens_saved / raw_context_tokens`); cumulative
percentages divide cumulative saved tokens by cumulative raw tokens rather
than adding or averaging rates.

The cache group similarly switches all four request/turn charts between hit
rate and cache-read tokens. A request hit rate is
`cache_read_tokens / prompt_tokens`, where canonical `prompt_tokens` is
`uncached_input + cache_read + cache_write`; cumulative and turn rates divide
summed cache-read tokens by summed prompt tokens, so they are token-weighted.
The run table and global/session KPI rows show this weighted rate, and display
an em dash when a stored run has no exact request-level cache telemetry. Cache
values come from the provider response: a zero can mean a measured miss or a
provider/proxy that returned no cache hit details, while a non-zero value is a
reported hit.

Every chart has a one-click CSV download containing its currently displayed
complete point series and identities, and the selected-session header has one
combined CSV download for all 16 charts, including both saved-token and cache
modes. Here,
one project is one request-time Object Context projection, one turn is one real
user turn (all projections in that turn are summed), and projection time is
local Object Context processing time only—it excludes model and network
latency. Saved/spent token values are the same rough conversation-message
estimates used by `stats`, not provider-billed usage; cache values are canonical
provider usage buckets. The webpage abbreviates Token labels with base-1000
`K`/`M`/`B` units regardless of browser locale; CSV downloads retain the raw,
unscaled numeric values.

The dashboard is an offline HTML/CSS/JavaScript/SVG file under the active
profile's `logs/object-context-monitor/` directory. It contains stored session
titles plus event identities, timestamps, and numeric telemetry—never prompt
text, messages, Cards, stored objects, or retrieved payloads. Re-run the command
to refresh the snapshot. If the browser cannot be launched, the CLI prints the
exact local file path. A resumed conversation can be monitored immediately,
before its first new model turn: the command resolves the persisted
conversation lineage and reads the profile's complete V1 telemetry without
forcing lazy agent initialization.

When V1 is active and has avoided tokens, the classic CLI status bar also
shows a compact live indicator. Medium-width terminals show the latest value,
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
| `hot_tail_max_deltas` | Maximum recent Deltas retained raw, subject to token pressure and active-turn protection. |
| `hot_tail_token_budget_ratio` | Fraction of model context available to the raw Object Hot Tail. |
| `context_soft_limit_ratio` | Prompt pressure point that can move older non-active Deltas out early. |
| `object_prefilter_min_tokens` | Cheap minimum object size before registration and Card work. |
| `min_absolute_saving_tokens` | Minimum raw-minus-Card token saving required to publish a Card. |
| `min_relative_saving_ratio` | Minimum proportional saving required in addition to the absolute gate. |
| `summary_max_tokens` | Hard output limit for a Card's type-specific semantic summary. |
| `wm_grace_deltas` | Delta-distance grace window before an unreferenced object becomes evictable. |
| `recent_retrieval_active_deltas` | Delta-distance window during which a retrieved object remains active. |
| `retrieval_max_tokens_ratio` | Largest full object that may be mounted relative to model context; retrieval is never truncated. |

The ordinary `compression.*` settings continue to govern the independent
whole-history summarizer, including its trigger, protected head/tail, model
route, and summary-failure policy.

## Deltas, objects, and Cards

A real user message is one `user` Delta. Each successful assistant inference,
including all tool calls and matching tool results caused by it, is one
`inference` Delta. Retries do not duplicate Deltas and the actively executing
reasoning chain stays raw even if it exceeds the configured Hot Tail budget.

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

`contains` is deterministically extracted by AST/parser/schema logic. The
bounded summary describes only supported high-level facts and does not replace
the structural index. Card bytes are validated and canonicalized before one
atomic batch publishes newly cold Deltas. Surrounding prose and multimodal
parts stay unchanged.

## Exact retrieval lifecycle

The only model-facing Working Memory operation is:

```text
retrieve_object(object_ref, reason)
```

It resolves the exact immutable `object://obj_<id>@vN` version from Working
Memory or Cold Archive, verifies SHA-256, and returns the complete object as a
normal tool result at the current prompt suffix. The original historical Card
stays in causal position. The result remains visible to every later inference
in that real user turn. At turn completion the lease is removed; later prompts
retain the call/result pair but render the result as a stable Retrieval Card.

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

The transaction order is exact store → integrity check → structural extraction
and bounded summary → Card validation → compressed-view commit. Store, parser,
extractor, summary, Card, renderer, commit, resolver, or integrity failure is
fail-open for the conversation: the raw view remains the context source and no
dangling Card is published.

Each normal historical Delta changes at most once from raw to compressed view.
Retrieval changes only the current prompt suffix, and its next-turn unload does
not rewrite the older Card prefix. This keeps prompt-cache churn bounded to
real batch-compression boundaries.

## Status and metrics

The engine status reports V1 availability, Delta/object state counts, physical
Working Memory object count/bytes, retrieval count/overhead, never-retrieved
object count, Hot Tail size, last batch size, aggregate metric totals, and the
last failure category. It never includes raw object content or a physical store
path.

Recorded metrics include raw/rendered/Hot Tail/Card/saved tokens, compression
ratio, detected/externalized/skipped objects, compression and retrieval
failures, retrieved tokens, repeated retrieval rate, consecutive-user-turn
retrievals, mount duration in turns, Working Memory bytes/counts by state, and
exact-recovery hash pass rate. Card
sufficiency and append-vs-baseline capability are evaluation metrics rather
than facts that the runtime can infer safely. Evaluation telemetry additionally
records compression/retrieval latency, retrieval failure codes, provider cache
read/write tokens and hit ratio, and the number and batch size of necessary
raw-to-Card prompt-prefix rewrite events. Summary length and Hot Tail size are
configurable ablation knobs. Inline rehydration and sticky retrieval remain
deliberately absent because V1's frozen scope defines append-only,
turn-scoped retrieval as the implementation under test.
