# Synthetic Tasks Agent Web v2

Repository: https://github.com/ATsahikian/synthetic-tasks-agent-web-v2

This application provides a Python task event engine with a REST API, action
dependencies, detached workers, durable job files and event history, and restart
recovery. Clients submit commands and cancellations; workers save completion
results in result.json for the engine to consume.

## Project layout

- `engine/`: Python engine, action definitions, API documentation, and tests.
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

## Tests

```bash
python3 -m unittest discover -s engine/tests -v
```
