"""axentx_traces — minimal tracing for cross-daemon causality.

Replaces "where in pipeline are we?" mystery. Each pipeline_item carries
a `trace_id` that propagates through all stages. Each daemon writes a
span (start_ts, end_ts, daemon, status, error_or_output_summary) to
shared_kv["trace.<trace_id>.<step_n>"]. Enables full pipeline-run reconstruction.

Future: emit OTel via OpenLLMetry to Langfuse self-hosted. For now, this
gives us the shape — full tracing without Docker overhead.

Usage in daemon work_fn:
    from axentx_traces import start_span, end_span
    span = start_span(item, daemon="bd")
    # ... do work ...
    end_span(span, status="ok", output_summary=verdict.get("verdict"))
"""
from __future__ import annotations
import datetime
import json
import os
import socket
import time
import uuid

HOST = socket.gethostname()


def ensure_trace_id(item: dict) -> str:
    """Either return existing item.trace_id or mint a new one."""
    tid = item.get("trace_id")
    if tid:
        return tid
    tid = str(uuid.uuid4())[:18]
    item["trace_id"] = tid
    return tid


def start_span(item: dict, daemon: str) -> dict:
    """Start a span. Returns a span dict to pass to end_span()."""
    tid = ensure_trace_id(item)
    return {
        "trace_id": tid,
        "daemon": daemon,
        "host": HOST,
        "item_id": item.get("id"),
        "stage": item.get("stage"),
        "start_ts": time.time(),
        "started_at": datetime.datetime.utcnow().isoformat() + "Z",
    }


def end_span(span: dict, status: str = "ok",
             output_summary: str = "",
             error: str | None = None) -> None:
    span["end_ts"] = time.time()
    span["duration_ms"] = int((span["end_ts"] - span["start_ts"]) * 1000)
    span["ended_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    span["status"] = status
    span["output_summary"] = (output_summary or "")[:300]
    if error:
        span["error"] = error[:300]
    try:
        from axentx_shared import kv_set
        # Span key: trace.<trace_id>.<unix_ts>.<daemon>
        k = f"trace.{span['trace_id']}.{int(span['start_ts'])}.{span['daemon']}"
        kv_set(k, span)
    except Exception:
        pass


__all__ = ["ensure_trace_id", "start_span", "end_span"]
