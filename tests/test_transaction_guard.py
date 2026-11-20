"""A transaction on the branch connection of a model also opens one on main: NetBox Branching writes its ChangeDiff rows there."""

import ast
from pathlib import Path

import pytest
from django.apps import apps

from netbox_device_view.artwork.branching import CATALOGUE

PACKAGE = Path(__file__).resolve().parent.parent / "netbox_device_view"
# Catalogue models always route to main, so one transaction covers them. NetBox Branching exempts core models too.
MAIN_ONLY = {label.split(".")[1] for label in CATALOGUE} | {
    model._meta.model_name for model in apps.get_app_config("core").get_models()
}


_NO_USING = object()


def _using(call):
    """Return the using argument of a transaction.atomic() call, _NO_USING without one, else None."""
    if not (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "atomic"
    ):
        return None
    if call.args:
        return call.args[0]
    return next((k.value for k in call.keywords if k.arg == "using"), _NO_USING)


def _routed_model(call):
    """Return the model name of atomic(using=router.db_for_write(Model)), else None."""
    using = _using(call)
    if (
        isinstance(using, ast.Call)
        and isinstance(using.func, ast.Attribute)
        and using.func.attr == "db_for_write"
        and using.args
    ):
        model = using.args[0]
        return model.attr if isinstance(model, ast.Attribute) else model.id
    return None


def _on_main(call):
    using = _using(call)
    return (
        using is _NO_USING
        or (isinstance(using, ast.Name) and using.id == "DEFAULT_DB_ALIAS")
        or (isinstance(using, ast.Constant) and using.value == "default")
    )


def _visit(node, on_main, found):
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
        # A function body runs later, outside the transaction that encloses its definition.
        on_main = False
    if isinstance(node, ast.With | ast.AsyncWith):
        # Python exits the items in reverse order, so main covers only the items after it.
        for item in node.items:
            call = item.context_expr
            model = _routed_model(call)
            if model and model.lower() not in MAIN_ONLY and not on_main:
                found.append(call.lineno)
            on_main = on_main or _on_main(call)
    for child in ast.iter_child_nodes(node):
        _visit(child, on_main, found)


def unpaired_branch_transactions(source):
    """Return the line of each routed transaction of a branched model that no earlier transaction on main covers."""
    found = []
    _visit(ast.parse(source), False, found)
    return found


def test_every_branch_transaction_also_covers_main():
    for path in sorted(PACKAGE.rglob("*.py")):
        assert unpaired_branch_transactions(path.read_text()) == [], path


ROUTED = "transaction.atomic(using=router.db_for_write(Association))"
MAIN = "transaction.atomic(using=DEFAULT_DB_ALIAS)"
UNPAIRED = {
    "alone": f"with {ROUTED}:\n    pass\n",
    "positional using": "with transaction.atomic(router.db_for_write(Association)):\n    pass\n",
    "main after routed": f"with {ROUTED}, {MAIN}:\n    pass\n",
    "after the main block": f"with {MAIN}:\n    pass\nwith {ROUTED}:\n    pass\n",
    "in a function of the main block": f"with {MAIN}:\n    def f():\n        with {ROUTED}:\n            pass\n",
    "beside an unrouted context": f"with lock, {ROUTED}:\n    pass\n",
}
PAIRED = {
    "main first": f"with {MAIN}, {ROUTED}:\n    pass\n",
    "default alias": f"with transaction.atomic(using='default'), {ROUTED}:\n    pass\n",
    "main without using": f"with transaction.atomic(), {ROUTED}:\n    pass\n",
    "enclosing main": f"with {MAIN}:\n    x = 1\n    with {ROUTED}:\n        pass\n",
    "catalogue model": "with transaction.atomic(using=router.db_for_write(models.VisualDefinition)):\n    pass\n",
    "core model": "with transaction.atomic(using=router.db_for_write(DataFile)):\n    pass\n",
}


@pytest.mark.parametrize("source", UNPAIRED.values(), ids=UNPAIRED.keys())
def test_the_guard_finds_a_branch_transaction_without_main(source):
    assert len(unpaired_branch_transactions(source)) == 1


@pytest.mark.parametrize("source", PAIRED.values(), ids=PAIRED.keys())
def test_the_guard_accepts_a_branch_transaction_inside_main(source):
    assert unpaired_branch_transactions(source) == []
