import django_tables2 as tables
from django.utils.html import mark_safe
from django.utils.text import capfirst
from netbox.tables import NetBoxTable, columns

from .components import BINDING_MODELS, component_label
from .models import (
    Association,
    BindingOverride,
    DefinitionRevision,
    DeviceView,
    VisualDefinition,
)


class LayoutColumn(tables.Column):
    """Show YAML and/or CSS badges depending on which layout data is present."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("orderable", False)
        kwargs.setdefault("verbose_name", "Layout")
        super().__init__(*args, **kwargs)

    def render(self, record):
        badges = []
        if record.has_yaml_layout:
            badges.append('<span class="badge text-bg-success">YAML</span>')
        if record.grid_template_area and record.grid_template_area.strip():
            badges.append('<span class="badge text-bg-secondary">CSS</span>')
        return (
            mark_safe(" ".join(badges))
            if badges
            else mark_safe('<span class="text-muted">—</span>')
        )

    def value(self, record):
        parts = []
        if record.has_yaml_layout:
            parts.append("YAML")
        if record.grid_template_area and record.grid_template_area.strip():
            parts.append("CSS")
        return ", ".join(parts) if parts else "—"


class DeviceViewTable(NetBoxTable):
    device_type = tables.Column(
        verbose_name="Device Type",
        linkify=lambda record: record.device_type.get_absolute_url(),
        accessor="device_type",
        order_by=("device_type__manufacturer__name", "device_type__model"),
    )

    device_count = columns.LinkedCountColumn(
        viewname="dcim:device_list",
        url_params={"device_type_id": "device_type_id"},
        verbose_name="Devices",
    )

    layout = LayoutColumn(accessor="pk")

    render_mode = columns.ChoiceFieldColumn(verbose_name="Render Mode")

    class Meta(NetBoxTable.Meta):
        model = DeviceView
        fields = ("pk", "id", "device_type", "device_count", "layout", "render_mode")
        default_columns = ("device_type", "device_count", "layout", "render_mode")


class VisualDefinitionTable(NetBoxTable):
    key = tables.Column(verbose_name="Definition key", linkify=True)
    kind = columns.ChoiceFieldColumn(verbose_name="Kind")
    key_owner = tables.Column(
        verbose_name="Key owner",
        linkify=True,
        default=capfirst(VisualDefinition.OFFLINE_UPLOADS),
    )
    revision_count = tables.Column(verbose_name="Revisions")
    actions = columns.ActionsColumn(actions=("edit", "changelog"))

    class Meta(NetBoxTable.Meta):
        model = VisualDefinition
        fields = ("pk", "id", "key", "kind", "key_owner", "revision_count", "actions")
        default_columns = ("key", "kind", "key_owner", "revision_count")


class DefinitionRevisionTable(NetBoxTable):
    key = tables.Column(
        verbose_name="Definition key",
        accessor="definition__key",
        linkify=lambda record: record.get_absolute_url(),
    )
    kind = tables.Column(verbose_name="Kind", accessor="definition__kind")
    title = tables.Column(
        verbose_name="Title", accessor="manifest__title", orderable=False
    )
    sku = tables.Column(
        verbose_name="SKU", accessor="manifest__hardware__sku", orderable=False
    )
    license = tables.Column(
        verbose_name="Licence", accessor="manifest__license", orderable=False
    )
    digest = tables.TemplateColumn(
        verbose_name="Digest",
        template_code='<span class="font-monospace" title="{{ value }}">{{ value|truncatechars:13 }}</span>',
    )
    created = columns.DateTimeColumn(verbose_name="Imported")
    imported_by = tables.Column(verbose_name="Imported by")
    source_name = tables.Column(verbose_name="Source")
    actions = columns.ActionsColumn(actions=("delete", "changelog"))

    class Meta(NetBoxTable.Meta):
        model = DefinitionRevision
        fields = (
            "pk",
            "id",
            "key",
            "kind",
            "title",
            "sku",
            "license",
            "digest",
            "created",
            "imported_by",
            "source_name",
            "actions",
        )
        default_columns = (
            "key",
            "kind",
            "title",
            "sku",
            "license",
            "digest",
            "created",
            "imported_by",
            "source_name",
        )


class AssociationTable(NetBoxTable):
    id = tables.Column(linkify=True, verbose_name="ID")
    hardware_type = tables.Column(
        verbose_name="Type",
        accessor="device_type",
        # A module association has no device type; render it anyway.
        empty_values=(),
        linkify=lambda record: record.hardware_type.get_absolute_url(),
        order_by=(
            "device_type__manufacturer__name",
            "device_type__model",
            "module_type__manufacturer__name",
            "module_type__model",
        ),
    )
    key = tables.Column(
        verbose_name="Definition key",
        accessor="revision__definition__key",
        linkify=lambda record: record.revision.definition.get_absolute_url(),
    )
    revision = tables.TemplateColumn(
        verbose_name="Active revision",
        accessor="revision__digest",
        linkify=lambda record: record.revision.get_absolute_url(),
        template_code='<span class="font-monospace" title="{{ value }}">{{ value|truncatechars:13 }}</span>',
    )
    last_updated = columns.DateTimeColumn(verbose_name="Activated")
    actions = columns.ActionsColumn(actions=("delete", "changelog"))

    class Meta(NetBoxTable.Meta):
        model = Association
        fields = (
            "pk",
            "id",
            "hardware_type",
            "key",
            "revision",
            "last_updated",
            "actions",
        )
        default_columns = ("id", "hardware_type", "key", "revision", "last_updated")

    def render_hardware_type(self, record):
        return record.hardware_type.full_name


def binding_labels(bindings) -> str:
    """Return the bindings of an override as text, for example "Interface eth1, Console port eth1"."""
    return ", ".join(
        component_label(BINDING_MODELS[binding["component"]], binding["name"])
        for binding in bindings
    )


class BindingOverrideTable(NetBoxTable):
    id = tables.Column(linkify=True, verbose_name="ID")
    scope = tables.Column(
        verbose_name="Scope",
        accessor="pk",
        orderable=False,
        linkify=lambda record: record.scope.get_absolute_url(),
    )
    definition = tables.Column(
        verbose_name="Definition key",
        accessor="definition__key",
        linkify=lambda record: record.definition.get_absolute_url(),
    )
    connector_key = tables.Column(verbose_name="Connector", linkify=True)
    bindings = tables.Column(verbose_name="Bindings", orderable=False)
    last_updated = columns.DateTimeColumn(verbose_name="Last updated")
    actions = columns.ActionsColumn(actions=("edit", "delete", "changelog"))

    class Meta(NetBoxTable.Meta):
        model = BindingOverride
        fields = (
            "pk",
            "id",
            "scope",
            "definition",
            "connector_key",
            "bindings",
            "last_updated",
            "actions",
        )
        default_columns = ("scope", "definition", "connector_key", "bindings")

    def render_scope(self, record):
        return record.scope_label

    def render_bindings(self, value):
        return binding_labels(value) or mark_safe(
            '<span class="text-muted">No binding</span>'
        )

    def value_bindings(self, value):
        return binding_labels(value)
