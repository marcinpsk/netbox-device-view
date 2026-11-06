from django.urls import path
from netbox.api.routers import NetBoxRouter

from . import views

app_name = "netbox_device_view"

router = NetBoxRouter()
router.register("device-view", views.DeviceViewViewSet)
router.register("artwork-definitions", views.VisualDefinitionViewSet)
router.register("artwork-revisions", views.DefinitionRevisionViewSet)
router.register("binding-overrides", views.BindingOverrideViewSet)
router.register("instance-pins", views.InstancePinViewSet)

urlpatterns = router.urls + [
    path(
        "devices/<int:pk>/rendered-layout/",
        views.DeviceRenderedLayoutView.as_view(),
        name="device_rendered_layout",
    ),
    path(
        "devices/<int:pk>/artwork/",
        views.DeviceArtworkView.as_view(),
        name="device_artwork",
    ),
    path(
        "artwork-bundles/",
        views.ArtworkBundleUploadView.as_view(),
        name="artwork_bundle_upload",
    ),
    path(
        "artwork-source-imports/",
        views.ArtworkSourceImportView.as_view(),
        name="artwork_source_import",
    ),
]
