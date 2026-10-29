"""Require explicit parameters (including receivers) and returns on every def.

Mypy checks annotation correctness; this check also requires annotations on
self, cls, nested functions, and variadic parameters. Python's lambda syntax
cannot express annotations, so lambdas are checked by mypy in their typed context.
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parent.parent


def check_source(source: str, filename: str = "<source>") -> list[str]:
    tree = ast.parse(source, filename=filename)
    errors: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        parameters = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        if node.args.vararg is not None:
            parameters.append(node.args.vararg)
        if node.args.kwarg is not None:
            parameters.append(node.args.kwarg)
        for parameter in parameters:
            if parameter.annotation is None:
                errors.append(
                    f"{filename}:{parameter.lineno}: {node.name}: missing annotation for parameter {parameter.arg}"
                )
        if node.returns is None:
            errors.append(
                f"{filename}:{node.lineno}: {node.name}: missing return annotation"
            )
    return errors


def check_paths(paths: Sequence[Path]) -> list[str]:
    files: set[Path] = set()
    for path in paths:
        if path.is_dir():
            files.update(path.rglob("*.py"))
        else:
            files.add(path)
    errors: list[str] = []
    for file in sorted(files):
        try:
            errors.extend(check_source(file.read_text("utf-8"), str(file)))
        except (OSError, UnicodeError, SyntaxError) as error:
            errors.append(f"{file}: cannot check source: {error}")
    return errors


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths", nargs="*", type=Path, default=[ROOT / "engine", ROOT / "tools"]
    )
    arguments = parser.parse_args(argv)
    errors = check_paths(arguments.paths)
    if errors:
        print("\n".join(errors))
        return 1
    print("All function parameters and returns have explicit annotations.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
