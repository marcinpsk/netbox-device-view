from core.api.serializers import DataSourceSerializer
from core.models import DataSource
from dcim.api.serializers import (
    DeviceSerializer,
    DeviceTypeSerializer,
    ModuleSerializer,
    ModuleTypeSerializer,
)
from django.core.exceptions import ValidationError as DjangoValidationError
from netbox.api.serializers import (
    BaseModelSerializer,
    ChangeLogMessageSerializer,
    NetBoxModelSerializer,
    ValidatedModelSerializer,
)
from rest_framework import serializers
from rest_framework.fields import get_error_detail

from ..artwork import overrides
from ..models import (
    Association,
    BindingOverride,
    DefinitionRevision,
    DeviceView,
    RevisionFile,
    RevisionImport,
    VisualDefinition,
)


class DeviceViewSerializer(NetBoxModelSerializer):
    class Meta:
        model = DeviceView
        fields = (
            "id",
            "display",
            "device_type",
            "grid_template_area",
            "yaml_layout",
            "render_mode",
            "tags",
            "custom_fields",
            "created",
            "last_updated",
        )


class RevisionFileSerializer(serializers.ModelSerializer):
    digest = serializers.CharField(source="blob.digest", read_only=True)
    size = serializers.IntegerField(source="blob.size", read_only=True)

    class Meta:
        model = RevisionFile
        fields = ("path", "digest", "size")


class RevisionImportSerializer(serializers.ModelSerializer):
    data_source = DataSourceSerializer(nested=True, read_only=True)

    class Meta:
        model = RevisionImport
        fields = (
            "created",
            "user_name",
            "source_name",
            "data_source",
            "last_synced",
            "path",
        )


class DefinitionRevisionSerializer(BaseModelSerializer):
    key = serializers.CharField(source="definition.key", read_only=True)
    kind = serializers.CharField(source="definition.kind", read_only=True)
    files = RevisionFileSerializer(many=True, read_only=True)
    imports = RevisionImportSerializer(many=True, read_only=True)

    class Meta:
        model = DefinitionRevision
        fields = (
            "id",
            "url",
            "display_url",
            "display",
            "key",
            "kind",
            "digest",
            "manifest",
            "files",
            "imports",
            "created",
        )
        brief_fields = ("id", "url", "display", "key", "digest")
        read_only_fields = fields


class VisualDefinitionSerializer(ChangeLogMessageSerializer, ValidatedModelSerializer):
    key_owner = DataSourceSerializer(nested=True, allow_null=True)

    class Meta:
        model = VisualDefinition
        fields = (
            "id",
            "url",
            "display_url",
            "display",
            "key",
            "kind",
            "key_owner",
            "created",
            "last_updated",
        )
        brief_fields = ("id", "url", "display", "key")
        read_only_fields = ("key", "kind")

    def validate_key_owner(self, source):
        # The nested lookup in NetBox 4.5 does not restrict the Data Source to the user.
        user = self.context["request"].user
        if (
            source
            and not DataSource.objects.restrict(user, "view")
            .filter(pk=source.pk)
            .exists()
        ):
            raise serializers.ValidationError("Select a Data Source that you can view.")
        return source


class AssociationSerializer(BaseModelSerializer):
    """The change events of an association; activation has no API."""

    device_type = DeviceTypeSerializer(nested=True, read_only=True)
    module_type = ModuleTypeSerializer(nested=True, read_only=True)
    revision = DefinitionRevisionSerializer(nested=True, read_only=True)

    class Meta:
        model = Association
        fields = (
            "id",
            "display",
            "device_type",
            "module_type",
            "revision",
            "created",
            "last_updated",
        )
        brief_fields = ("id", "display", "revision")
        read_only_fields = fields


class BindingOverrideSerializer(ChangeLogMessageSerializer, ValidatedModelSerializer):
    """Each write goes through the override writer, as on the plugin pages."""

    device_type = DeviceTypeSerializer(nested=True, required=False, allow_null=True)
    module_type = ModuleTypeSerializer(nested=True, required=False, allow_null=True)
    device = DeviceSerializer(nested=True, required=False, allow_null=True)
    module = ModuleSerializer(nested=True, required=False, allow_null=True)
    definition = VisualDefinitionSerializer(nested=True, read_only=True)

    class Meta:
        model = BindingOverride
        fields = (
            "id",
            "url",
            "display_url",
            "display",
            "device_type",
            "module_type",
            "device",
            "module",
            "definition",
            "connector_key",
            "bindings",
            "created",
            "last_updated",
        )
        brief_fields = ("id", "url", "display", "connector_key")

    def create(self, validated_data):
        return self._save(BindingOverride(**validated_data))

    def update(self, instance, validated_data):
        validated_data.pop("changelog_message", None)
        for name, value in validated_data.items():
            setattr(instance, name, value)
        return self._save(instance)

    def _save(self, override):
        try:
            return overrides.save(self.context["request"].user, override)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(get_error_detail(exc)) from exc
