"""Commit and approve one task's redesigned design document."""

from pathlib import Path

from .action_export_to_codimango import lock, read_git
from .base import SyncActionHandler
from .common import design_sources, require_source


class action_commit_desing(SyncActionHandler):
    name = "commit_desing"
    parameters = frozenset({"source"})
    required_parameters = frozenset({"source"})

    def run(self, config, execute, output_path):
        task_path = Path(config["task_path"]).resolve()
        design_sources(task_path)
        approval = require_source(config)
        repository = read_git(task_path, "rev-parse", "--show-toplevel", allow_failure=True)
        if repository.returncode:
            raise RuntimeError("commit_desing requires the task to be inside a Git repository.")
        root = Path(repository.stdout.strip()).resolve()
        design = (task_path / "design.md").relative_to(root).as_posix()
        pathspec = f":(literal){design}"
        common = Path(read_git(root, "rev-parse", "--git-common-dir").stdout.strip())
        common = (root / common).resolve()

        with lock(common / "design-commit.lock"):
            require_source(config)
            read_git(root, "add", "--", pathspec)
            difference = read_git(
                root, "diff", "--cached", "--quiet", "--", pathspec, allow_failure=True
            )
            if difference.returncode not in (0, 1):
                raise RuntimeError(difference.stderr.strip() or "Cannot inspect staged design changes.")
            committed = difference.returncode == 1
            if committed:
                require_source(config)
                read_git(
                    root, "commit", "--only", "--no-gpg-sign", "-m",
                    f"Commit design for {task_path.parent.name}/{task_path.name}",
                    "--", pathspec,
                )
            commit = read_git(root, "log", "-1", "--format=%H", "HEAD", "--", pathspec).stdout.strip()
            if not commit:
                raise RuntimeError(f"No design commit exists for {design}.")
            require_source(config)
            return {
                **approval, "approved": True, "repo_path": str(root),
                "design_path": design, "commit": commit, "committed": committed,
            }
