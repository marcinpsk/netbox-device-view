"""Request code parses a primary key only through views._parse_pk(): isdigit() also accepts "²"."""

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "netbox_device_view"
REQUEST_MODULES = ("views.py", "api/views.py")


def isdigit_calls(source):
    """Return the line of each isdigit() call outside _parse_pk()."""
    found = []

    def visit(node, function):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                visit(child, child.name)
                continue
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == "isdigit"
                and function != "_parse_pk"
            ):
                found.append(child.lineno)
            visit(child, function)

    visit(ast.parse(source), None)
    return found


@pytest.mark.parametrize("module", REQUEST_MODULES)
def test_request_code_parses_ids_with_parse_pk(module):
    assert isdigit_calls((PACKAGE / module).read_text()) == []


def test_the_guard_finds_an_isdigit_check():
    assert isdigit_calls("def f(value):\n    return value.isdigit()\n") == [2]
    assert isdigit_calls("def _parse_pk(value):\n    return value.isdigit()\n") == []
