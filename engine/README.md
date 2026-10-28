# Python task event engine

The engine discovers task folders and accepts action commands through REST.
All action implementations live in `actions/`, with one handler class per action
file named `action_<action-name>.py`, using the registered action name exactly
(for example, `action_commit_impl.py` or `action_local_bench.py`).
Synchronous handlers run inline and record their result or error in one
completed event. Asynchronous handlers record a started event and launch a detached
worker, then record a completed event with the result or error.
The runner and worker communicate through durable job files:
input.json describes the action, output.log captures its output, cancel.json
requests cancellation, and result.json records completion. The runner reads
results and creates completion events; clients submit commands and cancellations.

## Run

Python 3.12 on Linux is required. There are no third-party Python dependencies.
Design commits and exports also require Git.

~~~bash
cd synthetic-tasks-agent-web-v2/engine
python3 server.py
~~~

The default listener is http://127.0.0.1:4181. The default task root is the sibling
project named synthetic-tasks-project-v2 next to the repository. Paths for these
defaults are resolved from the engine location, independently of the working directory.
An empty project is valid. Stop the server with Ctrl+C.

From the parent v2 directory, you can also run:

~~~bash
python3 -m engine
~~~

Options:

~~~bash
python3 server.py --tasks-root ../../synthetic-tasks-project-v2 --port 4182
python3 server.py --tasks-root /path/to/project
python3 server.py --actions-file actions/actions.json
~~~

Runtime state is stored in the task project, following v1's .review layout.
Each task owns its event history, state snapshot, and filesystem lock in its
.review folder. Task-set workspace metadata lives in the task set's .review
folder. Select another project with --tasks-root; storage follows that project.

## Task directory layout

Add your own task sets and tasks to the project folder. For example:

~~~text
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
~~~

A task set is a direct child containing template.md or instruction.md.
A task is its direct child containing design.md, task.toml, or instruction.md.
Hidden folders and symlinks are ignored during task discovery. The engine reads
directory metadata and writes runtime state only into .review folders. Task
documents and existing .review/jobs artifacts are preserved; existing status
files are not imported.
Discovery runs at startup, so restart after adding or removing task folders.

## Actions and event semantics

Edit `actions/actions.json` to define the available action names and dependencies.
The local workflow is redesign → commit_desing → impl → commit_impl →
local_bench → local_bench_analyze. export_to_codimango depends directly on
impl. custom and troubleshoot are independent actions.
commit_desing commits only the task's design.md in its existing Git repository
and records approval of the redesign. The task project must be inside a Git
repository with a configured author. Its result includes the commit hash;
unchanged designs reuse their existing commit. Unrelated staged files are preserved.
commit_impl records approval of an implementation with a passing oracle; the local
benchmark consumes that approved implementation result.
`actions/actions.demo.json` provides an executable demonstration graph
prepare → process → summarize.
Dependencies must refer to known actions and cannot contain cycles. Changes take
effect on restart and apply to newly accepted commands. Accepted commands retain their
original parameters and dependency definitions in their events.

Every new command creates a distinct action ID. Each action handler inherits from
`SyncActionHandler` or `AsyncActionHandler` in `actions/base.py` and implements
`run(config, execute, output_path)`. The registry in `actions/registry.py` resolves
the class named by each definition's `handler` field. The class and its file have
the same name, for example `action_commit_desing` in `actions/action_commit_desing.py`.
Each handler owns its runner settings, parameter validation, and execution preparation.
Adding an action class requires its file and a configuration reference.

~~~json
"commit_desing": {
  "dependencies": ["redesign"],
  "handler": "action_commit_desing",
  "mode": "sync"
}
~~~

| Mode | Actions | Execution events |
| --- | --- | --- |
| `sync` | commit_desing, commit_impl | completed, including the result or error |
| `async` | redesign, impl, local_bench, local_bench_analyze, export_to_codimango, custom, troubleshoot, configured commands | started, then completed, including the result or error |

`mode` is captured when the command is accepted. It defaults to the handler's mode
and must match that handler. `execution_type` continues to describe agent or script
execution independently of the lifecycle.
Action names use underscores between words. The implementation approval action
is `commit_impl`, implemented in `action_commit_impl.py`. Design approval uses
`commit_desing`, implemented in `action_commit_desing.py`; implementation consumes
that approved design and rejects later design or template changes.
The implementation action is `impl`, implemented in `action_impl.py`.
Action names and handler class names are independent. Older dependency names such as
commit-desing, implement, and commit-impl can still be used with explicit handler references.
Accepted commands capture the handler reference alongside its prepared settings.
Configurations and pending executions using the previous runner selector must be
migrated to explicit handler references before running this version.

