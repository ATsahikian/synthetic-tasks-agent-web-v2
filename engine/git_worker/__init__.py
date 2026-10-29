"""Shared Git worker, loaded as the engine.git_worker package."""

from .worker import GitWorker, uses_git_worker

__all__ = ["GitWorker", "uses_git_worker"]
