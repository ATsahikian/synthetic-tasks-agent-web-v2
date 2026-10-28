# Python task event engine

This first milestone discovers task folders and accepts action commands through
REST. When dependencies are ready, the Python server stores a start event,
initiates the action, and waits for a completion signal. The server owns event
creation; clients submit commands and completion signals.

The demonstration actions use an initiation hook that logs the action without
launching a process. Send completion signals through REST to exercise dependency
handling. A job runner will plug into the server's initiation hook and send those
completion signals in a later milestone.

## Run

Python 3.12 on Linux is required. There are no third-party Python dependencies.
Exports also require Git.

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
python3 server.py --actions-file actions.json
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

Edit actions.json to define the available action names and their dependencies.
The local workflow is redesign → commit-desing → implement → commit-impl →
local_bench → local_bench_analyze. export_to_codimango depends directly on
implement. custom and troubleshoot are independent actions.
commit-impl records approval of an implementation with a passing oracle; the local
benchmark consumes that approved implementation result.
actions.demo.json provides the demonstration graph prepare → process → summarize.
Dependencies must refer to known actions and cannot contain cycles. Changes take
effect on restart and apply to newly accepted commands. Accepted commands retain their
original parameters and dependency definitions in their events.

Every new command creates a distinct action ID and records a waiting event.
Commands with unmet prerequisites remain waiting with waiting_reason set to
dependencies. When successful prerequisites are available, the server captures
their action IDs, durably records a started event, and invokes the initiation hook.
It then waits for a completion signal. Successful completion automatically starts
any queued commands whose dependencies are now satisfied.

- Clients cannot submit start events. Every action, including a short action,
  has a server-created start before completion.
- A completion signal is accepted only for a started action. Its outcome is
  succeeded, failed, cancelled, or timed_out.
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

For execution integration, pass initiate_action(task_set, task, action) to Engine.
The hook receives a copy of the started action, including its ID, parameters,
and captured prerequisite IDs. It must initiate work and return promptly;
completion is a separate signal. If initiation raises an exception, the server
records a failed completion. The default hook only logs initiation. Resource
locking for real parallel jobs belongs to the future execution layer.

## Export to Codimango

export_to_codimango is a script action that depends only on implement. It
copies the task into the Cyber Bench checkout, commits its files, and pushes to
origin/main. Codimango picks up the pushed changes automatically.

Set the destination in actions.json:

~~~json
"runner": {
  "builtin": "export_to_codimango",
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
| POST | /api/tasks/{set}/{task}/completions | Accept a completion signal |

POST requests require Content-Type: application/json and a stable request_id.
IDs are scoped to a task and shared between its commands and completions. Reuse the
same ID when retrying the same request. A new logical request needs a new ID.
Identical retries return the original acknowledgment without writing an event;
reusing an ID with different content returns HTTP 409. The engine: prefix is
reserved for server-created events.

Command:

~~~json
{"request_id":"prepare-command","action":"prepare","parameters":{"input":"demo"}}
~~~

Completion signal:

~~~json
{
  "request_id": "prepare-finish",
  "action_id": "UUID_FROM_COMMAND",
  "outcome": "succeeded",
  "result": {"value": 42}
}
~~~

For a failure, use outcome failed and an optional error string. Clients cannot
submit waiting or start events, choose sequences, or append arbitrary events.
The /events endpoint is read-only. Completion signals may optionally include
type completed; any other type is rejected.
The server returns action_id, event_id, sequence, and type as its acknowledgment.
Command acknowledgments identify the original waiting event, even when the
server has already started the action. Retrieve state separately to see its
current status and outcome.

Commands return HTTP 201; completions and reads return HTTP 200. Validation failures
return 400, missing tasks/actions 404, and transition or request-ID conflicts
409. Persistence failures return 503. Errors are JSON objects:

~~~json
{"error":{"code":"action_not_started","message":"Action has not been initiated by the server."}}
~~~

### Complete curl example

After adding set-one/task-one to the task root, run the following in another
terminal. This queues process before prepare. The server initiates prepare,
then automatically initiates process when prepare completes. Each action receives
only a completion signal from this client.

~~~bash
ENGINE_BASE=http://127.0.0.1:4181/api/tasks/set-one/task-one
send() {
  curl -fsS -H 'Content-Type: application/json' "$ENGINE_BASE/$1" --data "$2"
}
action_id() {
  python3 -c 'import json, sys; print(json.load(sys.stdin)["action_id"])'
}

PROCESS_ID=$(send commands '{"request_id":"demo-process","action":"process"}' | action_id)
curl -fsS "$ENGINE_BASE/state"

PREPARE_ID=$(send commands '{"request_id":"demo-prepare","action":"prepare"}' | action_id)
send completions '{"request_id":"demo-prepare-finish","action_id":"'"$PREPARE_ID"'","outcome":"succeeded"}'

send completions '{"request_id":"demo-process-finish","action_id":"'"$PROCESS_ID"'","outcome":"succeeded","result":{"processed":true}}'

SUMMARIZE_ID=$(send commands '{"request_id":"demo-summarize","action":"summarize"}' | action_id)
send completions '{"request_id":"demo-summarize-finish","action_id":"'"$SUMMARIZE_ID"'","outcome":"succeeded"}'

curl -fsS "$ENGINE_BASE/state"
curl -fsS "$ENGINE_BASE/events?after=2"
~~~

Repeat a request with its existing ID to verify deduplication. Repeating this
whole example also returns its original acknowledgments.

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
        jobs/                   # existing v1-style artifacts, when present
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
rebuilt from events. Ready commands resume initiation after replay; started
actions continue waiting for their completion signal and are not reinitiated.
Historical client start reports and direct completions remain replayable.
A malformed or missing snapshot does not affect recovery.
Tasks whose discovery markers are removed retain readable history at their
state/events URLs as long as their directories and .review files remain.
New commands and completions require a discovered task.

Startup truncates an incomplete final line. Corruption within newline-terminated
history stops startup. Event logs must be modified only through the engine.
If an append has an uncertain persistence result, the engine rejects further
mutations until restart. Retry the original request ID after replay.

This restores orchestration state. Task files, execution artifacts, process
launching, PIDs, process groups, action output logs, automatic retries,
and filesystem watching are future milestones.
If the server stops after persisting a start but before calling the initiation
hook, restart preserves the started state. A future runner must reconcile this
boundary with its execution records before attempting a launch again.

## Tests

From the parent v2 directory:

~~~bash
python3 -m unittest discover -s engine/tests -v
~~~

Tests cover discovery, REST requests, dependency transitions, parallel requests,
deduplication, captured inputs, stale results, persistence failures, and restart
recovery, including server-owned starts, initiation ordering, task-local storage,
project moves, and task lock ownership. Fixtures use temporary projects outside
the source tree and are removed on cleanup. Temporary servers are stopped and
their listening ports are checked for release.
