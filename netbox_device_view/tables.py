import django_tables2 as tables
from django.utils.html import mark_safe
from netbox.tables import NetBoxTable, columns

from .models import DefinitionRevision, DeviceView


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
