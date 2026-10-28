from pathlib import Path

from .errors import EngineError


SET_MARKERS = ("template.md", "instruction.md")
TASK_MARKERS = ("design.md", "task.toml", "instruction.md")


def validate_component(value):
    if (
        not isinstance(value, str)
        or not value
        or value.startswith(".")
        or len(value) > 255
        or any(character in value for character in ("/", "\\", "\x00"))
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise EngineError("Invalid task-set or task name.")
    return value


def markers(directory, candidates):
    return [
        name for name in candidates
        if (directory / name).is_file() and not (directory / name).is_symlink()
    ]


def child_directories(directory):
    return sorted(
        (
            child for child in directory.iterdir()
            if not child.name.startswith(".")
            and not child.is_symlink()
            and child.is_dir()
        ),
        key=lambda child: child.name,
    )


def discover_tasks(root):
    root = Path(root).resolve()
    if not root.is_dir():
        raise EngineError(f"Task root must be an existing directory: {root}")
    task_sets = []
    keys = set()
    for set_directory in child_directories(root):
        set_markers = markers(set_directory, SET_MARKERS)
        if not set_markers:
            continue
        validate_component(set_directory.name)
        tasks = []
        for task_directory in child_directories(set_directory):
            task_markers = markers(task_directory, TASK_MARKERS)
            if not task_markers:
                continue
            validate_component(task_directory.name)
            keys.add((set_directory.name, task_directory.name))
            tasks.append({
                "name": task_directory.name,
                "path": str(task_directory),
                "markers": task_markers,
            })
        task_sets.append({
            "name": set_directory.name,
            "path": str(set_directory),
            "markers": set_markers,
            "tasks": tasks,
        })
    return {"tasks_root": str(root), "task_sets": task_sets}, keys
