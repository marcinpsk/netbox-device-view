from core.models import DataSource
from dcim.models import Device, DeviceType, Manufacturer, Module, ModuleType
from django import forms
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse
from django.utils.http import urlencode
from django.utils.text import capfirst, format_lazy
from django.utils.translation import gettext_lazy as _
from netbox.context import current_request
from netbox.forms import (
    NetBoxModelFilterSetForm,
    NetBoxModelForm,
    NetBoxModelImportForm,
)
from netbox.forms.mixins import ChangelogMessageMixin, SavedFiltersMixin
from utilities.exceptions import AbortRequest, PermissionsViolation
from utilities.forms import FilterForm
from utilities.forms.fields import (
    CSVModelChoiceField,
    DynamicModelChoiceField,
    DynamicModelMultipleChoiceField,
    TagFilterField,
)
from utilities.forms.rendering import FieldSet, TabbedGroups

from . import datasource_import
from .artwork import overrides, pins, review
from .artwork.manifest import (
    BINDING_COMPONENTS,
    DEVICE_KIND,
    MAX_BINDING_NAME_LENGTH,
    MAX_BINDINGS,
    MODULE_KIND,
)
from .components import BINDING_MODELS
from .models import (
    INSTANCE_FIELDS,
    SCOPE_FIELDS,
    TYPE_SCOPES,
    Association,
    BindingOverride,
    DefinitionRevision,
    DeviceView,
    InstancePin,
    VisualDefinition,
    type_scope,
)
from .utils import normalize_grid_template_area


class SanitizeGridTemplateAreaMixin:
    """Normalize the whitespace of grid_template_area before validation."""

    def clean_grid_template_area(self):
        value = self.cleaned_data.get("grid_template_area", "")
        if not value:
            return value
        return normalize_grid_template_area(value)


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


class DeviceViewFilterForm(NetBoxModelFilterSetForm):
    model = DeviceView
    fieldsets = (
        FieldSet("q", "filter_id", "tag"),
        FieldSet("manufacturer_id", "device_type_id", name=_("Hardware")),
    )
    manufacturer_id = DynamicModelMultipleChoiceField(
        queryset=Manufacturer.objects.all(), required=False, label=_("Manufacturer")
    )
    device_type_id = DynamicModelMultipleChoiceField(
        queryset=DeviceType.objects.all(),
        required=False,
        query_params={"manufacturer_id": "$manufacturer_id"},
        label=_("Device type"),
    )
    tag = TagFilterField(model)


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
    q = forms.CharField(required=False, label=_("Search paths"))

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["source"].queryset = datasource_import.importable_sources(user)

    def clean_prefix(self):
        return datasource_import.normalize_prefix(self.cleaned_data["prefix"])


