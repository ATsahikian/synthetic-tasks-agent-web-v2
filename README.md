# Synthetic Tasks Agent Web v2

Repository: https://github.com/ATsahikian/synthetic-tasks-agent-web-v2

This application provides a Python engine where commands execute ordered action
chains with durable events, a persistent action pool, a serialized Git worker,
detached workers, and restart recovery. Actions are function-like jobs with one parameter object. `one-shot` runs
through trajectory analysis; `one-action` executes a caller-supplied debugging call.

New agent actions start with Codex and recover automatically through Opus 4.8
with 200k context, then Muse. Each attempt and retry schedule is recorded in events;
delayed retries survive restarts and block later steps for the same task.

## Project layout

- `engine/`: Python engine, action definitions, API documentation, and tests.
- `engine/actions/`: Independent job handlers, parameter contracts, and internal execution settings.
- `engine/git_worker/`: Durable Git queue, scoped transactions, and export publishing.
- `engine/commands/`: Command definitions, handlers, dependency and parameter bindings, scheduling, and recovery.
- `web/`: Reserved for the future web interface.

## Run the engine

Python 3.12 on Linux is sufficient; no external dependencies are required.
Run from the repository root and point the engine at an existing task project:

```bash
python3 -m engine --tasks-root /path/to/synthetic-tasks-project-v2
```

The default listener is http://127.0.0.1:4181. Without `--tasks-root`, the
engine uses a sibling directory named `synthetic-tasks-project-v2`.
Runtime state is stored in that project's `.review` directories.

See [the engine documentation](engine/README.md) for task layout, action
semantics, API examples, and persistence details.

## Development checks

Create a Python 3.12 environment and install the pinned development tools:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
```

Run all checks from the repository root:

```bash
python -m mypy
python -m tools.check_annotations
python -m unittest discover -s engine/tests -v
```

Strict mypy covers the engine, tests, and checking tools. Every function parameter
(including `self`, `cls`, `*args`, and `**kwargs`) and return must be annotated.
Use `Self` for instance receivers and `type[Self]` for class receivers, with
postponed annotations and `TYPE_CHECKING` imports for circular references.
Shared JSON and structured dictionary contracts live in `engine/type_defs.py`.
Runtime validation remains required for external JSON and saved data.

GitHub Actions runs these checks on pushes and pull requests.
