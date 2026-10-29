"""Copy one task to a shared Cyber Bench checkout and publish its commit."""

import hashlib
import os
import re
import shutil
import tempfile
import tomllib
import json
import uuid
from pathlib import Path


from .transactions import Transaction, common_directory, lock, read_git
from ..storage import atomic_json_write, sync_directory


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


def commit_paths(root, commit):
    output = read_git(root, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-z", commit).stdout
    return [path for path in output.split("\x00") if path]


def export_task(source, root, execute, *, job_path=None, action_id=None):
    if job_path is None:
        with tempfile.TemporaryDirectory(prefix="codimango-export-") as temporary:
            return export_task(source, root, execute, job_path=Path(temporary), action_id=action_id)
    source, root, job = source.resolve(), root.resolve(), Path(job_path)
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
    common = common_directory(root)
    destination = root / name
    helper = root / ".claude/scripts/home-git-push"
    has_helper = helper.is_file()
    job.mkdir(parents=True, exist_ok=True)
    journal = job / "export-transaction.json"
    if journal.is_symlink():
        raise RuntimeError("Export journal must not be a symlink.")
    state = json.loads(journal.read_text()) if journal.exists() else {}
    if state and (state.get("repo_path") != str(root) or state.get("task_name") != name):
        raise RuntimeError("Export destination changed during recovery.")
    action_id = action_id or state.get("action_id") or str(uuid.uuid4())
    if state and state.get("action_id") != action_id:
        raise RuntimeError("Export action identity changed during recovery.")

    def checkpoint(phase, **values):
        state.update({"schema_version": 1, "repo_path": str(root), "task_name": name,
                      "action_id": action_id, "phase": phase, **values})
        atomic_json_write(journal, state)

    def run(arguments):
        code = execute({
            "argv": arguments, "cwd": str(root), "literal": True,
            "env": {"GIT_TERMINAL_PROMPT": "0"}, "timeout_ms": 300000,
        })
        if code != 0:
            raise RuntimeError(f"Export command {arguments[0]} failed with exit status {code}; see action output.")

    def git(*arguments):
        run(["git", *arguments])

    def normalized_payload(path):
        return {key: list(value) for key, value in payload(path).items()}

    snapshot, backup = job / "export-snapshot", job / "export-previous"
    installation = root / f".codimango-stage-{action_id}"

    def replace_destination(saved):
        if installation.is_symlink():
            raise RuntimeError("Export installation directory must not be a symlink.")
        if installation.exists():
            shutil.rmtree(installation)
        if saved is not None:
            try:
                shutil.copytree(saved, installation, symlinks=True)
                _sync_tree(installation)
            except Exception:
                if installation.exists():
                    shutil.rmtree(installation)
                raise
        if destination.exists():
            shutil.rmtree(destination)
        if saved is not None:
            os.replace(installation, destination)
        sync_directory(root)

    commit_transaction = Transaction(root, job / "export-commit", action_id, execute)
    with lock(common / "git-worker.lock"), lock(common / "codimango-export.lock"), lock(common / "cyber-bench-task-locks" / f"{name}.lock"):
        if state.get("phase") == "verified":
            return state["result"]
        with lock(common / "cyber-bench-commit.lock"):
            if read_git(root, "branch", "--show-current").stdout.strip() != "main":
                raise RuntimeError("Cyber Bench must have main checked out before export.")
            if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
                raise RuntimeError(f"Export destination must be a regular task directory: {destination}")
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
            if not state:
                if read_git(root, "status", "--porcelain", "--untracked-files=all", "--", f":(literal){name}").stdout:
                    raise RuntimeError(f"Cyber Bench has uncommitted changes in {name}; export stopped.")
                before = normalized_payload(source)
                if snapshot.exists():
                    shutil.rmtree(snapshot)
                shutil.copytree(source, snapshot, ignore=excluded)
                if before != normalized_payload(snapshot) or before != normalized_payload(source):
                    raise RuntimeError("Task files changed while copying the export; retry the local workflow.")
                if backup.exists():
                    shutil.rmtree(backup)
                if destination.exists():
                    shutil.copytree(destination, backup, symlinks=True)
                # Persist copied bytes before recording that destructive copying may begin.
                for copied in (snapshot, backup):
                    if copied.exists():
                        _sync_tree(copied)
                checkpoint("prepared", source=before, original_head=read_git(root, "rev-parse", "HEAD").stdout.strip(),
                           had_destination=destination.exists(), previous=normalized_payload(destination))
            if normalized_payload(snapshot) != state["source"]:
                raise RuntimeError("Saved export snapshot changed; recovery stopped.")
            recovered = commit_transaction.recover_commit()
            if state["phase"] in {"prepared", "copying", "copied", "rollback_pending", "rolled_back"} and not recovered:
                if read_git(root, "rev-parse", "HEAD").stdout.strip() != state["original_head"]:
                    raise RuntimeError("Repository HEAD changed during export recovery.")
                current = normalized_payload(destination)
                expected = state["source"]
                partial_copy = all(expected.get(key) == value for key, value in current.items())
                partial_previous = all(state["previous"].get(key) == value for key, value in current.items())
                interrupted_copy = state["phase"] in {"copying", "rollback_pending"} and (partial_copy or partial_previous)
                if current != state["previous"] and current != expected and not interrupted_copy:
                    raise RuntimeError("Export destination changed outside the recorded transaction; recovery stopped.")
                if state["phase"] == "rollback_pending":
                    if state["had_destination"] and normalized_payload(backup) != state["previous"]:
                        raise RuntimeError("Saved export backup changed; recovery stopped.")
                    replace_destination(backup if state["had_destination"] else None)
                    git("restore", "--staged", "--source=HEAD", "--", f":(literal){name}")
                    checkpoint("rolled_back")
                try:
                    checkpoint("copying")
                    replace_destination(snapshot)
                    checkpoint("copied")
                    paths = [path for path in read_git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", f":(literal){name}").stdout.split("\x00") if path]
                    tag = re.match(r"cyber-bench-([a-z]+)-.*?(\d+(?:\.\d+)?)(?:-|$)", name, re.I)
                    subject = f"[{tag[1].upper()} {tag[2]}]" if tag else "[codimango]"
                    commit, _ = commit_transaction.commit(
                        paths, f"{subject} Export {name}",
                        lambda: _verify_snapshot(destination, state["source"]),
                    )
                except Exception:
                    # Never restore a checkout after the transaction actually committed.
                    if not commit_transaction.recover_commit() and read_git(root, "rev-parse", "HEAD").stdout.strip() == state["original_head"]:
                        if state["had_destination"] and normalized_payload(backup) != state["previous"]:
                            raise RuntimeError("Saved export backup changed; recovery stopped.")
                        checkpoint("rollback_pending")
                        git("restore", "--staged", "--source=HEAD", "--", f":(literal){name}")
                        replace_destination(backup if state["had_destination"] else None)
                        checkpoint("rolled_back")
                    raise
                checkpoint("committed", commit=commit)
            else:
                _verify_snapshot(destination, state["source"])
                commit = recovered or state.get("commit") or commit_transaction.state.get("commit")
                if not commit:
                    raise RuntimeError("Cannot reconcile the export's saved Git commit.")
                checkpoint("committed", commit=commit)
            already_exported = (
                bool(read_git(root, "ls-tree", remote, "--", f":(literal){name}").stdout)
                and read_git(root, "diff", "--quiet", remote, "HEAD", "--", f":(literal){name}", allow_failure=True).returncode == 0
            )
            if not has_helper and not already_exported:
                checkpoint("push_pending")
                try:
                    git("push", "origin", f"{commit}:refs/heads/main")
                except Exception as error:
                    raise RuntimeError(f"Export commit {commit} was saved locally. {error}") from error
        if has_helper and not already_exported:
            checkpoint("push_pending")
            try:
                run([str(helper), "task", name])
            except Exception as error:
                raise RuntimeError(f"Export commit {commit} was saved locally. {error}") from error
        git("fetch", "--quiet", "origin", "main")
        pushed = read_git(root, "rev-parse", "refs/remotes/origin/main").stdout.strip()
        read_git(root, "diff", "--quiet", pushed, "HEAD", "--", f":(literal){name}")
        task_commit = read_git(root, "log", "-1", "--format=%H", pushed, "--", f":(literal){name}").stdout.strip()
        if not task_commit:
            raise RuntimeError("The remote main branch does not contain the exported task.")
        result = {
            "repo_path": str(root), "task_name": name, "destination": str(destination),
            "commit": task_commit, "push_commit": pushed, "branch": "main",
            "pushed": not already_exported, "already_exported": already_exported,
        }
        checkpoint("verified", result=result)
        return result


def _verify_snapshot(destination, expected):
    if {key: list(value) for key, value in payload(destination).items()} != expected:
        raise RuntimeError("Export destination does not match the saved task snapshot.")


def _sync_tree(directory):
    for parent, _, files in os.walk(directory):
        for filename in files:
            file = Path(parent) / filename
            if not file.is_symlink():
                with file.open("rb") as stream:
                    os.fsync(stream.fileno())
        sync_directory(Path(parent))
