# V2 command and action engine

An **action** performs one job, like a function call with named parameters.
A **command** selects actions, owns their dependencies, constructs their parameters,
and executes them in sequence. Every new action execution belongs to a command.

## Run

Python 3.12 on Linux is required. There are no third-party Python dependencies.
Design commits and exports also require Git.

From the repository root:

```bash
python3 -m engine --tasks-root /path/to/synthetic-tasks-project-v2
```

Or run `python3 server.py` from `engine/`. The default listener is
http://127.0.0.1:4181. The default task root is the sibling
`synthetic-tasks-project-v2`, resolved from the engine location. An empty project
is valid. Stop the server with Ctrl+C.

Server options are `--tasks-root`, `--actions-file`, `--host`, and `--port`.
Command definitions are loaded from the bundled `commands/command.json`.
Configuration changes take effect on restart and affect newly accepted commands.
Accepted commands retain their original parameters, relationships, and prepared
execution settings.

## Task layout

```text
synthetic-tasks-project-v2/
  set-one/
    template.md
    instruction.md              # optional shared instructions
    task-one/
      design.md
      instruction.md            # optional
      task.toml                 # optional
    task-two/
      instruction.md
```

A task set is a direct child containing `template.md` or `instruction.md`.
A task is its direct child containing `design.md`, `task.toml`, or `instruction.md`.
Hidden directories and symlinks are ignored. Discovery runs at startup; restart
when adding or removing tasks. The engine preserves task documents and writes
runtime state into `.review` directories.

## Actions

`actions/actions.json` defines available jobs. Each definition names a handler,
its execution mode, and optional internal `runner` settings. Each handler class lives
in its matching file, such as `action_impl` in `actions/action_impl.py`, implements `run(config, execute, output_path)`,
and declares its accepted and required parameters. Action definitions have no
dependencies. Requests cannot override internal runner settings.

Every action receives one parameter object. Options and source data are all
parameters; there is no separate input object. Handlers validate their supplied
data and execute their job without looking up prerequisite actions or task history.
`GET /api/actions` lists the handler's accepted and required parameter names.

| Action | Parameters |
| --- | --- |
| `redesign` | Optional `agent`, `prompt` |
| `commit_desing` | Required `source`: design result with design/template hashes |
| `impl` | Required `source`: approved design result; optional `agent`, `prompt`, `reference_task` |
| `commit_impl` | Required `source`: implementation result with a passing oracle |
| `local_bench` | Required `source`: approved implementation result; optional `model`, `benchmark_agent`, `attempts`, `concurrency`, `timeout_multiplier`, `agent_timeout_multiplier` |
| `local_bench_analyze` | Required `source`: completed benchmark result containing saved trials/trajectories; required `source_id`: benchmark action ID; optional `agent`, `prompt` |
| `export_to_codimango` | Required `source`: implementation result with a passing oracle and design/template hashes |
| `custom` | Required `prompt`; optional `agent` |
| `troubleshoot` | Required `evidence_path`; optional `agent`, `prompt` |

`source` values use the result objects produced by the corresponding jobs.
Data guards still check hashes, approvals, oracle outcomes, and evidence files.
The design approval action retains its existing spelling, `commit_desing`.

| Mode | Execution |
| --- | --- |
| `sync` | Runs inline and emits one `completed` event with its result/error |
| `async` | Emits `started`, executes outside the engine lock, then emits `completed` |

All built-in actions are asynchronous. `commit_desing`, `commit_impl`, and
`export_to_codimango` use one dedicated `git-worker` thread and a shared durable FIFO
queue; other actions use detached workers. Handler mode and configured mode must match. Historical actions
without a mode, or with `mode: durable`, retain their asynchronous lifecycle.

The `action_command` handler executes configured argv arrays. Its internal runner
supports `cwd`, `env`, `input`, `timeout_ms`, `termination_grace_ms`, and
`success_result`. Known placeholders such as `{task_path}`, `{job_path}`, and
`{parameters.prompt}` are expanded. Arguments are passed directly to the executable.
A nonzero exit or missing required `SYNTHETIC_RESULT={...}` fields fails the job.
This handler accepts arbitrary parameter names for its configured placeholders.

