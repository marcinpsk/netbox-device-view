import django_filters
from core.models import DataSource
from dcim.models import Device, DeviceType, Manufacturer, Module, ModuleType
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from netbox.filtersets import ChangeLoggedModelFilterSet, NetBoxModelFilterSet
from utilities.filtersets import register_filterset

from .models import (
    Association,
    BindingOverride,
    DefinitionRevision,
    DeviceView,
    InstancePin,
    VisualDefinition,
)


@register_filterset
class DeviceViewFilterSet(NetBoxModelFilterSet):
    manufacturer_id = django_filters.ModelMultipleChoiceFilter(
        field_name="device_type__manufacturer",
        queryset=Manufacturer.objects.all(),
        label=_("Manufacturer (ID)"),
    )
    manufacturer = django_filters.ModelMultipleChoiceFilter(
        field_name="device_type__manufacturer__slug",
        queryset=Manufacturer.objects.all(),
        to_field_name="slug",
        label=_("Manufacturer (slug)"),
    )
    device_type_id = django_filters.ModelMultipleChoiceFilter(
        queryset=DeviceType.objects.all(),
        label=_("Device type (ID)"),
    )
    device_type = django_filters.ModelMultipleChoiceFilter(
        field_name="device_type__slug",
        queryset=DeviceType.objects.all(),
        to_field_name="slug",
        label=_("Device type (slug)"),
    )

    class Meta:
        model = DeviceView
        fields = ("id",)

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(device_type__model__icontains=value)
            | Q(device_type__manufacturer__name__icontains=value)
        )


@register_filterset
class DefinitionRevisionFilterSet(ChangeLoggedModelFilterSet):
    q = django_filters.CharFilter(method="search", label=_("Search"))
    kind = django_filters.CharFilter(field_name="definition__kind", label=_("Kind"))

    class Meta:
        model = DefinitionRevision
        fields = ("id", "digest", "definition")

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(definition__key__icontains=value)
            | Q(digest__istartswith=value)
            | Q(manifest__title__icontains=value)
            | Q(manifest__hardware__sku__icontains=value)
        )


@register_filterset
class VisualDefinitionFilterSet(ChangeLoggedModelFilterSet):
    q = django_filters.CharFilter(method="search", label=_("Search"))
    # "null" selects the offline uploads.
    key_owner_id = django_filters.ModelMultipleChoiceFilter(
        field_name="key_owner",
        queryset=DataSource.objects.all(),
        label=_("Key owner (ID)"),
    )

    class Meta:
        model = VisualDefinition
        fields = ("id", "key", "kind")

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(key__icontains=value)


@register_filterset
class AssociationFilterSet(ChangeLoggedModelFilterSet):
    q = django_filters.CharFilter(method="search", label=_("Search"))
    device_type_id = django_filters.ModelMultipleChoiceFilter(
        queryset=DeviceType.objects.all(), label=_("Device type (ID)")
    )
    module_type_id = django_filters.ModelMultipleChoiceFilter(
        queryset=ModuleType.objects.all(), label=_("Module type (ID)")
    )

    class Meta:
        model = Association
        fields = ("id", "revision")

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(device_type__model__icontains=value)
            | Q(module_type__model__icontains=value)
            | Q(revision__definition__key__icontains=value)
        )


@register_filterset
class BindingOverrideFilterSet(ChangeLoggedModelFilterSet):
    q = django_filters.CharFilter(method="search", label=_("Search"))
    device_type_id = django_filters.ModelMultipleChoiceFilter(
        queryset=DeviceType.objects.all(), label=_("Device type (ID)")
    )
    module_type_id = django_filters.ModelMultipleChoiceFilter(
        queryset=ModuleType.objects.all(), label=_("Module type (ID)")
    )
    device_id = django_filters.ModelMultipleChoiceFilter(
        queryset=Device.objects.all(), label=_("Device (ID)")
    )
    module_id = django_filters.ModelMultipleChoiceFilter(
        queryset=Module.objects.all(), label=_("Installed module (ID)")
    )
    definition_id = django_filters.ModelMultipleChoiceFilter(
        field_name="definition",
        queryset=VisualDefinition.objects.all(),
        label=_("Definition (ID)"),
    )

    class Meta:
        model = BindingOverride
        fields = ("id", "connector_key")

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(connector_key__icontains=value)
            | Q(definition__key__icontains=value)
            | Q(device_type__model__icontains=value)
            | Q(module_type__model__icontains=value)
            | Q(device__name__icontains=value)
            | Q(module__module_type__model__icontains=value)
        )


@register_filterset
class InstancePinFilterSet(ChangeLoggedModelFilterSet):
    q = django_filters.CharFilter(method="search", label=_("Search"))
    device_id = django_filters.ModelMultipleChoiceFilter(
        queryset=Device.objects.all(), label=_("Device (ID)")
    )
    module_id = django_filters.ModelMultipleChoiceFilter(
        queryset=Module.objects.all(), label=_("Installed module (ID)")
    )
    revision_id = django_filters.ModelMultipleChoiceFilter(
        field_name="revision",
        queryset=DefinitionRevision.objects.all(),
        label=_("Revision (ID)"),
    )
    definition_id = django_filters.ModelMultipleChoiceFilter(
        field_name="revision__definition",
        queryset=VisualDefinition.objects.all(),
        label=_("Definition (ID)"),
    )
    definition = django_filters.ModelMultipleChoiceFilter(
        field_name="revision__definition__key",
        queryset=VisualDefinition.objects.all(),
        to_field_name="key",
        label=_("Definition key"),
    )

    class Meta:
        model = InstancePin
        fields = ("id",)

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(device__name__icontains=value)
            | Q(module__device__name__icontains=value)
            | Q(module__module_type__model__icontains=value)
            | Q(revision__definition__key__icontains=value)
            | Q(revision__digest__istartswith=value)
        )
