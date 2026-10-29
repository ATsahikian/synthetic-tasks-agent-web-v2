"""Copy one task to a shared Cyber Bench checkout and publish its commit."""

import fcntl
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from contextlib import contextmanager
from pathlib import Path


from .base import AsyncActionHandler
from .common import digest, read_document
from ..errors import EngineError


OMITTED = {".git", ".review", ".remote", "__pycache__", "design.md", "design-status.json"}


def excluded(directory, names):
    return [name for name in names if name in OMITTED or (Path(directory) / name).is_symlink()]


def payload(directory):
    """Compare copied bytes and executable permissions against their source."""
    entries = {}
    for parent, directories, files in os.walk(directory, followlinks=False):
        directories[:] = [name for name in directories if name not in excluded(parent, directories)]
        for name in files:
            if name in excluded(parent, [name]):
                continue
            file = Path(parent) / name
            if not file.is_file():
                raise RuntimeError(f"Export source is not a regular file: {file}")
            entries[str(file.relative_to(directory))] = (
                hashlib.sha256(file.read_bytes()).hexdigest(), file.stat().st_mode & 0o111
            )
    return entries


@contextmanager
def lock(file):
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.open("a+b") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(f"Another Cyber Bench operation owns {file.name}; retry later.") from error
        yield


def read_git(root, *arguments, allow_failure=False):
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode and not allow_failure:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Git check failed.")
    return result


def commit_paths(root, commit):
    output = read_git(root, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-z", commit).stdout
    return [path for path in output.split("\x00") if path]


def export_task(source, root, execute):
    source, root = source.resolve(), root.resolve()
    if not root.is_dir() or Path(read_git(root, "rev-parse", "--show-toplevel").stdout.strip()) != root:
        raise RuntimeError("runner.repo_path must identify an existing Git repository root.")
    if source.is_relative_to(root) or root.is_relative_to(source):
        raise RuntimeError("The source task and destination repository must be separate directories.")
    metadata = source / "task.toml"
    if metadata.is_symlink():
        raise RuntimeError("Export requires a regular task.toml.")
    with metadata.open("rb") as stream:
        declared_name = tomllib.load(stream).get("task", {}).get("name")
    name = declared_name.rsplit("/", 1)[-1] if isinstance(declared_name, str) else ""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}", name):
        raise RuntimeError("task.toml must declare a safe [task].name for the destination folder.")
    common = Path(read_git(root, "rev-parse", "--git-common-dir").stdout.strip())
    common = (root / common).resolve()
    destination = root / name
    helper = root / ".claude/scripts/home-git-push"
    has_helper = helper.is_file()

    def run(arguments):
        code = execute({
            "argv": arguments, "cwd": str(root), "literal": True,
            "env": {"GIT_TERMINAL_PROMPT": "0"}, "timeout_ms": 300000,
        })
        if code != 0:
            raise RuntimeError(f"Export command {arguments[0]} failed with exit status {code}; see action output.")

    def git(*arguments):
        run(["git", *arguments])

    with lock(common / "codimango-export.lock"), lock(common / "cyber-bench-task-locks" / f"{name}.lock"):
        with lock(common / "cyber-bench-commit.lock"):
            if read_git(root, "branch", "--show-current").stdout.strip() != "main":
                raise RuntimeError("Cyber Bench must have main checked out before export.")
            if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
                raise RuntimeError(f"Export destination must be a regular task directory: {destination}")
            if read_git(root, "status", "--porcelain", "--untracked-files=all", "--", name).stdout:
                raise RuntimeError(f"Cyber Bench has uncommitted changes in {name}; export stopped.")
            hooks = read_git(root, "config", "--get", "core.hooksPath", allow_failure=True).stdout.strip()
            if hooks == ".githooks" and not has_helper:
                raise RuntimeError("This Cyber Bench checkout requires .claude/scripts/home-git-push.")
            git("fetch", "--quiet", "origin", "main")
            remote = read_git(root, "rev-parse", "refs/remotes/origin/main").stdout.strip()
            if not has_helper:
                read_git(root, "merge-base", "--is-ancestor", remote, "HEAD")
                for commit in read_git(root, "rev-list", f"{remote}..HEAD").stdout.splitlines():
                    paths = commit_paths(root, commit)
                    if not paths or any(not path.startswith(name + "/") for path in paths):
                        raise RuntimeError("Unpublished commits outside this task require the Cyber Bench push helper.")
            starting_head = read_git(root, "rev-parse", "HEAD").stdout.strip()
            committed = False
            with tempfile.TemporaryDirectory(prefix=".codimango-stage-", dir=root) as temporary:
                stage, backup = Path(temporary) / "task", Path(temporary) / "previous"
                snapshot = payload(source)
                shutil.copytree(source, stage, ignore=excluded)
                if snapshot != payload(stage) or snapshot != payload(source):
                    raise RuntimeError("Task files changed while copying the export; retry the local workflow.")
                had_destination = destination.exists()
                if had_destination:
                    os.replace(destination, backup)
                try:
                    os.replace(stage, destination)
                    git("add", "--", name)
                    difference = read_git(root, "diff", "--cached", "--quiet", "--", name, allow_failure=True)
                    if difference.returncode not in (0, 1):
                        raise RuntimeError(difference.stderr.strip() or "Cannot inspect staged task changes.")
                    if difference.returncode == 1:
                        tag = re.match(r"cyber-bench-([a-z]+)-.*?(\d+(?:\.\d+)?)(?:-|$)", name, re.I)
                        subject = f"[{tag[1].upper()} {tag[2]}]" if tag else "[codimango]"
                        git("commit", "--only", "--no-gpg-sign", "-m", f"{subject} Export {name}", "--", name)
                        committed = True
                        commit = read_git(root, "rev-parse", "HEAD").stdout.strip()
                        paths = commit_paths(root, commit)
                        if not paths or any(not path.startswith(name + "/") for path in paths):
                            raise RuntimeError(f"Export commit {commit} contains paths outside {name}.")
                except BaseException:
                    if not committed and read_git(root, "rev-parse", "HEAD").stdout.strip() == starting_head:
                        try:
                            read_git(root, "restore", "--staged", "--source=HEAD", "--", name)
                        finally:
                            if destination.exists():
                                shutil.rmtree(destination)
                            if had_destination:
                                os.replace(backup, destination)
                    raise
            commit = read_git(root, "log", "-1", "--format=%H", "HEAD", "--", name).stdout.strip()
            if not commit:
                raise RuntimeError(f"No task commit was created for {name}.")
            already_exported = (
                bool(read_git(root, "ls-tree", remote, "--", name).stdout)
                and read_git(root, "diff", "--quiet", remote, "HEAD", "--", name, allow_failure=True).returncode == 0
            )
            if not has_helper and not already_exported:
                try:
                    git("push", "origin", f"{commit}:refs/heads/main")
                except Exception as error:
                    raise RuntimeError(f"Export commit {commit} was saved locally. {error}") from error
        if has_helper and not already_exported:
            try:
                run([str(helper), "task", name])
            except Exception as error:
                raise RuntimeError(f"Export commit {commit} was saved locally. {error}") from error
        git("fetch", "--quiet", "origin", "main")
        pushed = read_git(root, "rev-parse", "refs/remotes/origin/main").stdout.strip()
        read_git(root, "diff", "--quiet", pushed, "HEAD", "--", name)
        task_commit = read_git(root, "log", "-1", "--format=%H", pushed, "--", name).stdout.strip()
        if not task_commit:
            raise RuntimeError("The remote main branch does not contain the exported task.")
        return {
            "repo_path": str(root), "task_name": name, "destination": str(destination),
            "commit": task_commit, "push_commit": pushed, "branch": "main",
            "pushed": not already_exported, "already_exported": already_exported,
        }