## Commands

All command code lives in `commands/`: definitions, handlers, registry, scheduling,
state transitions, and historical recovery. Shared engine storage, transport, and
worker launch code remain in their existing modules.

### one-shot

The local workflow stops after trajectory analysis:

```text
redesign → commit_desing → impl → commit_impl → local_bench → local_bench_analyze
```

`command.json` stores the ordered steps, their dependencies, and parameter bindings.
A binding selects a dependency's `result` or `action_id` and supplies it as an action
parameter. For example:

```json
{
  "action": "impl",
  "dependencies": ["commit_desing"],
  "parameter_bindings": {
    "source": {"step": "commit_desing", "value": "result"}
  }
}
```

Dependencies must name earlier steps. Empty chains, repeated actions, cycles,
and invalid bindings are rejected. Every selected action and its caller options
are validated before command acceptance. Required bound source data is checked
by the receiving action when that producer's result becomes available.

Shared command parameters are forwarded to handlers that accept them. A command's
`action_parameters` object provides overrides keyed by action name. Command-defined
bindings take precedence over caller values.

```json
{
  "request_id": "one-shot-example",
  "command": "one-shot",
  "parameters": {
    "agent": "codex",
    "prompt": "Revise this task",
    "action_parameters": {
      "local_bench": {"attempts": 2, "concurrency": 2}
    }
  }
}
```

### one-action

This generic debugging command executes exactly one selected action. The caller
supplies a valid function call through `action_parameters`, including any required
source data or artifact references. It does not resolve saved results, infer
dependencies, or execute prerequisites.

```json
{
  "request_id": "debug-custom",
  "command": "one-action",
  "parameters": {
    "action": "custom",
    "action_parameters": {"prompt": "Inspect this task", "agent": "codex"}
  }
}
```

`action_parameters` defaults to `{}`. Unknown parameters and missing required
parameters are rejected according to the selected handler's contract. Internal
handler and runner settings come from `actions.json`.

### Scheduling and failure handling

One active command owns each task. A second command receives HTTP 409, including
a debugging call. Different tasks can execute concurrently. Historical pending
or running actions also block new commands on their task until they finish or
are cancelled.

The engine durably records `command_started` before execution, including the full
frozen chain and each action ID. It starts the first action and stores the remaining
calls in `.review/action-pool.json` as an ordered JSON list. Queued actions have no
separate waiting events. Only the head can execute, and advancement waits for a
successful result. Synchronous steps retain their completed-only events.

After the last action succeeds, the engine emits `command_completed` with outcome
`succeeded` and the action results. On failure or timeout, it emits `cancelled`
for each remaining queued action, retaining the original error and failing action
ID, empties the pool, and completes the command as `failed`. A cancellation stops
the active worker and cancels the remaining chain. Command completion waits for
the running worker's result; a late successful result never starts another step
after cancellation.

## REST API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Availability and discovered task counts |
| GET | `/api/actions` | Available jobs and parameter contracts |
| GET | `/api/commands` | Predefined commands |
| GET | `/api/tasks` | Task sets and tasks |
| GET | `/api/tasks/{set}/{task}/state` | Command/action history and pending pool |
| GET | `/api/tasks/{set}/{task}/events?after=0` | Events after an exclusive sequence |
| POST | `/api/tasks/{set}/{task}/commands` | Submit `one-shot` or `one-action` |
| POST | `/api/tasks/{set}/{task}/cancellations` | Cancel a command |
| GET | `/api/tasks/{set}/{task}/actions/{id}/output?after=0` | Read action output by byte offset |

POST requests require `Content-Type: application/json` and a stable `request_id`.
IDs are task-scoped and shared between submissions and cancellations. Identical
retries return their original acknowledgment without adding events, even after
completion or restart. Reusing an ID with different content returns HTTP 409.
The `engine:` prefix is reserved for server-generated events.

Submission returns HTTP 201 with `type: command_started`, `command_id`, `action_ids`,
`event_id`, and `sequence`. A single-step command also includes `action_id`.
The acknowledgment records acceptance; retrieve state/events for the eventual
outcome. State includes `commands`, `active_command_id`, `actions`, and `action_pool`.
Commands expose their frozen steps, outcome, result, error, and failing action ID.

