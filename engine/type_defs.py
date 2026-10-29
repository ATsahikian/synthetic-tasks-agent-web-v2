"""Shared in-memory contracts; serialized keys retain their existing formats.

JSON input is untrusted until the boundary's runtime checks have succeeded.
Typed dictionaries describe those checked objects, including historical fields.
"""

from __future__ import annotations

from os import PathLike as OsPathLike
from typing import Literal, NotRequired, Required, TypedDict

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject = dict[str, JsonValue]
TaskKey = tuple[str, str]
PathLike = str | OsPathLike[str]
Outcome = Literal["succeeded", "failed", "cancelled", "timed_out"]
ErrorClassification = Literal["rate_limit", "cyber_safety", "unknown"]
FailureOrigin = Literal[
    "agent", "validation", "launch", "worker", "timed_out", "cancelled"
]


class FailureInfo(TypedDict):
    classification: ErrorClassification
    origin: FailureOrigin
    diagnostics: str
    exit_code: int | None


class RetryDecision(TypedDict):
    model: str
    attempt_number: int
    reason: Literal["rate_limit", "cyber_safety"]
    execute_at: str
    execution: ExecutionSpec


class CommandOptions(TypedDict, total=False):
    input: str
    env: dict[str, str]
    cwd: str
    timeout_ms: int | None
    termination_grace_ms: int | None
    literal: bool


class ExecutionCommand(CommandOptions):
    argv: list[str]


class ExecutionSpec(CommandOptions, total=False):
    argv: list[str]
    handler: str
    executor: str
    success_result: JsonObject
    agent: str
    agent_bin: str
    agent_binaries: dict[str, str]
    recovery: bool
    templates_path: str
    guide: str
    skill: str
    codimango_bin: str
    repo_path: str


class ActionDefinition(TypedDict, total=False):
    handler: Required[str]
    label: str
    description: str
    execution_type: str
    runner: ExecutionSpec
    parameters: list[str]
    required_parameters: list[str]
    accepts_arbitrary_parameters: bool


class Binding(TypedDict):
    step: str
    value: Literal["result", "action_id"]


class ChainStep(TypedDict):
    action: str
    dependencies: list[str]
    parameter_bindings: dict[str, Binding]


class CommandDefinition(TypedDict, total=False):
    handler: Required[str]
    label: str
    description: str
    actions: list[ChainStep]


class ActionStep(ChainStep):
    action_id: str
    parameters: JsonObject
    execution: ExecutionSpec
    execution_type: str
    mode: NotRequired[str]
    command_id: NotRequired[str]
    root_action_id: NotRequired[str]
    predecessor_action_id: NotRequired[str]
    attempt_number: NotRequired[int]
    execute_at: NotRequired[str]


class ActionState(TypedDict):
    action_id: str
    name: str
    parameters: JsonObject
    execution: ExecutionSpec | None
    execution_type: str | None
    status: str
    outcome: Outcome | None
    result: JsonValue
    error: str | None
    requested_at: str
    requested_sequence: int
    started_at: str | None
    completed_at: str | None
    completed_sequence: int | None
    cancel_requested: bool
    command_id: NotRequired[str]
    root_action_id: NotRequired[str]
    predecessor_action_id: NotRequired[str]
    attempt_number: NotRequired[int]
    execute_at: NotRequired[str]
    failure: NotRequired[FailureInfo | None]
    recovery_exhausted: NotRequired[bool]
    dependencies: NotRequired[list[str]]
    dependency_actions: NotRequired[dict[str, str]]
    stale: NotRequired[bool]
    stale_reasons: NotRequired[list[str]]
    blocked_by: NotRequired[list[str]]
    waiting_reason: NotRequired[str | None]
    task_set: NotRequired[str]
    task: NotRequired[str]


class CapturedAction(TypedDict):
    action_id: str
    name: str
    parameters: JsonObject
    task_set: str
    task: str
    dependency_actions: NotRequired[dict[str, str]]


class ActionConfig(TypedDict):
    schema_version: int
    action: CapturedAction
    execution: ExecutionSpec
    task_path: str
    job_path: str
    dependency_results: NotRequired[dict[str, JsonValue]]


