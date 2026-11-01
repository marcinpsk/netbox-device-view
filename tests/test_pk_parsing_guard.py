"""Request code parses a primary key only through views._parse_pk() or the model field: isdigit() also accepts "²"."""

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


def raw_url_kwargs(source):
    """Return the line of each self.kwargs read that no to_python() call coerces."""
    tree = ast.parse(source)
    coerced = {
        id(inner)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "to_python"
        for arg in node.args
        for inner in ast.walk(arg)
    }
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr == "kwargs"
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and id(node) not in coerced
    )


@pytest.mark.parametrize("module", REQUEST_MODULES)
def test_request_code_parses_ids_with_parse_pk(module):
    assert isdigit_calls((PACKAGE / module).read_text()) == []


def test_the_guard_finds_an_isdigit_check():
    assert isdigit_calls("def f(value):\n    return value.isdigit()\n") == [2]
    assert isdigit_calls("def _parse_pk(value):\n    return value.isdigit()\n") == []


def test_api_code_coerces_url_ids_before_a_query():
    # DRF routes accept any id, and only its own get_object() answers 404 for a bad one.
    assert raw_url_kwargs((PACKAGE / "api/views.py").read_text()) == []


def test_the_guard_finds_a_raw_url_id():
    found = raw_url_kwargs("lock([self.kwargs['pk']])\nself.kwargs.get('pk')\n")
    assert found == [1, 2]
    assert raw_url_kwargs("pk = field.to_python(self.kwargs.get('pk'))\n") == []