Cancellation:

```json
{"request_id":"cancel-example","command_id":"COMMAND_UUID"}
```

Legacy requests of the form `{"request_id":"...","action":"...","parameters":{...}}`
are wrappers around `one-action`; they do not resolve historical results.
Legacy `action_id` cancellations cancel that action's owning command, including
when the referenced action is still queued. Previously stored standalone actions
remain cancellable during recovery.

Reads and cancellations return HTTP 200. Validation errors return 400, missing
resources 404, conflicts 409, and persistence failures 503. Errors have the shape:

```json
{"error":{"code":"command_already_active","message":"A command or historical action already owns this task."}}
```

Events and execution results are server-owned. There is no action submission
endpoint or client completion endpoint.

## CLI

The CLI talks to an already running server:

```bash
python3 -m engine.cli commands
python3 -m engine.cli actions
python3 -m engine.cli command set-one task-one one-shot --wait
python3 -m engine.cli command set-one task-one one-action \
  --parameters '{"action":"custom","action_parameters":{"prompt":"Inspect this task"}}' --wait
python3 -m engine.cli run set-one task-one custom --prompt 'Inspect this task' --wait
python3 -m engine.cli cancel-command set-one task-one COMMAND_UUID
python3 -m engine.cli state set-one task-one
python3 -m engine.cli events set-one task-one --after 0
python3 -m engine.cli output set-one task-one ACTION_UUID --after 0
```

`run` forwards its parameters through `one-action`. `cancel` accepts an action ID
and cancels its owning command. `--request-id` supplies a stable request ID.
`--wait` prints the acceptance acknowledgment and final command result; it exits
with status 0 only for success. Without `--wait`, status 0 means accepted.
Ctrl+C leaves the command running and exits the client with status 130.

For a lightweight demonstration, start with `--actions-file actions/actions.demo.json`
and use `one-action` or `run` to execute `prepare`, `process`, and `summarize`.
These jobs are independent; the default `one-shot` requires the built-in workflow
jobs from the regular action catalog.

## Export to Codimango

`one-shot` ends at analysis. To export separately, call `one-action` with
`action: export_to_codimango` and pass the implementation result as its `source`
parameter. The action copies the task into Cyber Bench, commits its files, and
pushes to `origin/main`.

Configure its internal `runner.repo_path` in `actions.json`;
`CODIMANGO_REPO_ROOT` can override it when starting the server. Relative configured
paths are resolved from the actions file. The destination must have `main` checked
out and an `origin` remote with a main branch.

The destination uses the leaf name from `task.toml`'s `[task].name`. Copies omit
`.review`, `.remote`, `.git`, `__pycache__`, `design.md`, `design-status.json`, and
symlinks. Existing destination files must be clean. Commits include only the task,
preserving unrelated staged work. Cyber Bench's `.claude/scripts/home-git-push`
helper is used when present; otherwise a direct push rejects unrelated unpublished
commits. A failed push leaves the local task commit for an explicit retry. An
unchanged task already on remote main succeeds without another push.

## Persistence and restart recovery

```text
set-one/
  .review/workspace.json
  task-one/
    .review/
      engine.lock
      events.jsonl
      action-pool.json
      job.json
      jobs/engine/{action_id}/
        input.json
        launch.json
        worker.lock
        worker.json
        process.json
        stdin.txt
        output.log
        cancel.json             # after a cancellation request
        result.json             # after worker completion
```

Each task has an exclusive filesystem lock held by its engine. Another engine
cannot open the same tasks. Locks are released by the operating system at exit;
lock files remain. Task-set workspace metadata records the resolved project
location and is refreshed when the project moves.

Events are newline-delimited JSON with contiguous sequences, server-owned IDs,
and timestamps. New schema v2 events include `command_id`; command lifecycle
events have `action_id: null`. Historical schema v1 events remain replayable.
Events are flushed with `fsync` before acknowledgment. An uncertain append makes
the engine reject further mutations until restart; retry the original request ID.
An incomplete final line is truncated during recovery. Corruption within committed
history stops startup.