Ready synchronous commands record exactly one completed event, and ready async
commands record exactly two events: started and completed. Their first event also
captures the command's parameters, dependency definitions, and execution settings.
Commands with unmet prerequisites record a waiting event with waiting_reason set
to dependencies. Once dependencies succeed, the server captures their action IDs
and runs the appropriate lifecycle. Queued sync actions therefore have waiting
and completed events; queued async actions have waiting, started, and completed.
Successful completion automatically runs newly ready queued commands.

- Clients cannot submit start or completion events. The server creates all
  execution events. Synchronous actions have no started event or worker files.
- Worker completion outcomes are succeeded, failed, cancelled, or timed_out.
- Failed prerequisites block downstream work. Submit another command to retry.
- Different action types can be active simultaneously. Only one waiting or
  started invocation of a given action type is allowed in each task.
- At initiation, the engine captures the exact prerequisite action IDs consumed.
  Running actions retain those references.
- New successful prerequisite executions make old dependent results stale,
  including transitive dependents. Staleness is derived from the captured
  dependency references; no separate invalidation rules are needed. Results
  remain in history. Submit explicit commands to replace them.
- A pending or failed prerequisite replacement blocks new consumers. Its prior
  successful result remains available in history.

Every action requires a `handler` reference in `actions/actions.json`. Handler settings
are optional and go in `runner`. Configured commands use `"handler": "action_command"`
and a `runner` object containing an argv array. The command runner supports cwd,
env, input, timeout_ms, termination_grace_ms, and success_result. Known placeholders
such as {task_path}, {job_path}, and {parameters.prompt} are expanded in command
values. Arguments are passed directly to the executable. Commands must exit with
status zero; success_result can additionally require fields from a
SYNTHETIC_RESULT={"approved":true} output marker.

If a worker cannot be launched, the server records a failed completion. Each
command runs in its own process group, with a default one-hour timeout and
ten-second termination grace period. Workers clean up remaining group members
after exit, cancellation, or timeout. The runner checks job results every 200 ms.

## Export to Codimango

export_to_codimango is a script action that depends only on impl. It
copies the task into the Cyber Bench checkout, commits its files, and pushes to
origin/main. Codimango picks up the pushed changes automatically.

Set the destination in `actions/actions.json`:

~~~json
"handler": "action_export_to_codimango",
"runner": {
  "repo_path": "/home/atsagikyan/AAI/cyber-bench-atsagikyan"
}
~~~

Relative paths are resolved from the actions file. The included path selects the
sibling cyber-bench-atsagikyan checkout. CODIMANGO_REPO_ROOT can override it when
starting the server. The resolved path is captured when a command is accepted.
The checkout must have main checked out and an origin remote with a main branch.

The destination folder uses the leaf name declared in task.toml's [task].name.
Copies omit .review, .remote, .git, __pycache__, design.md, design-status.json,
and symlinks. An existing task is replaced only when its destination files are
clean. Commits include only that task; unrelated staged files remain staged.
Cyber Bench's .claude/scripts/home-git-push helper is used when present. Other
checkouts use a direct push to origin/main and reject unrelated unpublished
commits. A failed push leaves the task commit local for an explicit retry.
An unchanged task already on origin/main succeeds without another push.

~~~bash
python3 -m engine.cli run set-one task-one export_to_codimango --wait
~~~

The CLI's auto-iterate workflow also submits an export command. Export can start
as soon as implementation succeeds with a passing oracle.

## REST API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | /health | Engine availability and discovered task counts |
| GET | /api/actions | Predefined actions |
| GET | /api/tasks | Task sets and discovered tasks |
| GET | /api/tasks/{set}/{task}/state | Derived task state |
| GET | /api/tasks/{set}/{task}/events?after=0 | Events after an exclusive sequence |
| POST | /api/tasks/{set}/{task}/commands | Accept a predefined action |
| POST | /api/tasks/{set}/{task}/cancellations | Request action cancellation |
| GET | /api/tasks/{set}/{task}/actions/{id}/output?after=0 | Read command output using a byte offset |

POST requests require Content-Type: application/json and a stable request_id.
IDs are scoped to a task and shared between its commands and cancellations. Reuse the
same ID when retrying the same request. A new logical request needs a new ID.
Identical retries return the original acknowledgment without writing an event;
reusing an ID with different content returns HTTP 409. The engine: prefix is
reserved for server-created events.

Command:

~~~json
{"request_id":"prepare-command","action":"prepare","parameters":{"input":"demo"}}
~~~

Cancellation:

~~~json
{
  "request_id": "prepare-cancel",
  "action_id": "UUID_FROM_COMMAND"
}
~~~

