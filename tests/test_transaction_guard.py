"""A transaction on the branch connection of a model also opens one on main: NetBox Branching writes its ChangeDiff rows there."""

import ast
from pathlib import Path

from django.apps import apps

from netbox_device_view.artwork.branching import CATALOGUE

PACKAGE = Path(__file__).resolve().parent.parent / "netbox_device_view"
# Catalogue models always route to main, so one transaction covers them. NetBox Branching exempts core models too.
MAIN_ONLY = {label.split(".")[1] for label in CATALOGUE} | {
    model._meta.model_name for model in apps.get_app_config("core").get_models()
}


def _using(call):
    if not (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "atomic"
    ):
        return None
    return next((k.value for k in call.keywords if k.arg == "using"), None)


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
    return (isinstance(using, ast.Name) and using.id == "DEFAULT_DB_ALIAS") or (
        isinstance(using, ast.Constant) and using.value == "default"
    )


def unpaired_branch_transactions(source):
    """Return the line of each routed transaction of a branched model without a transaction on main beside it."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.With | ast.AsyncWith):
            continue
        calls = [item.context_expr for item in node.items]
        on_main = any(_on_main(call) for call in calls)
        for call in calls:
            model = _routed_model(call)
            if model and model.lower() not in MAIN_ONLY and not on_main:
                found.append(call.lineno)
    return found


def test_every_branch_transaction_also_covers_main():
    for path in sorted(PACKAGE.rglob("*.py")):
        assert unpaired_branch_transactions(path.read_text()) == [], path


def test_the_guard_finds_a_branch_transaction_without_main():
    alone = (
        "with transaction.atomic(using=router.db_for_write(Association)):\n    pass\n"
    )
    assert unpaired_branch_transactions(alone) == [1]
    paired = (
        "with (\n"
        "    transaction.atomic(using=DEFAULT_DB_ALIAS),\n"
        "    transaction.atomic(using=router.db_for_write(models.DeviceView)),\n"
        "):\n"
        "    pass\n"
    )
    assert unpaired_branch_transactions(paired) == []
    catalogue = "with transaction.atomic(using=router.db_for_write(models.VisualDefinition)):\n    pass\n"
    assert unpaired_branch_transactions(catalogue) == []
    core = "with transaction.atomic(using=router.db_for_write(DataFile)):\n    pass\n"
    assert unpaired_branch_transactions(core) == []
