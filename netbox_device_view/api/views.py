from dcim.models import Device
from netbox.api.authentication import TokenPermissions
from netbox.api.viewsets import NetBoxModelViewSet, NetBoxReadOnlyModelViewSet
from rest_framework import mixins, status
from rest_framework.generics import RetrieveAPIView
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .. import models
from ..artwork import importing
from ..artwork.errors import ArtworkError, Issue
from ..filtersets import DefinitionRevisionFilterSet, DeviceViewFilterSet
from .rendered_layout import compose_rendered_layout
from .serializers import DefinitionRevisionSerializer, DeviceViewSerializer


class DeviceViewViewSet(NetBoxModelViewSet):
    queryset = models.DeviceView.objects.prefetch_related("tags")
    serializer_class = DeviceViewSerializer
    filterset_class = DeviceViewFilterSet


class DeviceRenderedLayoutView(RetrieveAPIView):
    """
    GET /api/plugins/device_view/devices/<pk>/rendered-layout/

    Read-only, versioned JSON composition of a device's rendered layout —
    SVG structure plus hotspot geometry and connection state, keyed by
    NetBox object — for API clients that can't (or shouldn't) reimplement
    this plugin's YAML/SVG rendering pipeline. See
    netbox_device_view.api.rendered_layout for the composition logic and
    docs/rendered-layout-api.md for the response schema.

    Uses the same permission/authentication stack as the rest of the
    plugin's API (DRF's default TokenPermissions, configured globally by
    NetBox): a 403 for users without ``dcim.view_device`` at all, a normal
    404 for a device that doesn't exist *or* is outside the requesting
    user's object-level permission scope (``get_queryset`` applies
    ``.restrict()`` — the same object-level filtering NetBoxModelViewSet
    uses — so an out-of-scope device is indistinguishable from a missing
    one, matching NetBox's own API behaviour elsewhere).

    No serializer_class: the response is a composed dict, not a model
    representation, so it's returned directly rather than forced through
    ModelSerializer machinery that doesn't fit its shape.
    """

    queryset = Device.objects.all()

    def get_queryset(self):
        return super().get_queryset().restrict(self.request.user, "view")

    def get(self, request, *args, **kwargs):
        device = self.get_object()
        return Response(compose_rendered_layout(device, request.user))


class DefinitionRevisionViewSet(mixins.DestroyModelMixin, NetBoxReadOnlyModelViewSet):
    """Read and delete definition revisions; a stored revision cannot be changed."""

    queryset = models.DefinitionRevision.objects.select_related(
        "definition"
    ).prefetch_related("files__blob", "imports")
    serializer_class = DefinitionRevisionSerializer
    filterset_class = DefinitionRevisionFilterSet


class ArtworkBundleUploadView(APIView):
    """
    POST /api/plugins/device_view/artwork-bundles/ (multipart, field "bundle")

    Import an offline bundle with the same validation and import code as the UI.
    """

    parser_classes = (MultiPartParser,)
    # TokenPermissions maps POST to the add permission of the queryset model.
    permission_classes = (TokenPermissions,)
    queryset = models.DefinitionRevision.objects.all()

    def post(self, request):
        upload = request.FILES.get("bundle")
        try:
            if upload is None:
                raise ArtworkError(
                    [Issue("bundle", None, "Upload a .zip file as bundle.")]
                )
            results = importing.import_bundle(request.user, upload.name, upload)
        except ArtworkError as exc:
            return Response(
                {"errors": [vars(issue) for issue in exc.issues]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        visible = set(
            models.DefinitionRevision.objects.restrict(request.user, "view")
            .filter(pk__in=[result.revision.pk for result in results])
            .values_list("pk", flat=True)
        )
        context = {"request": request}
        body = {
            "revisions": [
                {
                    "created": result.created,
                    # Without view permission, a known revision shows no earlier provenance.
                    "revision": (
                        DefinitionRevisionSerializer(
                            result.revision, context=context
                        ).data
                        if result.revision.pk in visible
                        else {
                            "key": result.revision.definition.key,
                            "digest": result.revision.digest,
                        }
                    ),
                }
                for result in results
            ]
        }
        created = any(result.created for result in results)
        return Response(
            body, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK
        )
