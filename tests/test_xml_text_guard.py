"""Each text that the artwork renderer gives lxml goes through _xml_text, as lxml refuses characters that XML does not allow."""

import ast
from pathlib import Path

RENDER = Path(__file__).resolve().parent.parent / "netbox_device_view/artwork/render.py"


def unfiltered_text(source):
    """Return the line of each ``x.text = value`` whose value is not a call of _xml_text.

    A tuple assignment copies text from one parsed element to another, so it is left out.
    """
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AugAssign | ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if not any(isinstance(t, ast.Attribute) and t.attr == "text" for t in targets):
            continue
        value = node.value
        if not (
            isinstance(node, ast.Assign | ast.AnnAssign)
            and isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "_xml_text"
        ):
            found.append(node.lineno)
    return found


def test_every_text_of_the_renderer_goes_through_xml_text():
    assert unfiltered_text(RENDER.read_text()) == []


def test_the_guard_finds_a_text_without_xml_text():
    source = (
        "a.text = name\n"
        "b.text += name\n"
        "c.text = _xml_text(name)\n"
        "d.text, d.tail = e.text, e.tail\n"
        "SubElement(f, 'title').text = f'{name}'\n"
    )
    assert unfiltered_text(source) == [1, 2, 5]
