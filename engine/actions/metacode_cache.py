"""Stage cached MetaCode binaries without concurrent shared LATEST downloads."""

from __future__ import annotations

import errno
import os
import platform
import shutil
import stat
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

FILES = ("metacode", "metacode-bin")


def valid_cache(directory: Path) -> bool:
    try:
        for name in FILES:
            details = (directory / name).stat()
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_size <= 0
                or not details.st_mode & 0o111
            ):
                return False
        return True
    except OSError:
        return False


def cached_source(environment: Mapping[str, str]) -> Path | None:
    explicit = environment.get("SYNTHETIC_METACODE_CACHE_SOURCE")
    if explicit:
        source = Path(explicit)
        return source if valid_cache(source) else None
    root = Path(
        environment.get(
            "SYNTHETIC_METACODE_SHARED_CACHE",
            str(Path.home() / ".cache" / "harbor" / "metacode"),
        )
    )
    machine = platform.machine().lower()
    architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(machine, machine)
    directory = root / f"{platform.system().lower()}-{architecture}"
    try:
        candidates = sorted(
            directory.iterdir(), key=lambda path: (path.name != "LATEST", path.name)
        )
    except OSError:
        return None
    return next((path for path in candidates if valid_cache(path)), None)


@contextmanager
def metacode_local_cache(
    environment: Mapping[str, str] | None = None,
) -> Iterator[Path]:
    environment = os.environ if environment is None else environment
    configured = environment.get("HARBOR_METACODE_LOCAL_CACHE")
    if configured:
        directory = Path(configured)
        if not valid_cache(directory):
            raise RuntimeError(
                "HARBOR_METACODE_LOCAL_CACHE must contain executable metacode "
                f"and metacode-bin files: {directory}"
            )
        yield directory
        return
    source = cached_source(environment)
    if source is None:
        raise RuntimeError(
            "MetaCode cache is not populated; set HARBOR_METACODE_LOCAL_CACHE "
            "to a flat directory containing executable metacode and metacode-bin files."
        )
    with tempfile.TemporaryDirectory(prefix="synthetic-benchmark-metacode-") as temporary:
        directory = Path(temporary)
        try:
            for name in FILES:
                try:
                    os.link(source / name, directory / name)
                except OSError as error:
                    if error.errno != errno.EXDEV:
                        raise
                    shutil.copyfile(source / name, directory / name)
                    (directory / name).chmod(0o755)
            if not valid_cache(directory):
                raise RuntimeError("Staged files failed validation.")
        except OSError as error:
            raise RuntimeError(
                f"Could not stage the MetaCode local cache: {error}"
            ) from error
        yield directory