`job.json` and `action-pool.json` are atomic derived projections. Missing, malformed,
or outdated projections are rebuilt from events. A projection write failure does
not discard a durable event. The pool is engine-owned; editing it does not submit
or reorder work. Command and result provenance are reconstructed from their captured
relationships, allowing older dependent results to become stale after a newer
successful producer execution. `one-action` never uses that history to build a call.

Restart resumes pending commands, completes interrupted failure cleanup, and writes
missing command completion events. Live detached workers are reconnected; saved
results are applied once. A dead detached worker without a result is marked failed
and its remaining processes are cleaned up. Those actions are not automatically
relaunched. Historical synchronous completed-only events remain replayable.

### Git worker

The implementation lives in `git-worker/`: `worker.py` owns the queue,
`transactions.py` handles scoped commits, and `export.py` handles publishing.
The folder's `__init__.py` exposes the package API. The engine initializer registers
the folder as `engine.git_worker` for Python imports.

The engine stores the global queue in `<tasks-root>/.review/git-queue.json` and
holds `<tasks-root>/.review/git-worker.lock` while running. Queue entries identify
the task, action ID, FIFO sequence, and execution phase. `execution.executor` is
captured as `git-worker` when preparing a Git action; callers cannot override it.
The existing per-task action pool still represents later command steps. An action
waiting for the Git worker has already emitted `started`.

Only one Git request executes at a time, including an export's fetch and push.
Git execution does not hold the engine lock, so API reads and other actions remain
responsive. A repository lock in its Git common directory also coordinates separate
engine processes that target the same checkout. Independent Git tools must cooperate
with that lock. Agent invocations are instructed to leave Git mutations to the engine.

`commit_desing` commits only `design.md`. `commit_impl` commits added, modified, and
deleted files within the selected task directory, respecting Git ignore rules for
untracked files and excluding `.review`, `.remote`, `.git`, and `__pycache__`.
It requires a passing oracle and unchanged design/template hashes. Its result retains
the approval/source fields and adds `repo_path`, `task_path`, `commit`, and `committed`.
Unchanged content reuses the latest relevant commit with `committed: false`.
Commits use explicit paths and preserve unrelated staged files.

Each Git job retains `input.json`, `output.log`, `result.json`, and a
`git-transaction.json` journal. Export additionally retains `export-transaction.json`,
`export-snapshot/`, `export-previous/`, and `export-commit/git-transaction.json`.
Journals record the intended content, staging state, commit SHA, and export/push
progress. Newly created commits include a `Synthetic-Action-ID` trailer. Restart
restores FIFO order, reconstructs interrupted enqueue records, resumes unexecuted
requests, and reconciles existing commits or remote content before retrying. Saved
results produce exactly one completion event. Ambiguous repository changes fail the
request with a recovery explanation instead of creating another commit.

Cancellation skips waiting Git requests; an active request finishes and reports its
actual result, then remaining command steps are cancelled. Normal shutdown stops
admission and further action dispatch, drains accepted Git requests, publishes their
completions, and joins the worker before releasing engine locks. Later command steps
resume on restart. Drain time includes accepted exports and their network operations.
For the initial upgrade, finish old active/queued commands before replacing the engine.

Detached workers remain running when the server stops. Their output is synced before
an atomic `result.json` is saved. Worker locks prevent duplicate execution within a
job directory. The engine checks results every 200 ms. Detached-worker cancellation
and timeout stop owned process groups, with a default one-hour timeout and ten-second
termination grace period. Active Git requests ignore cancellation; Git commands retain
their bounded timeouts and interrupted transactions are reconciled on restart.

Stored histories remain readable when a task loses its discovery markers. Existing
worker results are still reconciled, and remaining queued command work is cancelled
when the task is no longer discovered.

## Tests

From the repository root:

```bash
python3 -m unittest discover -s engine/tests -v
```

Tests cover current command calls, parameter bindings, both action lifecycles,
CLI and REST behavior, queue projection, original-error propagation, cancellation,
timeout, task ownership, concurrent retries, crash boundaries, and worker recovery.
Historical fixtures explicitly generate schema v1 events to retain coverage of
old logs, dependencies, approvals, and saved worker configurations. Temporary task
projects and Git remotes are outside the source tree and cleaned up after each test.