class DataSourceImportTargetForm(forms.Form):
    """One device type selector and one Replace choice for each selected DataFile."""

    def __init__(self, *args, user, datafiles, catalog_rows=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.datafiles = list(datafiles)
        for datafile in self.datafiles:
            self.fields[self.field_name(datafile.pk)] = DynamicModelChoiceField(
                queryset=datasource_import.permitted_device_types(user),
                label=_("Device type"),
            )
            self.fields[self.replace_name(datafile.pk)] = forms.BooleanField(
                required=False, label=_("Replace an existing device view")
            )
        for row in catalog_rows:
            if row.is_selectable:
                self.initial[self.field_name(row.datafile.pk)] = row.suggestion.pk

    @staticmethod
    def field_name(datafile_id):
        return f"device_type_{datafile_id}"

    @staticmethod
    def replace_name(datafile_id):
        return f"replace_{datafile_id}"

    def rows(self):
        return [
            (
                datafile,
                self[self.field_name(datafile.pk)],
                self[self.replace_name(datafile.pk)],
            )
            for datafile in self.datafiles
        ]

    def selections(self):
        return [
            datasource_import.Selection(
                datafile_id=datafile.pk,
                device_type_id=self.cleaned_data[self.field_name(datafile.pk)].pk,
                replace=self.cleaned_data[self.replace_name(datafile.pk)],
            )
            for datafile in self.datafiles
        ]


class ArtworkBundleImportForm(forms.Form):
    bundle = forms.FileField(
        label=_("Bundle"),
        help_text=_("A .zip file with one directory per visual definition."),
    )


class ArtworkSourceImportForm(forms.Form):
    data_source = forms.ModelChoiceField(
        queryset=None,
        label=_("Data Source"),
        help_text=_("Only enabled Data Sources are listed."),
    )
    prefix = forms.CharField(
        required=False,
        label=_("Directory prefix"),
        help_text=_(
            "Path relative to the source root. Every file below it must belong to a"
            " definition directory."
        ),
    )

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["data_source"].queryset = DataSource.objects.restrict(
            user, "view"
        ).filter(enabled=True)

    def clean_prefix(self):
        return datasource_import.normalize_prefix(self.cleaned_data["prefix"])


class VisualDefinitionForm(ChangelogMessageMixin, forms.ModelForm):
    key_owner = DynamicModelChoiceField(
        queryset=DataSource.objects.all(),
        required=False,
        label=_("Key owner"),
        help_text=format_lazy(
            _(
                "The only source that can import revisions of this key. Leave empty for"
                " the {}."
            ),
            VisualDefinition.OFFLINE_UPLOADS,
        ),
    )

    class Meta:
        model = VisualDefinition
        fields = ("key_owner",)


# The text of the activation page and the help of its type field, for each definition kind.
_ACTIVATION_TEXTS = {
    DEVICE_KIND: (
        _(
            "Select the device types that show this revision of {key}. The review"
            " lists the devices that change and what the change breaks. Nothing"
            " changes until you activate the reviewed selection."
        ),
        _("The Device View of every device of these types shows the revision."),
    ),
    MODULE_KIND: (
        _(
            "Select the module types that show this revision of {key}. The review"
            " lists the installed modules and the devices that change, and what the"
            " change breaks. Nothing changes until you activate the reviewed"
            " selection."
        ),
        _(
            "Every installed module of these types shows the revision in the bay of"
            " its device."
        ),
    ),
}


class ActivateRevisionForm(forms.Form):
    """The types of the revision's kind that a review activates the revision for.

    The prefix of the form names the revision, so one query holds the types
    of several revisions: see review_url().
    """

    def __init__(self, *args, user, revision, **kwargs):
        super().__init__(*args, prefix=self.prefix_of(revision), **kwargs)
        kind = revision.definition.kind
        scope = TYPE_SCOPES[kind]
        self.revision = revision
        self.type_field = scope.field
        intro, help_text = _ACTIVATION_TEXTS[kind]
        self.intro = intro.format(key=revision.definition.key)
        self.fields[self.type_field] = DynamicModelMultipleChoiceField(
            queryset=scope.model.objects.restrict(user, "view"),
            label=capfirst(scope.model._meta.verbose_name_plural),
            help_text=help_text,
        )

    @staticmethod
    def prefix_of(revision) -> str:
        return f"r{revision.pk}"

    @property
    def type_bound_field(self):
        return self[self.type_field]

    @property
    def pairs(self) -> list[review.Pair]:
        return [
            review.Pair(hardware_type, self.revision)
            for hardware_type in self.cleaned_data[self.type_field]
        ]


def review_url(pairs, **params) -> str:
    """Return the URL of the review of ``pairs`` of (type, revision), in the format that ReviewForm reads."""
    query = {"revision": [], **params}
    for hardware_type, revision in pairs:
        if revision.pk not in query["revision"]:
            query["revision"].append(revision.pk)
        name = f"{ActivateRevisionForm.prefix_of(revision)}-{type_scope(hardware_type).field}"
        query.setdefault(name, []).append(hardware_type.pk)
    page = reverse("plugins:netbox_device_view:association_review")
    return f"{page}?{urlencode(query, doseq=True)}"


class ReviewForm(forms.Form):
    """The selection of a review: the revisions, and the types of each in its ActivateRevisionForm."""

    revision = forms.ModelMultipleChoiceField(queryset=None, required=False)
    # The associations that a review of an update leaves out: they have the newest revision.
    current = forms.ModelMultipleChoiceField(queryset=None, required=False)

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["revision"].queryset = DefinitionRevision.objects.restrict(
            user, "view"
        ).select_related("definition")
        self.fields["current"].queryset = Association.objects.restrict(
            user, "view"
        ).select_related("device_type__manufacturer", "module_type__manufacturer")
        self.pairs = []

    def clean(self):
        data = super().clean()
        for revision in data.get("revision") or ():
            form = ActivateRevisionForm(self.data, user=self.user, revision=revision)
            if not form.is_valid():
                for errors in form.errors.values():
                    for error in errors:
                        self.add_error(None, f"{revision}: {error}")
                continue
            self.pairs.extend(form.pairs)
        keys = [pair.key for pair in self.pairs]
        if len(set(keys)) != len(keys):
            raise ValidationError(_("Select each type once."))
        if len(keys) > review.MAX_PAIRS:
            raise ValidationError(
                _("Select at most {count} types in one review.").format(
                    count=review.MAX_PAIRS
                )
            )
        return data


class AssociationFilterForm(SavedFiltersMixin, FilterForm):
    model = Association
    fieldsets = (
        FieldSet("q", "filter_id"),
        FieldSet("device_type_id", "module_type_id", name=_("Type")),
    )
    device_type_id = DynamicModelMultipleChoiceField(
        queryset=DeviceType.objects.all(), required=False, label=_("Device type")
    )
    module_type_id = DynamicModelMultipleChoiceField(
        queryset=ModuleType.objects.all(), required=False, label=_("Module type")
    )


class VisualDefinitionFilterForm(SavedFiltersMixin, FilterForm):
    model = VisualDefinition
    fieldsets = (FieldSet("q", "filter_id"), FieldSet("key_owner_id"))
    key_owner_id = DynamicModelMultipleChoiceField(
        queryset=DataSource.objects.all(),
        required=False,
        null_option=capfirst(VisualDefinition.OFFLINE_UPLOADS),
        label=_("Key owner"),
    )


class BindingRowForm(forms.Form):
    """One binding of an override: a component type and a name."""

    component = forms.ChoiceField(
        choices=[
            ("", "---------"),
            *(
                (name, capfirst(BINDING_MODELS[name]._meta.verbose_name))
                for name in BINDING_COMPONENTS
            ),
        ],
        required=False,
        label=_("Component type"),
        # A plain select, as the script cannot give a new row the select of NetBox.
        widget=forms.Select(attrs={"class": "form-select no-ts"}),
    )
    name = forms.CharField(
        max_length=MAX_BINDING_NAME_LENGTH,
        required=False,
        label=_("Name"),
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # A row shows no label, so each input takes the name of its column.
        for field in self.fields.values():
            field.widget.attrs["aria-label"] = field.label

    def clean(self):
        data = super().clean()
        if bool(data.get("component")) != bool(data.get("name")):
            raise ValidationError(
                _("Give the component type and the name, or clear both.")
            )
        return data


class BaseBindingRows(forms.BaseFormSet):
    @property
    def bindings(self):
        """Return the binding list of the rows; an empty row is left out."""
        return [
            {
                "component": form.cleaned_data["component"],
                "name": form.cleaned_data["name"],
            }
            for form in self.forms
            if form.cleaned_data.get("component")
        ]


BindingRows = forms.formset_factory(
    BindingRowForm,
    formset=BaseBindingRows,
    extra=1,
    max_num=MAX_BINDINGS,
    absolute_max=MAX_BINDINGS,
    validate_max=True,
)


class WriterSaveMixin:
    """Save the instance of a model form through ``writer``, which takes the type lock and checks the saved row."""

    writer = None

    def save(self, commit=True):
        if not commit:
            raise ValueError(
                f"A {self.instance._meta.verbose_name} saves only through its writer."
            )
        # ObjectEditView gives the form no user; NetBox sets the request of each view.
        user = current_request.get().user
        try:
            return self.writer(user, self.instance)
        except ValidationError as exc:
            # ObjectEditView shows an AbortRequest or a PermissionsViolation on the form.
            raise AbortRequest(" ".join(exc.messages)) from exc
        except PermissionDenied as exc:
            raise PermissionsViolation from exc


class BindingOverrideForm(WriterSaveMixin, ChangelogMessageMixin, forms.ModelForm):
    """The scope and the connector of a new override, and the binding rows; only the rows change later."""

    device_type = DynamicModelChoiceField(
        queryset=DeviceType.objects.all(), required=False, label=_("Device type")
    )
    module_type = DynamicModelChoiceField(
        queryset=ModuleType.objects.all(), required=False, label=_("Module type")
    )
    device = DynamicModelChoiceField(
        queryset=Device.objects.all(), required=False, selector=True, label=_("Device")
    )
    module = DynamicModelChoiceField(
        queryset=Module.objects.all(),
        required=False,
        selector=True,
        label=_("Installed module"),
    )
    connector_key = forms.CharField(
        label=_("Connector key"),
        help_text=_(
            "The key of the connector in the active artwork, for example con-eth1. The"
            " revision page lists the keys."
        ),
    )
    # The binding rows give the value; the field shows the errors of the list.
    bindings = forms.JSONField(required=False, disabled=True, widget=forms.HiddenInput)

    fieldsets = (
        FieldSet(
            TabbedGroups(
                FieldSet("device_type", name=_("Device type")),
                FieldSet("module_type", name=_("Module type")),
                FieldSet("device", name=_("Device")),
                FieldSet("module", name=_("Installed module")),
            ),
            name=_("Scope"),
        ),
        FieldSet("connector_key", name=_("Connector")),
    )

    writer = staticmethod(overrides.save)

    class Meta:
        model = BindingOverride
        fields = (*SCOPE_FIELDS, "connector_key", "bindings")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.binding_rows = BindingRows(
            self.data if self.is_bound else None,
            prefix="binding",
            initial=self.instance.bindings,
        )
        if not self.instance._state.adding:
            for name in (*SCOPE_FIELDS, "connector_key"):
                self.fields[name].disabled = True

    def clean_bindings(self):
        if not self.binding_rows.is_valid():
            raise ValidationError(_("Correct the bindings."))
        return self.binding_rows.bindings

    def clean(self):
        data = super().clean()
        # The model form keeps the stored list for an empty value that the POST omits.
        if "bindings" in data:
            self.instance.bindings = data["bindings"]
        return data


class BindingOverrideFilterForm(SavedFiltersMixin, FilterForm):
    model = BindingOverride
    fieldsets = (
        FieldSet("q", "filter_id"),
        FieldSet(*(f"{name}_id" for name in SCOPE_FIELDS), name=_("Scope")),
        FieldSet("definition_id", "connector_key", name=_("Connector")),
    )
    device_type_id = DynamicModelMultipleChoiceField(
        queryset=DeviceType.objects.all(), required=False, label=_("Device type")
    )
    module_type_id = DynamicModelMultipleChoiceField(
        queryset=ModuleType.objects.all(), required=False, label=_("Module type")
    )
    device_id = DynamicModelMultipleChoiceField(
        queryset=Device.objects.all(), required=False, label=_("Device")
    )
    module_id = DynamicModelMultipleChoiceField(
        queryset=Module.objects.all(), required=False, label=_("Installed module")
    )
    definition_id = DynamicModelMultipleChoiceField(
        queryset=VisualDefinition.objects.all(),
        required=False,
        label=_("Definition key"),
    )
    connector_key = forms.CharField(required=False, label=_("Connector key"))


def _revision_field(kind):
    return DynamicModelChoiceField(
        queryset=DefinitionRevision.objects.filter(definition__kind=kind),
        required=False,
        query_params={"kind": kind},
        label=_("Revision"),
    )


class InstancePinForm(WriterSaveMixin, ChangelogMessageMixin, forms.ModelForm):
    """The device or the installed module of a new pin, with a revision of its kind; only the revision changes later."""

    device = DynamicModelChoiceField(
        queryset=Device.objects.all(), required=False, selector=True, label=_("Device")
    )
    device_revision = _revision_field(DEVICE_KIND)
    module = DynamicModelChoiceField(
        queryset=Module.objects.all(),
        required=False,
        selector=True,
        label=_("Installed module"),
    )
    module_revision = _revision_field(MODULE_KIND)

    fieldsets = (
        FieldSet(
            TabbedGroups(
                FieldSet("device", "device_revision", name=_("Device")),
                FieldSet("module", "module_revision", name=_("Installed module")),
            ),
            name=_("Pin"),
        ),
    )
    writer = staticmethod(pins.save)

    class Meta:
        model = InstancePin
        fields = INSTANCE_FIELDS

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance._state.adding:
            for name in INSTANCE_FIELDS:
                self.fields[name].disabled = True
                if name != self.instance.scope_field:
                    del self.fields[f"{name}_revision"]
            self.initial[f"{self.instance.scope_field}_revision"] = (
                self.instance.revision_id
            )

    def clean(self):
        data = super().clean()
        # The check constraint reports a missing or a second instance.
        chosen = [name for name in INSTANCE_FIELDS if data.get(name)]
        if len(chosen) == 1:
            field = f"{chosen[0]}_revision"
            if data.get(field) is None:
                self.add_error(field, self.fields[field].error_messages["required"])
            else:
                self.instance.revision = data[field]
        return data


class InstancePinFilterForm(SavedFiltersMixin, FilterForm):
    model = InstancePin
    fieldsets = (
        FieldSet("q", "filter_id"),
        FieldSet("device_id", "module_id", name=_("Instance")),
        FieldSet("definition_id", "revision_id", name=_("Revision")),
    )
    device_id = DynamicModelMultipleChoiceField(
        queryset=Device.objects.all(), required=False, label=_("Device")
    )
    module_id = DynamicModelMultipleChoiceField(
        queryset=Module.objects.all(), required=False, label=_("Installed module")
    )
    definition_id = DynamicModelMultipleChoiceField(
        queryset=VisualDefinition.objects.all(),
        required=False,
        label=_("Definition key"),
    )
    revision_id = DynamicModelMultipleChoiceField(
        queryset=DefinitionRevision.objects.all(), required=False, label=_("Revision")
    )
