from netbox.api.serializers import BaseModelSerializer, NetBoxModelSerializer
from rest_framework import serializers

from ..models import DefinitionRevision, DeviceView, RevisionFile, RevisionImport


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
    class Meta:
        model = RevisionImport
        fields = ("created", "user_name", "source_name", "path")


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
