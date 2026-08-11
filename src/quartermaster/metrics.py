"""Local aggregate product and runtime metrics."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from math import ceil
from typing import Any

from .db import SQLiteStore


_HISTOGRAM_BOUNDS = (1, 5, 10, 25, 50, 100, 250, 500, 1000, 2000, 5000)


def _bucket_start(timestamp: str | None = None) -> str:
    value = (
        datetime.now(timezone.utc)
        if timestamp is None
        else datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    )
    return (
        value.astimezone(timezone.utc)
        .replace(minute=0, second=0, microsecond=0)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _histogram_key(duration_ms: float) -> str:
    for bound in _HISTOGRAM_BOUNDS:
        if duration_ms <= bound:
            return str(bound)
    return "+Inf"


def _empty_histogram() -> dict[str, int]:
    return {str(bound): 0 for bound in _HISTOGRAM_BOUNDS} | {"+Inf": 0}


def record_metric(
    store: SQLiteStore,
    metric_name: str,
    duration_ms: float,
    *,
    dimension: str = "",
    at: str | None = None,
) -> None:
    """Record one bounded aggregate sample without actor or interaction identity."""
    if not metric_name.strip():
        raise ValueError("metric name must not be empty")
    if duration_ms < 0:
        raise ValueError("metric duration must not be negative")
    bucket = _bucket_start(at)
    key = _histogram_key(duration_ms)
    with store.transaction() as connection:
        row = connection.execute(
            """SELECT sample_count, total_ms, max_ms, histogram
                 FROM local_metric_buckets
                WHERE bucket_start = ? AND metric_name = ? AND dimension = ?""",
            (bucket, metric_name, dimension),
        ).fetchone()
        histogram = _empty_histogram()
        if row is not None:
            try:
                parsed = json.loads(row["histogram"])
                if isinstance(parsed, dict):
                    histogram.update({str(name): int(count) for name, count in parsed.items()})
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
            histogram = {name: max(0, histogram.get(name, 0)) for name in histogram}
            sample_count = int(row["sample_count"])
            total_ms = float(row["total_ms"])
            max_ms = float(row["max_ms"])
        else:
            sample_count = 0
            total_ms = 0.0
            max_ms = 0.0
        histogram.setdefault(key, 0)
        histogram[key] += 1
        connection.execute(
            """INSERT INTO local_metric_buckets(
                       bucket_start, metric_name, dimension, sample_count, total_ms, max_ms, histogram
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(bucket_start, metric_name, dimension) DO UPDATE SET
                       sample_count = excluded.sample_count,
                       total_ms = excluded.total_ms,
                       max_ms = excluded.max_ms,
                       histogram = excluded.histogram""",
            (
                bucket,
                metric_name,
                dimension,
                sample_count + 1,
                total_ms + duration_ms,
                max(max_ms, duration_ms),
                json.dumps(histogram, sort_keys=True, separators=(",", ":")),
            ),
        )


def _percentile(
    histogram: dict[str, int],
    sample_count: int,
    percentile: float,
    max_ms: float,
) -> float | None:
    if sample_count <= 0:
        return None
    target = max(1, ceil(sample_count * percentile))
    seen = 0
    for bound in _HISTOGRAM_BOUNDS:
        seen += histogram.get(str(bound), 0)
        if seen >= target:
            return float(bound)
    return max_ms


def metric_report(store: SQLiteStore, *, window_hours: int = 24) -> dict[str, Any]:
    """Return p50/p95/max aggregates for the requested recent local window."""
    if window_hours <= 0:
        raise ValueError("metric window must be positive")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    cutoff_text = cutoff.isoformat(timespec="seconds").replace("+00:00", "Z")
    with store.connection_lock:
        rows = store._require_connection().execute(
            """SELECT metric_name, dimension, sample_count, total_ms, max_ms, histogram
                 FROM local_metric_buckets
                WHERE bucket_start >= ?
                ORDER BY metric_name, dimension, bucket_start""",
            (cutoff_text,),
        ).fetchall()
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row["metric_name"]), str(row["dimension"]))
        aggregate = merged.setdefault(
            key,
            {"count": 0, "total_ms": 0.0, "max_ms": 0.0, "histogram": _empty_histogram()},
        )
        aggregate["count"] += int(row["sample_count"])
        aggregate["total_ms"] += float(row["total_ms"])
        aggregate["max_ms"] = max(aggregate["max_ms"], float(row["max_ms"]))
        try:
            histogram = json.loads(row["histogram"])
        except (TypeError, ValueError, json.JSONDecodeError):
            histogram = {}
        if isinstance(histogram, dict):
            for name in aggregate["histogram"]:
                try:
                    aggregate["histogram"][name] += max(0, int(histogram.get(name, 0)))
                except (TypeError, ValueError):
                    continue
    metrics: dict[str, dict[str, dict[str, Any]]] = {}
    for (metric_name, dimension), aggregate in merged.items():
        count = aggregate["count"]
        metrics.setdefault(metric_name, {})[dimension or "all"] = {
            "count": count,
            "mean_ms": round(aggregate["total_ms"] / count, 2) if count else None,
            "p50_ms": _percentile(aggregate["histogram"], count, 0.50, aggregate["max_ms"]),
            "p95_ms": _percentile(aggregate["histogram"], count, 0.95, aggregate["max_ms"]),
            "max_ms": round(aggregate["max_ms"], 2) if count else None,
        }
    return {"window_hours": window_hours, "since": cutoff_text, "metrics": metrics}


def render_metrics(report: dict[str, Any]) -> str:
    """Render local aggregates for an operator or DM/admin surface."""
    lines = [f"Local metrics (last {report['window_hours']} hours)"]
    metrics = report.get("metrics", {})
    if not metrics:
        return "\n".join(lines + ["No samples recorded."])
    for metric_name, dimensions in metrics.items():
        for dimension, values in dimensions.items():
            lines.append(
                f"- {metric_name} [{dimension}]: n={values['count']} "
                f"p50={values['p50_ms']}ms p95={values['p95_ms']}ms max={values['max_ms']}ms"
            )
    return "\n".join(lines)