class action_export_to_codimango(AsyncActionHandler):
    name = "export_to_codimango"
    parameters = frozenset({"source"})
    required_parameters = frozenset({"source"})
    runner_fields = frozenset({"repo_path"})

    def configure_runner(self, runner, directory):
        runner = super().configure_runner(runner, directory)
        repo_path = runner.get("repo_path")
        if not isinstance(repo_path, str) or not repo_path.strip() or "\x00" in repo_path:
            raise EngineError("export_to_codimango runner.repo_path must be nonempty text.")
        runner["repo_path"] = str((directory / Path(repo_path).expanduser()).resolve())
        return runner

    def prepare(self, runner, parameters):
        spec = super().prepare(runner, parameters)
        spec["repo_path"] = str(Path(os.environ.get("CODIMANGO_REPO_ROOT") or spec["repo_path"]).expanduser().resolve())
        return spec

    def run(self, config, execute, output_path):
        spec = config["execution"]
        task_path = Path(config["task_path"])
        design_path, template_path = task_path / "design.md", task_path.parent / "template.md"
        inputs = config["action"]["parameters"]["source"]
        if inputs.get("oracle_passed") is not True:
            raise RuntimeError("A verified passing implementation oracle is required before exporting.")
        if inputs.get("design_sha256") != digest(design_path) or inputs.get(
            "template_sha256"
        ) != digest(template_path):
            raise RuntimeError("Task inputs changed; rerun the prerequisite before exporting.")
        return export_task(task_path, Path(spec["repo_path"]), execute)
