import unicodedata

from dcim.models import DeviceType
from django import forms
from django.utils.translation import gettext_lazy as _
from netbox.forms import NetBoxModelForm, NetBoxModelImportForm
from utilities.forms.fields import CSVModelChoiceField, DynamicModelChoiceField

from . import datasource_import
from .models import DeviceView


class SanitizeGridTemplateAreaMixin:
    """
    Normalize non-standard whitespace characters in the grid_template_area
    field before validation/save.

    The CSS `grid-template-areas` property is sensitive to the exact
    whitespace used to separate tokens within each quoted row string.
    Pasting layout definitions from documentation, examples, or other
    sources (especially from rendered web pages or word processors) can
    silently introduce non-breaking spaces (U+00A0) and other Unicode
    space separators that look identical to a normal space in the NetBox
    UI's text field but are invalid as `grid-template-areas` separators.

    This normalizes every Unicode "space separator" (category Zs) to a
    regular ASCII space, while leaving newlines untouched so multi-row
    definitions keep their line structure.
    """

    def clean_grid_template_area(self):
        value = self.cleaned_data.get("grid_template_area", "")
        if not value:
            return value
        return "".join(" " if unicodedata.category(ch) == "Zs" else ch for ch in value)


class DeviceViewForm(SanitizeGridTemplateAreaMixin, NetBoxModelForm):
    class Meta:
        model = DeviceView
        fields = ("device_type", "grid_template_area", "yaml_layout", "render_mode")


class DeviceViewImportForm(SanitizeGridTemplateAreaMixin, NetBoxModelImportForm):
    device_type = CSVModelChoiceField(
        queryset=DeviceType.objects.all(),
        to_field_name="model",
        help_text=_("Device Model Name"),
    )

    class Meta:
        model = DeviceView
        fields = ("device_type", "grid_template_area", "yaml_layout", "render_mode")


class DataSourceImportSourceForm(forms.Form):
    source = forms.ModelChoiceField(
        queryset=None,
        label=_("Data Source"),
        help_text=_("Only synchronized Data Sources are listed."),
    )
    prefix = forms.CharField(
        required=False,
        initial="examples/",
        label=_("Directory prefix"),
        help_text=_("Path relative to the source root. Leave empty for the root."),
    )

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["source"].queryset = datasource_import.importable_sources(user)

    def clean_prefix(self):
        return datasource_import.normalize_prefix(self.cleaned_data["prefix"])


class DataSourceImportTargetForm(forms.Form):
    """One device type selector for each selected DataFile."""

    def __init__(self, *args, user, datafiles, **kwargs):
        super().__init__(*args, **kwargs)
        self.datafiles = list(datafiles)
        for datafile in self.datafiles:
            self.fields[self.field_name(datafile.pk)] = DynamicModelChoiceField(
                queryset=DeviceType.objects.restrict(user, "view"),
                label=_("Device type"),
            )

    @staticmethod
    def field_name(datafile_id):
        return f"device_type_{datafile_id}"

    def rows(self):
        return [
            (datafile, self[self.field_name(datafile.pk)])
            for datafile in self.datafiles
        ]

    def selections(self):
        return [
            datasource_import.Selection(
                datafile_id=datafile.pk,
                device_type_id=self.cleaned_data[self.field_name(datafile.pk)].pk,
            )
            for datafile in self.datafiles
        ]
