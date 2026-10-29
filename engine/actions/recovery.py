"""The fixed agent policy, invocation diagnostics, and deterministic retry timing."""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timedelta
from typing import Literal, cast

from engine.errors import EngineError
from engine.type_defs import (
    ActionState,
    ErrorClassification,
    FailureInfo,
    RetryDecision,
)

AGENTS = ("codex", "claude", "muse")
MODELS = {"codex": "codex", "claude": "claude-opus-4-8", "muse": "muse"}
MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 60
LiteralReason = Literal["rate_limit", "cyber_safety"]
CYBER_SAFETY = re.compile(
    r"content was flagged for possible cybersecurity risk|Trusted Access for Cyber program",
    re.IGNORECASE,
)
RATE_LIMIT = re.compile(
    r"rate[ -]?limit(?:ed)?\s+(?:reached|exceeded)|too many requests|"
    r"resource[_ ]exhausted|(?:http(?: error)?|status(?: code)?)\s*429\b",
    re.IGNORECASE,
)


def classify_error(diagnostics: str) -> ErrorClassification:
    # Match v1's precedence when a diagnostic contains both patterns.
    if CYBER_SAFETY.search(diagnostics):
        return "cyber_safety"
    if RATE_LIMIT.search(diagnostics):
        return "rate_limit"
    return "unknown"


def validate_failure(value: object) -> FailureInfo | None:
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"classification", "origin", "diagnostics", "exit_code"}
        or value["classification"] not in ("rate_limit", "cyber_safety", "unknown")
        or value["origin"]
        not in ("agent", "validation", "launch", "worker", "timed_out", "cancelled")
        or not isinstance(value["diagnostics"], str)
        or (value["exit_code"] is not None and type(value["exit_code"]) is not int)
    ):
        raise EngineError("Invalid structured failure diagnostics.")
    return cast(FailureInfo, value)


class AgentInvocationError(RuntimeError):
    def __init__(
        self: AgentInvocationError, message: str, diagnostics: str, code: int
    ) -> None:
        super().__init__(message)
        self.failure: FailureInfo = {
            "classification": classify_error(diagnostics),
            "origin": "agent",
            "diagnostics": diagnostics,
            "exit_code": code,
        }


def terminal_error(output: str) -> str | None:
    """Inspect the final CLI outcome; earlier internal retries can succeed.

    Claude reports terminal errors in result events even with exit status zero.
    Codex/Muse can emit error events or plain ERROR lines. Tool messages and echoed
    prompts are not CLI outcomes. Later success clears an earlier error event.
    """
    failure: str | None = None
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            # A normal final text line supersedes an intermediate retry diagnostic.
            failure = (
                line if re.match(r"^(?:\[?error\]?|fatal)\s*:", line, re.I) else None
            )
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "result":
            subtype = event.get("subtype", "")
            failed = event.get("is_error") is True or (
                isinstance(subtype, str) and subtype.startswith("error")
            )
            failure = line if failed else None
        elif kind in ("error", "turn.failed"):
            failure = line
        elif kind in ("turn.completed", "message", "assistant"):
            failure = None
    return failure


def retry_decision(action: ActionState) -> RetryDecision | None:
    """Use only captured execution and the durable completion timestamp."""
    spec, failure = action["execution"], action.get("failure")
    if (
        not spec
        or not spec.get("recovery")
        or action["outcome"] != "failed"
        or not failure
        or failure["origin"] != "agent"
        or failure["classification"] == "unknown"
    ):
        return None
    reason = failure["classification"]
    agent = spec["agent"]
    attempt = action.get("attempt_number", 1)
    delay = 0
    if reason == "rate_limit" and attempt < MAX_ATTEMPTS:
        attempt += 1
        delay = RETRY_DELAY_SECONDS
    else:
        index = AGENTS.index(agent) + 1
        if index == len(AGENTS):
            return None
        agent, attempt = AGENTS[index], 1
    execution = copy.deepcopy(spec)
    execution["agent"] = agent
    execution["agent_bin"] = spec["agent_binaries"][agent]
    assert action["completed_at"] is not None
    due = datetime.fromisoformat(action["completed_at"]) + timedelta(seconds=delay)
    return {
        "model": MODELS[agent],
        "attempt_number": attempt,
        "reason": reason,
        "execute_at": due.isoformat().replace("+00:00", "Z"),
        "execution": execution,
    }


def is_exhausted(action: ActionState) -> bool:
    spec, failure = action["execution"], action.get("failure")
    return bool(
        spec
        and spec.get("recovery")
        and spec.get("agent") == "muse"
        and action["outcome"] == "failed"
        and failure
        and failure["origin"] == "agent"
        and failure["classification"] != "unknown"
        and retry_decision(action) is None
    )
