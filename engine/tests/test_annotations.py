"""Regression fixtures for the annotation gate beyond mypy's receiver inference."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from typing import Self

from tools.check_annotations import check_paths, check_source, main


class AnnotationTests(unittest.TestCase):
    def test_missing_receivers(self: Self) -> None:
        source = """
class Example:
    def instance(self) -> None: pass
    @classmethod
    def factory(cls) -> None: pass
"""
        errors = check_source(source)
        self.assertEqual(len(errors), 2)
        self.assertIn("parameter self", errors[0])
        self.assertIn("parameter cls", errors[1])

    def test_all_parameter_kinds_and_return(self: Self) -> None:
        errors = check_source(
            "def example(positional, /, normal, *args, keyword, **kwargs): pass"
        )
        self.assertEqual(len(errors), 6)
        for name in ("positional", "normal", "args", "keyword", "kwargs"):
            self.assertTrue(any(f"parameter {name}" in error for error in errors))
        self.assertTrue(any("return annotation" in error for error in errors))

    def test_nested_async_functions(self: Self) -> None:
        errors = check_source(
            "async def outer() -> None:\n    async def inner(value): pass\n"
        )
        self.assertEqual(len(errors), 2)
        self.assertTrue(all("inner:" in error for error in errors))

    def test_fully_annotated_source(self: Self) -> None:
        source = """
from __future__ import annotations
from typing import Self
class Example:
    def instance(self: Self, value: str, /, *args: int, flag: bool, **kwargs: str) -> None: pass
    @classmethod
    def factory(cls: type[Self]) -> Self: return cls()
    @staticmethod
    async def helper(value: int) -> int: return value
"""
        self.assertEqual(check_source(source), [])

    def test_file_discovery_and_cli_failure(self: Self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nested = root / "nested"
            nested.mkdir()
            file = nested / "fixture.py"
            file.write_text("def example(value): pass\n", "utf-8")
            self.assertEqual(len(check_paths([root, file])), 2)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main([str(root)]), 1)
                file.write_text("def example(value: int) -> None: pass\n", "utf-8")
                self.assertEqual(main([str(root)]), 0)
                file.write_text("def broken(\n", "utf-8")
                self.assertEqual(main([str(root)]), 1)


if __name__ == "__main__":
    unittest.main()
