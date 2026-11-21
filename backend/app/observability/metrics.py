"""Tiny dependency-free metrics registry with Prometheus text exposition (counters + fixed-bucket histograms).

Process-local: with several workers each exposes its own numbers and the scraper aggregates, as usual for Prometheus.
Labels must be low-cardinality; HTTP metrics use the route *template* (e.g. /documents/{document_id}), never the raw path."""
from __future__ import annotations

import threading
import time
from collections import defaultdict

BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)


def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(names: tuple[str, ...], values: tuple[str, ...]) -> str:
    return "{" + ",".join(f'{n}="{_esc(v)}"' for n, v in zip(names, values)) + "}" if names else ""


class Registry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, tuple[str, tuple[str, ...], dict[tuple[str, ...], float]]] = {}
        self._gauges: dict[str, tuple[str, tuple[str, ...], dict[tuple[str, ...], float]]] = {}
        self._hists: dict[str, tuple[str, tuple[str, ...], dict[tuple[str, ...], list[float]]]] = {}
        self.started = time.time()

    def gauge(self, name: str, help_: str, labels: tuple[str, ...] = ()) -> None:
        self._gauges.setdefault(name, (help_, labels, {}))

    def set(self, name: str, value: float, *label_values: str) -> None:
        with self._lock:
            self._gauges[name][2][tuple(label_values)] = value

    def counter(self, name: str, help_: str, labels: tuple[str, ...] = ()) -> None:
        self._counters.setdefault(name, (help_, labels, defaultdict(float)))

    def histogram(self, name: str, help_: str, labels: tuple[str, ...] = ()) -> None:
        self._hists.setdefault(name, (help_, labels, {}))

    def inc(self, name: str, *label_values: str, by: float = 1.0) -> None:
        with self._lock:
            self._counters[name][2][tuple(label_values)] += by

    def observe(self, name: str, value: float, *label_values: str) -> None:
        with self._lock:
            series = self._hists[name][2].setdefault(tuple(label_values), [0.0] * (len(BUCKETS) + 2))  # buckets..., +Inf, sum
            for i, b in enumerate(BUCKETS):
                if value <= b:
                    series[i] += 1
            series[len(BUCKETS)] += 1           # +Inf == count
            series[len(BUCKETS) + 1] += value   # sum

    def render(self) -> str:
        out: list[str] = []
        with self._lock:
            for name, (help_, names, data) in self._counters.items():
                out += [f"# HELP {name} {help_}", f"# TYPE {name} counter"]
                out += [f"{name}{_labels(names, k)} {v:g}" for k, v in sorted(data.items())]
            for name, (help_, names, data) in self._hists.items():
                out += [f"# HELP {name} {help_}", f"# TYPE {name} histogram"]
                for k, s in sorted(data.items()):
                    for i, b in enumerate(BUCKETS):
                        out.append(f"{name}_bucket{_labels(names + ('le',), k + (f'{b:g}',))} {s[i]:g}")
                    out.append(f"{name}_bucket{_labels(names + ('le',), k + ('+Inf',))} {s[len(BUCKETS)]:g}")
                    out.append(f"{name}_sum{_labels(names, k)} {s[len(BUCKETS) + 1]:.6f}")
                    out.append(f"{name}_count{_labels(names, k)} {s[len(BUCKETS)]:g}")
            for name, (help_, names, data) in self._gauges.items():
                out += [f"# HELP {name} {help_}", f"# TYPE {name} gauge"]
                for k, v in sorted(data.items()):
                    out.append(f"{name}{_labels(names, k)} {v:g}")
            out += ["# HELP nexus_uptime_seconds Process uptime", "# TYPE nexus_uptime_seconds gauge",
                    f"nexus_uptime_seconds {time.time() - self.started:.1f}"]
        return "\n".join(out) + "\n"


def build_registry() -> Registry:
    r = Registry()
    r.counter("nexus_http_requests_total", "HTTP requests", ("method", "route", "status"))
    r.histogram("nexus_http_request_duration_seconds", "HTTP request latency", ("method", "route"))
    r.counter("nexus_agentic_tool_calls_total", "Agent tool invocations", ("tool", "status"))
    r.histogram("nexus_agentic_tool_duration_seconds", "Agent tool latency", ("tool",))
    r.counter("nexus_llm_calls_total", "LLM calls by route/task/status", ("model", "task", "status"))
    r.counter("nexus_llm_cost_usd_total", "Estimated LLM spend (USD)", ("model",))
    r.counter("nexus_llm_tokens_total", "LLM tokens", ("model", "direction"))
    r.counter("nexus_pii_findings_total", "PII/secret findings handled by policy", ("point", "kind", "action"))
    r.counter("nexus_policy_blocks_total", "Requests/documents refused by security policy", ("point", "reason"))
    r.gauge("nexus_ingestion_queue_depth", "Ingestion jobs by status (all tenants): the autoscaling signal for workers", ("status",))
    r.gauge("nexus_quality_regressions_active", "Quality regressions currently detected across orgs (monitor)")
    r.counter("nexus_agentic_runs_total", "Autonomous agent runs", ("status",))
    return r
