from core.api.serializers import DataSourceSerializer
from core.models import DataSource
from netbox.api.serializers import (
    BaseModelSerializer,
    ChangeLogMessageSerializer,
    NetBoxModelSerializer,
    ValidatedModelSerializer,
)
from rest_framework import serializers

from ..models import (
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