class Completion(TypedDict):
    outcome: Outcome
    result: JsonValue
    error: str | None
    failure: NotRequired[FailureInfo | None]


class RequestBody(TypedDict, total=False):
    request_id: Required[str]
    action: str
    command: str
    parameters: JsonObject
    action_id: str
    command_id: str
    type: str
    outcome: Outcome
    result: JsonValue
    error: str | None
    failed_action_id: str | None
    failure: FailureInfo | None


class EventPayload(TypedDict, total=False):
    operation: str
    request: RequestBody
    command: str
    action: str
    parameters: JsonObject
    steps: list[ActionStep]
    dependencies: list[str]
    dependency_actions: dict[str, str]
    execution: ExecutionSpec | None
    execution_type: str | None
    mode: str
    outcome: Outcome
    result: JsonValue
    error: str | None
    failed_action_id: str | None
    failure: FailureInfo | None
    root_action_id: str
    predecessor_action_id: str
    model: str
    attempt_number: int
    reason: Literal["rate_limit", "cyber_safety"]
    execute_at: str
    resources: JsonValue
    invalidated_by: JsonValue


class Event(TypedDict):
    schema_version: int
    sequence: int
    event_id: str
    request_id: str
    request_fingerprint: str
    action_id: str | None
    timestamp: str
    type: str
    payload: EventPayload
    command_id: NotRequired[str]


class Receipt(TypedDict, total=False):
    task_set: Required[str]
    task: Required[str]
    event_id: Required[str]
    sequence: Required[int]
    type: Required[str]
    action_id: str | None
    command_id: str
    action_ids: list[str]
    outcome: Outcome
    result: JsonValue
    error: str | None
    failed_action_id: str | None


class CommandState(TypedDict):
    command_id: str
    name: str
    parameters: JsonObject
    steps: list[ActionStep]
    action_ids: list[str]
    schema_version: NotRequired[int]
    step_action_ids: NotRequired[dict[str, str]]
    status: str
    outcome: Outcome | None
    result: JsonValue
    error: str | None
    failed_action_id: str | None
    cancel_requested: bool
    started_at: str
    completed_at: str | None
    requested_sequence: int
    completed_sequence: int | None


class State(TypedDict):
    schema_version: int
    task_set: str
    task: str
    revision: int
    actions: dict[str, ActionState]
    current_actions: dict[str, str]
    latest_successful_actions: dict[str, str]
    ready_action_ids: list[str]
    commands: dict[str, CommandState]
    active_command_id: str | None
    action_pool: list[ActionStep]


class Task(TypedDict):
    name: str
    path: str
    markers: list[str]


class TaskSet(Task):
    tasks: list[Task]


class Catalog(TypedDict):
    tasks_root: str
    task_sets: list[TaskSet]


class ActionCatalog(TypedDict):
    schema_version: int
    actions: dict[str, ActionDefinition]


class CommandCatalog(TypedDict):
    schema_version: int
    commands: dict[str, CommandDefinition]


class EventsResponse(TypedDict):
    task_set: str
    task: str
    revision: int
    events: list[Event]


class OutputResponse(TypedDict):
    action_id: str
    after: int
    next: int
    output: str


class QueueEntry(TypedDict):
    sequence: int
    task_set: str
    task: str
    action_id: str
    phase: str
    recovering: NotRequired[bool]


class QueueState(TypedDict):
    schema_version: int
    next_sequence: int
    requests: list[QueueEntry]


class GitCheckpoint(TypedDict, total=False):
    paths: list[str]
    original_head: str
    source: dict[str, list[str] | None]
    staged: dict[str, list[str] | None]
    commit: str
    committed: bool


class GitJournal(GitCheckpoint, total=False):
    schema_version: int
    repo_path: str
    action_id: str
    phase: str


class ExportCheckpoint(TypedDict, total=False):
    source: dict[str, list[str | int]]
    previous: dict[str, list[str | int]]
    original_head: str
    had_destination: bool
    commit: str
    result: JsonObject


class ExportJournal(ExportCheckpoint, total=False):
    schema_version: int
    repo_path: str
    task_name: str
    action_id: str
    phase: str
