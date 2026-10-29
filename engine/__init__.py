"""A small, task-scoped event engine."""

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


# Register the hyphenated source folder under its Python import name.
_git_worker_spec = spec_from_file_location(
    f"{__name__}.git_worker", Path(__file__).with_name("git-worker") / "__init__.py"
)
git_worker = module_from_spec(_git_worker_spec)
sys.modules[_git_worker_spec.name] = git_worker
_git_worker_spec.loader.exec_module(git_worker)

from .core import Engine

__all__ = ["Engine"]