Waiting actions are cancelled immediately. For a started action, the runner
writes cancel.json and the worker stops its command before recording the outcome.
Clients cannot submit events, choose sequences, or report action results.
The /events endpoint is read-only; there is no /completions endpoint.
The server returns action_id, event_id, sequence, and type as its acknowledgment.
A ready synchronous command returns type completed and includes outcome, result,
and error. A ready async command returns type started. A queued command returns
type waiting, and retries retain that original acknowledgment after execution.
Retrieve state or event history for subsequent results.
The CLI prints a ready synchronous result once, including with --wait, and exits
with status 1 when that action fails. With --wait, async commands print their
start acknowledgment and their completion result.

Commands return HTTP 201; cancellations and reads return HTTP 200. Validation failures
return 400, missing tasks/actions 404, and transition or request-ID conflicts
409. Persistence failures return 503. Errors are JSON objects:

~~~json
{"error":{"code":"action_not_started","message":"Action has not been initiated by the server."}}
~~~

### Demonstration workflow

After adding set-one/task-one to the task root, start the server with
--actions-file engine/actions/actions.demo.json from the repository root. In another
terminal, queue the dependent commands before their prerequisites:

~~~bash
python3 -m engine.cli run set-one task-one summarize
python3 -m engine.cli run set-one task-one process
python3 -m engine.cli run set-one task-one prepare --wait
python3 -m engine.cli state set-one task-one
python3 -m engine.cli events set-one task-one
~~~

Workers complete the actions automatically in dependency order. State and event
reads show progress; no client completion request is needed.

## Persistence and restart recovery

Runtime files are distributed across the task project:

~~~text
synthetic-tasks-project-v2/
  set-one/
    .review/
      workspace.json
    task-one/
      .review/
        engine.lock
        events.jsonl
        job.json
        jobs/
          engine/
            {action_id}/
              input.json
              launch.json
              worker.lock
              worker.json
              process.json
              stdin.txt
              output.log
              cancel.json       # only when cancellation is requested
              result.json       # only after the worker finishes
    task-two/
      .review/
        engine.lock
        events.jsonl
        job.json
~~~

Each engine locks every task it opens before replaying or writing state. Another
engine cannot open the same tasks while those locks are held. Different task
projects have independent locks. Lock files stay in place after shutdown; the
operating system releases the locks when the process closes them or exits.

Each task set's workspace.json identifies its resolved task root and task-set
name. This is derived metadata and is refreshed when a project moves. Task
histories and snapshots travel with their task directories. The project
.gitignore excludes .review folders from version control, matching v1.

Each event is a complete JSON object on one line, followed by a newline.
The server owns event IDs and contiguous sequences. Task revision is the latest
sequence. Events carry schema_version, sequence, event_id, request_id,
request_fingerprint, action_id, timestamp, type, and payload.

Events are flushed with fsync before acknowledgment. State reduction has no job
execution or other external effects. job.json is an atomic, derived snapshot
containing actions, readiness, dependency references, outcomes, and revision.
Snapshot failure does not discard an already durable event.

To check restoration, stop the server, optionally delete only a task's job.json,
and run the same startup command again. State and request-ID deduplication are
rebuilt from events. Ready queued commands resume execution after replay. Started actions
reconnect to live workers or consume results saved while the server was offline.
If a worker is dead and has no saved result, the runner cleans up its remaining
processes and marks the action failed. Submit a new command to retry; started
actions are never automatically relaunched. Historical events remain replayable.
Old starts without worker records are marked failed during recovery.
Historical commands without mode or with `mode: durable` replay as `async`, keeping
their detached worker lifecycle even when their handler now uses sync for new
commands. Existing events retain their original contents. Ready sync results replay
directly from their completed event without running the handler again.
A malformed or missing snapshot does not affect recovery.
Tasks whose discovery markers are removed retain readable history at their
state/events URLs as long as their directories and .review files remain.
New commands require a discovered task; stored worker results are still reconciled
when a task's discovery markers have been removed.

Startup truncates an incomplete final line. Corruption within newline-terminated
history stops startup. Event logs must be modified only through the engine.
If an append has an uncertain persistence result, the engine rejects further
mutations until restart. Retry the original request ID after replay.

Workers are detached and continue running when the server stops. Completion
results are written atomically, and output is synced before result.json is saved.
The runner applies a completed result once and starts newly ready actions.
A worker lock prevents concurrent launches in the same job directory, and an
existing result prevents rerunning a completed job.

## Tests

From the parent v2 directory:

~~~bash
python3 -m unittest discover -s engine/tests -v
~~~

Tests cover synchronous results and errors, both action lifecycles, discovery,
REST requests, worker execution, cancellation, timeouts,
dependency transitions, parallel requests, deduplication, captured inputs, stale
results, persistence failures, and restart recovery, including server-owned
starts, launch ordering, task-local storage, project moves, and task lock ownership.
Fixtures use temporary projects outside the source tree and are removed on cleanup.
Temporary servers are stopped and their listening ports are checked for release.
