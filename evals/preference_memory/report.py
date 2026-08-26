"""Aggregate preference-memory eval results and render Markdown/HTML reports."""

from __future__ import annotations

import argparse
import glob
import html
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable


@dataclass(frozen=True)
class ResultSet:
    label: str
    protocol: str
    variant: str
    model: str
    provider: str
    dataset: str
    records: tuple[dict[str, Any], ...]
    files: tuple[str, ...]
    comparison_signature: str = ""

    @property
    def key(self) -> str:
        return f"{self.label} · {self.protocol} · {self.variant} · {self.model or 'offline'}"


def _expand_inputs(patterns: Iterable[str]) -> list[Path]:
    paths: list[Path] = []
    for pattern in patterns:
        matches = [Path(item) for item in glob.glob(pattern, recursive=True)]
        if not matches and Path(pattern).is_file():
            matches = [Path(pattern)]
        paths.extend(matches)
    unique = sorted({path.resolve() for path in paths if path.is_file()})
    if not unique:
        raise SystemExit("no result JSON files matched --inputs")
    return unique


def load_result_sets(patterns: Iterable[str]) -> list[ResultSet]:
    grouped: dict[tuple[str, ...], dict[str, Any]] = {}
    for path in _expand_inputs(patterns):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"cannot read result file {path}: {exc}") from exc
        run = payload.get("run") or {}
        dataset = payload.get("dataset") or {}
        if not isinstance(run, dict) or not isinstance(dataset, dict):
            raise SystemExit(f"invalid result manifest in {path}")
        key = (
            str(run.get("label", "")),
            str(run.get("protocol", "")),
            str(run.get("variant", "")),
            str(run.get("model", "")),
            str(run.get("provider", "")),
            f"{dataset.get('name', '')}@{dataset.get('version', '')}",
            json.dumps(
                {
                    "dataset_sha256": dataset.get("sha256"),
                    "memory_provider": run.get("memory_provider"),
                    "base_url": run.get("base_url"),
                    "temperature": run.get("temperature"),
                    "turns_per_session": run.get("turns_per_session"),
                    "native_distractor_mode": run.get("native_distractor_mode"),
                    "profile_intent_gate": run.get("profile_intent_gate"),
                    "distances": run.get("distances"),
                    "case_ids": run.get("case_ids"),
                    "seed": run.get("seed"),
                    "isolated_config_sha256": run.get("isolated_config_sha256"),
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        bucket = grouped.setdefault(key, {"records": [], "files": []})
        records = payload.get("records")
        if not isinstance(records, list):
            raise SystemExit(f"result file has no records list: {path}")
        bucket["records"].extend(
            record for record in records if isinstance(record, dict)
        )
        bucket["files"].append(str(path))

    result_sets = []
    for key, bucket in sorted(grouped.items()):
        label, protocol, variant, model, provider, dataset, signature = key
        result_sets.append(
            ResultSet(
                label=label,
                protocol=protocol,
                variant=variant,
                model=model,
                provider=provider,
                dataset=dataset,
                records=tuple(bucket["records"]),
                files=tuple(bucket["files"]),
                comparison_signature=signature,
            )
        )
    return result_sets


def _percent(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
    return ordered[index]


def _false_application(record: dict[str, Any]) -> bool:
    """Distinguish preference overreach from unrelated negative-case errors."""
    grade = record.get("grade") or {}
    assertions = grade.get("assertions") or []
    scope_assertions = [
        item
        for item in assertions
        if isinstance(item, dict) and item.get("metric") == "scope_control"
    ]
    if scope_assertions:
        return any(not item.get("passed") for item in scope_assertions)
    # Backward compatibility for result files made before metric annotations.
    return not bool(record.get("passed"))


def summarize(result_set: ResultSet) -> dict[str, Any]:
    records = list(result_set.records)
    valid = [record for record in records if not record.get("error")]
    positive = [record for record in valid if record.get("applicable") is True]
    negative = [record for record in valid if record.get("applicable") is False]
    recalled = [
        record for record in positive if record.get("memory_recalled") is not None
    ]
    walls = [float(record.get("wall_s", 0)) for record in valid]
    tokens = [
        int(record.get("total_tokens", 0))
        for record in valid
        if record.get("total_tokens") is not None
    ]

    assertion_failures: Counter[str] = Counter()
    for record in valid:
        grade = record.get("grade") or {}
        for assertion in grade.get("assertions") or []:
            if isinstance(assertion, dict) and not assertion.get("passed"):
                assertion_failures[str(assertion.get("id", "unknown"))] += 1

    by_distance: dict[int, list[dict[str, Any]]] = defaultdict(list)
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in valid:
        by_distance[int(record.get("distance", 0))].append(record)
        by_category[str(record.get("category", "unknown"))].append(record)

    return {
        "key": result_set.key,
        "dataset": result_set.dataset,
        "records": len(records),
        "valid_records": len(valid),
        "error_rate": _percent(len(records) - len(valid), len(records)),
        "mean_score": mean(float(record.get("score", 0)) for record in valid)
        if valid
        else None,
        "strict_pass_rate": _percent(
            sum(bool(record.get("passed")) for record in valid), len(valid)
        ),
        "correct_application": _percent(
            sum(bool(record.get("passed")) for record in positive), len(positive)
        ),
        "false_application": _percent(
            sum(_false_application(record) for record in negative), len(negative)
        ),
        "preference_recall": _percent(
            sum(bool(record.get("memory_recalled")) for record in recalled),
            len(recalled),
        ),
        "wall_p50": median(walls) if walls else None,
        "wall_p95": _quantile(walls, 0.95),
        "mean_tokens": mean(tokens) if tokens else None,
        "assertion_failures": assertion_failures,
        "by_distance": {
            distance: {
                "n": len(items),
                "score": mean(float(item.get("score", 0)) for item in items),
                "pass_rate": _percent(
                    sum(bool(item.get("passed")) for item in items), len(items)
                ),
            }
            for distance, items in sorted(by_distance.items())
        },
        "by_category": {
            category: {
                "n": len(items),
                "score": mean(float(item.get("score", 0)) for item in items),
                "pass_rate": _percent(
                    sum(bool(item.get("passed")) for item in items), len(items)
                ),
            }
            for category, items in sorted(by_category.items())
        },
    }


def compare_variants(result_sets: list[ResultSet]) -> list[dict[str, Any]]:
    """Aggregate comparable control/structured runs and compute A/B deltas."""
    buckets: dict[tuple[str, ...], dict[str, list[ResultSet]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for result_set in result_sets:
        if result_set.variant not in {"control", "structured"}:
            continue
        key = (
            result_set.protocol,
            result_set.model,
            result_set.provider,
            result_set.dataset,
            result_set.comparison_signature,
        )
        buckets[key][result_set.variant].append(result_set)

    comparisons: list[dict[str, Any]] = []
    for key, variants in sorted(buckets.items()):
        if not variants["control"] or not variants["structured"]:
            continue

        def combined_summary(variant: str) -> dict[str, Any]:
            members = variants[variant]
            combined = ResultSet(
                label=" + ".join(item.label for item in members),
                protocol=key[0],
                variant=variant,
                model=key[1],
                provider=key[2],
                dataset=key[3],
                records=tuple(record for item in members for record in item.records),
                files=tuple(path for item in members for path in item.files),
                comparison_signature=key[4],
            )
            return summarize(combined)

        control = combined_summary("control")
        structured = combined_summary("structured")

        def delta(metric: str) -> float | None:
            left = control[metric]
            right = structured[metric]
            return None if left is None or right is None else right - left

        latency_ratio = None
        if control["wall_p95"] and structured["wall_p95"] is not None:
            latency_ratio = structured["wall_p95"] / control["wall_p95"]
        recall_delta = delta("preference_recall")
        gates = {
            "preference_recall": (
                None
                if structured["preference_recall"] is None or recall_delta is None
                else structured["preference_recall"] >= 0.90 and recall_delta >= 0.10
            ),
            "correct_application": (
                None
                if structured["correct_application"] is None
                else structured["correct_application"] >= 0.90
            ),
            "false_application": (
                None
                if structured["false_application"] is None
                else structured["false_application"] <= 0.03
            ),
            "p95_latency": (None if latency_ratio is None else latency_ratio <= 1.35),
        }
        comparisons.append(
            {
                "key": " · ".join([key[0], key[1] or "unspecified model", key[3]]),
                "control": control,
                "structured": structured,
                "mean_score_delta": delta("mean_score"),
                "correct_application_delta": delta("correct_application"),
                "false_application_delta": delta("false_application"),
                "preference_recall_delta": recall_delta,
                "p95_latency_ratio": latency_ratio,
                "gates": gates,
            }
        )
    return comparisons


def _fmt_percent(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _fmt_number(value: float | None, digits: int = 3) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def _fmt_delta(value: float | None, *, percent: bool = True) -> str:
    if value is None:
        return "—"
    scaled = value * 100 if percent else value
    suffix = " pp" if percent else ""
    return f"{scaled:+.1f}{suffix}"


def _gate_summary(gates: dict[str, bool | None]) -> str:
    measured = [value for value in gates.values() if value is not None]
    if not measured:
        return "not measured"
    return f"{sum(measured)}/{len(measured)} partial gates"


def render_markdown(result_sets: list[ResultSet]) -> str:
    summaries = [summarize(item) for item in result_sets]
    comparisons = compare_variants(result_sets)
    lines = [
        "# Workflow Preference Evaluation Report",
        "",
        "## Overall",
        "",
        "| Run | N | Mean score | Strict pass | Correct application | False application | Preference recall | Errors | p50 / p95 | Mean tokens |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for summary in summaries:
        latency = (
            "—"
            if summary["wall_p50"] is None
            else f"{summary['wall_p50']:.1f}s / {summary['wall_p95']:.1f}s"
        )
        tokens = (
            "—" if summary["mean_tokens"] is None else f"{summary['mean_tokens']:,.0f}"
        )
        lines.append(
            "| {key} | {n} | {score} | {strict} | {correct} | {false} | {recall} | {errors} | {latency} | {tokens} |".format(
                key=summary["key"],
                n=summary["records"],
                score=_fmt_number(summary["mean_score"]),
                strict=_fmt_percent(summary["strict_pass_rate"]),
                correct=_fmt_percent(summary["correct_application"]),
                false=_fmt_percent(summary["false_application"]),
                recall=_fmt_percent(summary["preference_recall"]),
                errors=_fmt_percent(summary["error_rate"]),
                latency=latency,
                tokens=tokens,
            )
        )

    if comparisons:
        lines.extend(
            [
                "",
                "## Control vs structured",
                "",
                "| Comparable runs | Δ mean score | Δ correct application | Δ false application | Δ preference recall | p95 latency ratio | Partial gates |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for comparison in comparisons:
            latency = (
                "—"
                if comparison["p95_latency_ratio"] is None
                else f"{comparison['p95_latency_ratio']:.2f}×"
            )
            lines.append(
                "| {key} | {score} | {correct} | {false} | {recall} | {latency} | {gates} |".format(
                    key=comparison["key"],
                    score=_fmt_delta(comparison["mean_score_delta"], percent=False),
                    correct=_fmt_delta(comparison["correct_application_delta"]),
                    false=_fmt_delta(comparison["false_application_delta"]),
                    recall=_fmt_delta(comparison["preference_recall_delta"]),
                    latency=latency,
                    gates=_gate_summary(comparison["gates"]),
                )
            )
        lines.extend(
            [
                "",
                "Partial gates cover recall, correct application, false application, and p95 latency only. They are diagnostic, not a release verdict: recall precision, context limits, timeout violations, and the required release-set size are evaluated separately.",
            ]
        )

    lines.extend(["", "## Retention by intervening distance", ""])
    all_distances = sorted(
        {distance for summary in summaries for distance in summary["by_distance"]}
    )
    lines.append(
        "| Run | "
        + " | ".join(f"{distance} exchanges" for distance in all_distances)
        + " |"
    )
    lines.append("|---|" + "---:|" * len(all_distances))
    for summary in summaries:
        cells = []
        for distance in all_distances:
            item = summary["by_distance"].get(distance)
            cells.append(
                "—"
                if item is None
                else f"{item['score']:.3f} / {_fmt_percent(item['pass_rate'])}"
            )
        lines.append(f"| {summary['key']} | " + " | ".join(cells) + " |")

    for summary in summaries:
        lines.extend(["", f"## {summary['key']}", "", "### Categories", ""])
        lines.extend(
            ["| Category | N | Mean score | Pass rate |", "|---|---:|---:|---:|"]
        )
        for category, item in summary["by_category"].items():
            lines.append(
                f"| {category} | {item['n']} | {item['score']:.3f} | {_fmt_percent(item['pass_rate'])} |"
            )
        lines.extend(["", "### Most frequent failed assertions", ""])
        failures = summary["assertion_failures"].most_common(12)
        if failures:
            lines.extend([f"- `{name}`: {count}" for name, count in failures])
        else:
            lines.append("- None")
    lines.append("")
    return "\n".join(lines)


def _metric_card(label: str, value: str, tone: str = "") -> str:
    return (
        f'<div class="metric {html.escape(tone)}"><span>{html.escape(label)}</span>'
        f"<strong>{html.escape(value)}</strong></div>"
    )


def _bar(value: float | None) -> str:
    if value is None:
        return '<span class="muted">—</span>'
    display = max(0.0, min(1.0, value))
    tone = "good" if display >= 0.9 else "warn" if display >= 0.7 else "bad"
    return (
        f'<div class="bar"><i class="{tone}" style="width:{display * 100:.1f}%"></i></div>'
        f"<small>{value * 100:.1f}%</small>"
    )


def render_html(result_sets: list[ResultSet]) -> str:
    summaries = [summarize(item) for item in result_sets]
    comparisons = compare_variants(result_sets)
    comparison_rows = "".join(
        "<tr><td>{key}</td><td>{score}</td><td>{correct}</td><td>{false}</td><td>{recall}</td><td>{latency}</td><td>{gates}</td></tr>".format(
            key=html.escape(comparison["key"]),
            score=html.escape(
                _fmt_delta(comparison["mean_score_delta"], percent=False)
            ),
            correct=html.escape(_fmt_delta(comparison["correct_application_delta"])),
            false=html.escape(_fmt_delta(comparison["false_application_delta"])),
            recall=html.escape(_fmt_delta(comparison["preference_recall_delta"])),
            latency=(
                "—"
                if comparison["p95_latency_ratio"] is None
                else f"{comparison['p95_latency_ratio']:.2f}×"
            ),
            gates=html.escape(_gate_summary(comparison["gates"])),
        )
        for comparison in comparisons
    )
    comparison_section = (
        ""
        if not comparison_rows
        else f"""
        <section><div class="section-head"><div><p>A/B diagnostic</p><h2>Control vs structured</h2></div></div>
        <article><table><thead><tr><th>Comparable runs</th><th>Δ score</th><th>Δ correct</th><th>Δ false</th><th>Δ recall</th><th>p95 ratio</th><th>Partial gates</th></tr></thead><tbody>{comparison_rows}</tbody></table>
        <p class="note">Partial gates exclude recall precision, context and timeout limits, and release-set size; they are not a rollout verdict.</p></article></section>
        """
    )
    sections = []
    for summary in summaries:
        distance_rows = "".join(
            "<tr><td>{distance}</td><td>{score:.3f}</td><td>{bar}</td><td>{n}</td></tr>".format(
                distance=distance,
                score=item["score"],
                bar=_bar(item["pass_rate"]),
                n=item["n"],
            )
            for distance, item in summary["by_distance"].items()
        )
        category_rows = "".join(
            "<tr><td>{category}</td><td>{score:.3f}</td><td>{bar}</td><td>{n}</td></tr>".format(
                category=html.escape(category),
                score=item["score"],
                bar=_bar(item["pass_rate"]),
                n=item["n"],
            )
            for category, item in summary["by_category"].items()
        )
        failures = summary["assertion_failures"].most_common(12)
        failure_list = (
            "".join(
                f"<li><code>{html.escape(name)}</code><b>{count}</b></li>"
                for name, count in failures
            )
            or "<li>None</li>"
        )
        cards = "".join(
            [
                _metric_card("Mean score", _fmt_number(summary["mean_score"])),
                _metric_card("Strict pass", _fmt_percent(summary["strict_pass_rate"])),
                _metric_card(
                    "Correct application", _fmt_percent(summary["correct_application"])
                ),
                _metric_card(
                    "False application", _fmt_percent(summary["false_application"])
                ),
                _metric_card(
                    "Preference recall", _fmt_percent(summary["preference_recall"])
                ),
                _metric_card("Errors", _fmt_percent(summary["error_rate"])),
            ]
        )
        sections.append(
            f"""
            <section>
              <div class="section-head"><div><p>{html.escape(summary['dataset'])}</p><h2>{html.escape(summary['key'])}</h2></div><b>{summary['records']} records</b></div>
              <div class="metrics">{cards}</div>
              <div class="grid">
                <article><h3>Retention curve</h3><table><thead><tr><th>Intervening exchanges</th><th>Score</th><th>Pass rate</th><th>N</th></tr></thead><tbody>{distance_rows}</tbody></table></article>
                <article><h3>Categories</h3><table><thead><tr><th>Category</th><th>Score</th><th>Pass rate</th><th>N</th></tr></thead><tbody>{category_rows}</tbody></table></article>
              </div>
              <article><h3>Frequent failed assertions</h3><ul class="failures">{failure_list}</ul></article>
            </section>
            """
        )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Workflow Preference Evaluation</title>
<style>
:root{{--ink:#17202a;--muted:#64748b;--paper:#f5f7fb;--card:#fff;--line:#dbe2ea;--brand:#3157d5;--good:#169c62;--warn:#df9a18;--bad:#d84b4b}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.5 ui-sans-serif,system-ui,-apple-system,sans-serif}}main{{max-width:1320px;margin:auto;padding:48px 28px 80px}}header{{margin-bottom:32px}}header p,.section-head p{{color:var(--muted);margin:0 0 5px;text-transform:uppercase;letter-spacing:.09em;font-size:12px}}h1{{font-size:38px;line-height:1.1;margin:0}}section,article{{background:var(--card);border:1px solid var(--line);border-radius:16px}}section{{padding:24px;margin:20px 0;box-shadow:0 10px 30px rgba(35,48,75,.06)}}article{{padding:20px}}.section-head{{display:flex;justify-content:space-between;gap:20px;align-items:flex-start}}.section-head h2{{margin:0;font-size:22px}}.section-head>b{{color:var(--muted)}}.metrics{{display:grid;grid-template-columns:repeat(6,minmax(120px,1fr));gap:10px;margin:22px 0}}.metric{{padding:14px;border:1px solid var(--line);border-radius:12px;background:#fbfcfe}}.metric span{{display:block;color:var(--muted);font-size:12px}}.metric strong{{font-size:22px}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}}h3{{margin:0 0 12px}}table{{width:100%;border-collapse:collapse}}th,td{{padding:9px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}}th{{font-size:12px;color:var(--muted);text-transform:uppercase}}.bar{{display:inline-block;width:110px;height:8px;background:#edf1f6;border-radius:99px;overflow:hidden;margin-right:8px}}.bar i{{display:block;height:100%}}.bar .good{{background:var(--good)}}.bar .warn{{background:var(--warn)}}.bar .bad{{background:var(--bad)}}small,.note{{color:var(--muted)}}.failures{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;list-style:none;padding:0;margin:0}}.failures li{{display:flex;justify-content:space-between;padding:9px 12px;background:#f7f8fb;border-radius:9px}}code{{font-size:12px}}@media(max-width:900px){{.metrics{{grid-template-columns:repeat(2,1fr)}}.grid{{grid-template-columns:1fr}}.failures{{grid-template-columns:1fr}}}}
</style></head><body><main><header><p>PrefEval-inspired · deterministic workflow checks</p><h1>Workflow Preference Evaluation</h1></header>{comparison_section}{''.join(sections)}</main></body></html>"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inputs", nargs="+", required=True, help="result files or glob patterns"
    )
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--html", type=Path)
    args = parser.parse_args(argv)
    result_sets = load_result_sets(args.inputs)
    markdown = render_markdown(result_sets)
    print(markdown)
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(markdown, encoding="utf-8")
        print(f"wrote {args.markdown}")
    if args.html:
        args.html.parent.mkdir(parents=True, exist_ok=True)
        args.html.write_text(render_html(result_sets), encoding="utf-8")
        print(f"wrote {args.html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
