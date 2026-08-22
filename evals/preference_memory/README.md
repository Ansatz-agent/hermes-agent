# Workflow Preference Evaluation

This is a PrefEval-inspired evaluation platform for durable professional work
preferences.  It measures whether Hermes can retain, retrieve, and correctly
apply instructions such as:

> When returning LaTeX source, keep each prose paragraph on one physical line
> and use the `equation` environment for display equations.

The platform extends PrefEval's preference/query pair with two contracts that
matter for an agent product:

1. **Protocol is explicit.** Replaying the whole conversation tests long-context
   following; starting a fresh session without replaying it tests persistent
   memory. The report never conflates the two.
2. **Work outputs are deterministically graded.** LaTeX environments, physical
   source lines, type annotations, citation commands, headings, and Markdown
   tables are checked by code. An LLM judge is not required for the included
   suite.

The design is based on [PrefEval](https://prefeval.github.io/) and complements
the repository's structured preference-memory implementation.

## Included platform

```text
evals/preference_memory/
├── datasets/workflow_prefeval_v1.json  # 16-case smoke suite
├── fixtures/ideal_responses.json       # grader calibration oracle
├── dataset.py                          # validated public dataset contract
├── graders.py                          # deterministic weighted assertions
├── runner.py                           # replay/full-history/native-memory
├── report.py                           # aggregate Markdown + HTML dashboard
└── results/                            # ignored run artifacts
```

The fixed smoke suite has eight positive/near-neighbor pairs across:

- LaTeX source layout and equation environments;
- Python type annotations and dependency restraint;
- customer-email conventions;
- weekly-report structure;
- LaTeX citation commands;
- missing-file analysis workflow;
- technical language and temporary overrides; and
- conditional Markdown-table use.

It intentionally calls itself `1.0-smoke`: it validates the platform and
provides meaningful regression coverage, but it is not yet the experiment
document's release dataset of at least 40 preference/task pairs.

`fixtures/ideal_responses.json` is a grader-calibration oracle, not a model
baseline. Every response in it must receive a strict pass; this makes broken
or impossible assertions fail CI before a paid run begins.

## Protocols

### `replay`: offline grading

Use this for responses collected from any agent or UI. It makes no model calls.
The response JSON maps a case id—or a case id plus distance—to response text:

```json
{
  "latex_source_positive@30": "模型设定如下：\n\\begin{equation}\ny_i=\\mu_i+\\varepsilon_i.\n\\end{equation}\n其中，$\\varepsilon_i\\sim\\mathcal{N}(0,\\sigma^2)$。",
  "latex_source_negative@30": "样本均值是把样本中的数值相加后除以样本数量。它用一个数概括样本的平均水平。"
}
```

Run:

```bash
python3 evals/preference_memory/runner.py \
  --protocol replay \
  --responses /path/to/responses.json \
  --tasks latex_source_positive,latex_source_negative \
  --distances 30 \
  --label offline-smoke
```

To calibrate all included graders without a model call:

```bash
python3 evals/preference_memory/runner.py \
  --protocol replay \
  --responses evals/preference_memory/fixtures/ideal_responses.json \
  --distances 0 \
  --label grader-calibration
```

If a response is keyed only by case id, it is reused at every requested
distance. This is convenient for grader tests, but distance-specific keys are
recommended for real experiments.

### `full-history`: PrefEval-style long-context evaluation

The runner constructs:

```text
preference setup → N deterministic distractor exchanges → final probe
```

and gives the complete history to a real Hermes `AIAgent`. Only the final probe
is submitted as a live agent turn, matching the economical PrefEval-style
setup; if the agent elects to use tools, the manifest records the resulting
additional API iterations rather than pretending the turn was a single call.

```bash
# Source the provider credential into the current environment first.
set -a
source ~/.hermes/.env
set +a

python3 evals/preference_memory/runner.py \
  --protocol full-history \
  --model anthropic/claude-sonnet-4.6 \
  --provider openrouter \
  --distances 0,10,30,100 \
  --reps 3 \
  --label full-history-baseline
```

Here, a distance is the number of complete intervening user/assistant
exchanges, not individual messages. Distractors are chosen from the fixed pool
with a stable digest and seed, so every compared variant sees the same history.

### `native-memory`: real cross-session memory evaluation

This protocol sends the preference through a real Hermes session, closes it so
the configured provider commits the conversation, sends distractors through
additional sessions, and issues the probe in a fresh session without replaying
the old transcript.

The runner uses a temporary `HERMES_HOME` per case. It inherits the selected
model routing and memory-provider options from the current raw Hermes config,
but replaces profile database/snapshot paths with paths inside that temporary
home. An inline model API key, when present, is passed directly to `AIAgent`
and is never copied into the temporary config or result manifest; environment
credentials remain preferable. `--base-url` can override a stale endpoint for
one run without mutating the user's config. Each OpenViking run receives a
unique `OPENVIKING_AGENT` namespace, so variants do not contaminate each other
or normal user memories.

The runner deliberately does not delete those remote evaluation namespaces:
automatic cleanup would be a destructive operation against the configured
OpenViking service. They can be removed later through the service's normal
administrative workflow after the result manifests are no longer needed.

```bash
set -a
source ~/.hermes/.env
set +a

# Control: normal OpenViking recall.
python3 evals/preference_memory/runner.py \
  --protocol native-memory \
  --variant control \
  --memory-provider openviking \
  --model anthropic/claude-sonnet-4.6 \
  --provider openrouter \
  --distances 0,10,30 \
  --turns-per-session 5 \
  --reps 3 \
  --label openviking-control

# Experiment: structured preference-intent recall and reserved quota.
python3 evals/preference_memory/runner.py \
  --protocol native-memory \
  --variant structured \
  --memory-provider openviking \
  --model anthropic/claude-sonnet-4.6 \
  --provider openrouter \
  --distances 0,10,30 \
  --turns-per-session 5 \
  --reps 3 \
  --label openviking-structured
```

Native-memory is deliberately expensive: a distance of 30 makes 30 real
distractor requests per case and repetition. Start with one positive and its
negative neighbor:

```bash
--tasks latex_source_positive,latex_source_negative --distances 0,10 --reps 1
```

For retention/storage tests where the benchmark's fixed assistant distractor
replies are sufficient, `--native-distractor-mode fixture` commits those
completed user/assistant transcripts through the real provider's session-end
extraction path. Setup and probe responses remain live. This avoids generating
paid replies whose text is already fixed by the dataset:

```bash
--native-distractor-mode fixture --distances 30 --turns-per-session 30
```

Provider-internal extraction calls are not currently included in the runner's
token counters; the counters cover the host `AIAgent` calls. The default mode
is `live`, which remains the appropriate choice when the distractor replies
themselves are part of the behavior under test.

Terminal model-provider failures (`failed: true`, such as an exhausted HTTP
503 retry sequence) are recorded as infrastructure errors and excluded from
valid behavioral aggregates. They are never graded as memory failures.

For an isolated A/B of the experimental `profile_memory` pre-retrieval gate,
override only the disposable benchmark config:

```bash
--memory-provider profile_memory --profile-intent-gate on
```

Use `off` for a forced control or `inherit` (the default) to copy the current
provider setting. Result manifests and per-record metrics identify the selected
mode and count gate decisions, skips, and fail-open events.
Gate input/output/total tokens are reported separately from the host agent's
normal generation-token counters.

The control and structured commands must use the same model, seed, distances,
task list, repetition count, and `--turns-per-session` value.
The report will only calculate an A/B delta when those settings, the memory
provider, temperature, and dataset hash match exactly; incompatible runs remain
visible as standalone summaries instead of being silently compared.

## Reports

Each run writes:

```text
results/<label>/<protocol>/<variant>/<model>/rep<N>.json
```

Generate a console summary, Markdown artifact, and self-contained HTML report:

```bash
python3 evals/preference_memory/report.py \
  --inputs \
    'evals/preference_memory/results/openviking-control/**/*.json' \
    'evals/preference_memory/results/openviking-structured/**/*.json' \
  --markdown /tmp/workflow-prefeval.md \
  --html /tmp/workflow-prefeval.html
```

The report includes:

- weighted mean score;
- strict all-rules pass rate;
- correct application on positive tasks;
- false application on near-neighbor negative tasks;
- preference recall when native-memory context is observable;
- control-versus-structured deltas and diagnostic partial gates when both
  variants are present;
- retention by intervening distance;
- category breakdown;
- most frequently failed assertions;
- errors, token use, and p50/p95 wall time.

Use `--save-transcripts` only when detailed debugging is needed. By default,
result files retain final responses and metrics but omit full message histories,
recalled context, and setup responses.

## Dataset contract

The root object has `schema_version`, metadata, a reusable distractor pool, and
cases. A case contains:

```json
{
  "id": "stable_case_id",
  "category": "open-vocabulary-scope",
  "preference_form": "explicit",
  "setup": [{"user": "durable preference", "assistant": "acknowledgement"}],
  "probe": "a later task that does not repeat the preference",
  "applicable": true,
  "expected_memory_markers": ["required marker", ["中文写法", "English spelling"]],
  "assertions": [
    {"id": "rule_name", "kind": "contains_all", "values": ["required text"], "weight": 2}
  ]
}
```

Each top-level memory-marker entry is a required recall concept. A plain
string requires that literal marker; a nested list supplies accepted lexical
alternatives for the same concept. This keeps retrieval diagnostics stable
when a profile extractor preserves the preference correctly but normalizes it
into another language.

On a negative near-neighbor, mark the assertion that detects inappropriate
preference use with `"metric": "scope_control"`. Other quality checks still
affect strict pass rate, but do not inflate the false-application metric.

Supported deterministic assertion kinds are:

- `contains_all`, `contains_any`, `not_contains_any`;
- `regex`, `not_regex`, `ordered_contains`;
- `source_only`;
- `latex_prose_single_line`;
- `max_chars`;
- `markdown_table`, `no_markdown_table`.

Every positive should have a near-neighbor negative with the same stored
preference. That pairing is what distinguishes correct personalization from
blindly applying every remembered preference everywhere.

## Interpretation

Keep these failure classes separate:

| Observation | Likely failure |
|---|---|
| Full-history fails | Preference inference or behavior execution |
| Full-history passes, native recall false | Memory capture/retrieval |
| Native recall true, output fails | Memory-to-behavior utilization |
| Positive passes, negative fails | Scope control / over-personalization |
| Control passes, structured fails | Experimental recall regression |
| Oracle/reminded output fails | Immediate instruction following, not memory |

Do not claim a memory improvement from retrieval recall alone. The release gate
is downstream correct application with controlled false application and latency.
