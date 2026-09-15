"""The suite AUTEF hands back must not be redder than the one it was given.

These cover the rule that makes that true: a test this framework wrote and
could not repair is taken back out, and a test the project wrote is never
touched no matter how red it is.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from autef2.agents.generation import GENERATED_MARKER
from autef2.quarantine import (
    has_tests,
    quarantine,
    sidecar_for,
    sweep,
)


def _generated(tmp_path: Path, body: str, name: str = "test_thing.py") -> Path:
    path = tmp_path / name
    path.write_text(
        f'"""Written by AUTEF. {GENERATED_MARKER}"""\n\n' + textwrap.dedent(body),
        encoding="utf-8",
    )
    return path


class _Record:
    """The parts of a RepairRecord that sweep() reads."""

    def __init__(self, nodeid, test_file, test_function, **kw):
        self.nodeid = nodeid
        self.test_file = str(test_file)
        self.test_function = test_function
        self.test_class = kw.get("test_class")
        self.fixed = kw.get("fixed", False)
        self.skipped_reason = kw.get("skipped_reason")
        self.attempts = kw.get("attempts", [])
        self.diagnosis = kw.get("diagnosis")


def test_project_authored_tests_are_never_touched(tmp_path):
    """The one mistake that must be impossible: editing the user's tests."""
    path = tmp_path / "test_theirs.py"
    original = "def test_theirs():\n    assert False\n"
    path.write_text(original, encoding="utf-8")

    result = quarantine(
        str(path), "test_theirs", nodeid="test_theirs.py::test_theirs",
        reason="whatever",
    )

    assert result is None
    assert path.read_text(encoding="utf-8") == original, "their file was edited"
    assert not sidecar_for(path).exists()


def test_generated_test_is_excised_and_kept_in_a_sidecar(tmp_path):
    path = _generated(tmp_path, """
        def test_keeps_working():
            assert 1 == 1


        def test_wrong_guess():
            assert compute() == 42
    """)

    result = quarantine(
        str(path), "test_wrong_guess",
        nodeid="test_thing.py::test_wrong_guess",
        reason="repair exhausted after 3 attempt(s)",
    )

    assert result is not None
    remaining = path.read_text(encoding="utf-8")
    assert "test_wrong_guess" not in remaining, "the bad test is still collected"
    assert "test_keeps_working" in remaining, "a good test was taken with it"

    sidecar = sidecar_for(path)
    assert sidecar.exists(), "the removal left no evidence"
    text = sidecar.read_text(encoding="utf-8")
    assert "test_wrong_guess" in text and "repair exhausted" in text
    assert not sidecar.name.startswith("test_"), "pytest would collect the sidecar"


def test_a_unittest_method_takes_its_class_when_it_was_the_only_one(tmp_path):
    """Excising the last method would otherwise leave a body-less class."""
    path = _generated(tmp_path, """
        import unittest


        class TestOnly(unittest.TestCase):
            def setUp(self):
                self.value = 1

            def test_wrong(self):
                self.assertEqual(self.value, 2)
    """)

    result = quarantine(
        str(path), "test_wrong", test_class="TestOnly",
        nodeid="test_thing.py::TestOnly::test_wrong", reason="wrong",
    )

    assert result is not None
    remaining = path.read_text(encoding="utf-8")
    compile(remaining, str(path), "exec")  # the file must still parse
    assert "class TestOnly" not in remaining


def test_a_class_with_other_tests_keeps_them(tmp_path):
    path = _generated(tmp_path, """
        import unittest


        class TestPair(unittest.TestCase):
            def test_good(self):
                self.assertEqual(1, 1)

            def test_bad(self):
                self.assertEqual(1, 2)
    """)

    quarantine(
        str(path), "test_bad", test_class="TestPair",
        nodeid="x::TestPair::test_bad", reason="wrong",
    )

    remaining = path.read_text(encoding="utf-8")
    compile(remaining, str(path), "exec")
    assert "test_good" in remaining and "test_bad" not in remaining
    assert "class TestPair" in remaining


def test_sweep_leaves_repaired_tests_alone_and_removes_the_rest(tmp_path):
    path = _generated(tmp_path, """
        def test_fixed_one():
            assert True


        def test_never_fixed():
            assert False
    """)
    theirs = tmp_path / "test_theirs.py"
    theirs.write_text("def test_theirs():\n    assert False\n", encoding="utf-8")

    result = sweep([
        _Record("x::test_fixed_one", path, "test_fixed_one", fixed=True),
        _Record("x::test_never_fixed", path, "test_never_fixed",
                skipped_reason="not repairable by editing the test"),
        _Record("y::test_theirs", theirs, "test_theirs"),
    ])

    assert result.count == 1
    assert result.removed[0].test_function == "test_never_fixed"
    remaining = path.read_text(encoding="utf-8")
    assert "test_fixed_one" in remaining and "test_never_fixed" not in remaining
    assert theirs.read_text(encoding="utf-8").count("assert False") == 1
    assert any("not ours" in k["why"] for k in result.kept)


def test_a_file_emptied_of_tests_is_removed(tmp_path):
    path = _generated(tmp_path, """
        def test_only_one():
            assert False
    """)

    result = sweep([
        _Record("x::test_only_one", path, "test_only_one",
                skipped_reason="exhausted"),
    ])

    assert result.count == 1
    assert not path.exists(), "an empty generated file was left behind"
    assert str(path) in result.files_deleted
    assert sidecar_for(path).exists(), "the evidence went with it"


def test_has_tests_sees_both_shapes(tmp_path):
    plain = tmp_path / "a.py"
    plain.write_text("def test_x():\n    pass\n", encoding="utf-8")
    klass = tmp_path / "b.py"
    klass.write_text("class TestX:\n    def test_y(self):\n        pass\n", encoding="utf-8")
    empty = tmp_path / "c.py"
    empty.write_text("import os\n", encoding="utf-8")

    assert has_tests(plain) and has_tests(klass)
    assert not has_tests(empty)
