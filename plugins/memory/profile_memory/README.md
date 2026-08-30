# Profile Memory

`profile_memory` is a deliberately narrow local memory provider for Ansatz. It
stores only durable information about the user and the user's preferred ways
of communicating and working. It does not store arbitrary events, project
resources, whole conversations, or skills. Its profile records are organized
as a normalized virtual directory tree inside SQLite; it is not a general
resource filesystem.

The provider borrows OpenViking's useful separation of evidence, structured
memory units, extraction, and relevance-based recall without running an
OpenViking server. Profile extraction uses the active Ansatz model through the
host-owned `ctx.llm` facade, so the provider neither starts a second generation
model nor receives raw credentials. Semantic recall runs locally with the
small `bge-small-zh-v1.5-f16` GGUF model through `llama-cpp-python`.

## Data model

The SQLite database is authoritative and contains:

- verbatim user evidence with session and turn provenance;
- atomic profile items (`identity`, `interaction_preference`, or
  `workflow_preference`);
- normalized parent/child category nodes beneath stable roots, with dynamic
  open-vocabulary task topics and an explicit applicability condition per item;
- active, candidate, superseded, and revoked states;
- evidence links and distinct-session activation for inferred preferences;
- one local vector per live item and idempotent extraction-run records.

`PROFILE.md` is generated only as a human-readable audit snapshot. Recall never
searches that aggregate Markdown file, so unrelated rules are not forced into
one retrieval unit.

## Category directories

Every item belongs to one normalized category node. The fixed roots preserve a
stable user-profile contract while lower topic segments remain extensible:

```text
profile://
├── identity/
└── preferences/
    ├── interaction/
    │   └── <dynamic communication topics>/
    └── workflow/
        └── <dynamic artifact, development, tool, project, and task topics>/
```

For example, the relative scope `artifacts/latex/equations` on a
`workflow_preference` is stored as
`profile://preferences/workflow/artifacts/latex/equations`. Each path segment
is a row in `profile_scope_nodes`, linked through `parent_id`; profile items
retain the canonical path and the leaf `scope_id`. Existing flat-scope databases
are upgraded in place when opened. The extractor receives the current populated
category paths and must reuse the deepest matching category before creating a
new topic.

`profile_browse` exposes this tree and its atomic items to the user. It supports
the complete tree, a `profile://` subtree, a short relative suffix such as
`artifacts/latex`, and optional kind/status filters. `profile_remember` and
automatic extraction use the same normalized directory writer.

The interactive CLI exposes the same data without a model round-trip:

```text
/profile
/profile latex
/profile preferences/workflow/artifacts/latex
/profile profile://preferences/workflow/artifacts/latex/equations
```

Bare `/profile` renders the complete category directory with direct active and
candidate counts but does not dump every rule. Supplying a category renders
that subtree and its atomic items, including applicability, status, confidence,
and the exact item id needed for correction or revocation. Short category names
are accepted when they identify one unique suffix; ambiguous names return the
candidate full paths. `/profile runtime` retains the previous CLI view of the
active Hermes configuration profile and home directory.

The command reads through the live memory manager when an agent already exists.
Before the first model turn it opens a temporary provider instance in browse
mode, which skips BGE initialization. Both paths read the same authoritative
SQLite database and never call the main model.

## Capture and recall

Explicit durable statements can be written immediately with
`profile_remember`. At session boundaries and before context compression, the
active Ansatz model receives numbered user turns only and emits schema-checked
atomic operations. One-off task instructions, assistant text, secrets, and
generic events are excluded by the extraction contract. Inferred items remain
`candidate` until supported by the configured number of distinct sessions.

Before a non-trivial user turn, an optional intent gate can ask the active
Ansatz model whether any durable profile information could materially help the
request. Only a validated `skip` at or above the configured confidence may
suppress retrieval; unavailable, invalid, low-confidence, and uncertain results
fail open to normal recall. When retrieval proceeds, populated category nodes
are routed against the current request first. The provider then ranks atomic
items inside related branches, while retaining bounded fallbacks for global
interaction/identity preferences and for environments without semantic
embeddings. Local BGE cosine similarity is preferred; lexical matching remains
non-fatal. Only the top bounded results enter the existing per-turn memory
sidecar, with the item id, type, `profile://` category, applicability condition,
and rule.

## Configuration

Select the provider in `$HERMES_HOME/config.yaml`:

```yaml
memory:
  provider: profile_memory
  memory_enabled: false
  user_profile_enabled: false
  profile_memory:
    db_path: $HERMES_HOME/profile_memory.sqlite3
    snapshot_path: $HERMES_HOME/profile_memory/PROFILE.md
    embedding_model_path: $HERMES_HOME/models/profile-memory/bge-small-zh-v1.5-f16.gguf
    semantic_recall: true
    extract_on_session_end: true
    intent_gate: false
    intent_gate_min_skip_confidence: 0.85
    intent_gate_timeout_seconds: 30
    recall_limit: 3
    score_threshold: 0.35
    semantic_only_score_threshold: 0.50
    semantic_score_window: 0.08
    scope_score_threshold: 0.42
    scope_score_window: 0.10
    scope_route_limit: 5
    inferred_activation_sessions: 2
```

`intent_gate` is experimental and defaults to `false`. Enabling it adds one
bounded structured model call before a non-trivial turn when active profile
items exist. It is designed to reduce over-recall for explicit exclusions and
temporary task overrides, not to replace item-level `applies_when` conditions.
The gate deliberately retrieves on uncertainty and failure, so an unavailable
gate cannot make the agent silently forget a relevant preference.

Disabling the two built-in Markdown stores removes the generic `memory` tool
and its frozen `MEMORY.md`/`USER.md` prompt blocks. The profile provider's own
tools and task-time recall remain available.

No provider-specific API key is needed. Main-model extraction follows the
currently active Ansatz provider, model, authentication profile, timeout
routing, and fallback policy. If extraction fails, the turn/session still
completes and the failed run is recorded for retry and diagnosis; no profile
item is invented from an invalid response.
