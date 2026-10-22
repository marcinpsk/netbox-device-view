from typing import ClassVar

from dcim.models import Device
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.http import urlencode
from django.utils.translation import gettext_lazy as _
from django.views import View
from netbox.object_actions import (
    AddObject,
    BulkDelete,
    BulkEdit,
    BulkExport,
    BulkImport,
    BulkRename,
    ObjectAction,
)
from netbox.views import generic
from netbox.views.generic import BulkImportView
from utilities.paginator import EnhancedPaginator, get_paginate_count
from utilities.views import (
    ContentTypePermissionRequiredMixin,
    ViewTab,
    register_model_view,
)

from netbox_device_view.forms import DeviceViewImportForm

from . import datasource_import, forms, models, tables
from .utils import (
    active_panel_key,
    device_height_px,
    get_stylenames_for_device_type,
    prepare,
    prepare_svg,
)


class DeviceViewView(generic.ObjectView):
    queryset = models.DeviceView.objects


class DataSourceImport(ObjectAction):
    name = "datasource_import"
    label = _("Import from Data Source")
    permissions_required: ClassVar[set[str]] = {"add"}
    template_name = "netbox_device_view/buttons/datasource_import.html"


class DeviceViewListView(generic.ObjectListView):
    queryset = models.DeviceView.objects.annotate(
        device_count=Count("device_type__instances")
    )
    table = tables.DeviceViewTable
    actions = (
        AddObject,
        BulkImport,
        DataSourceImport,
        BulkExport,
        BulkEdit,
        BulkRename,
        BulkDelete,
    )


class DeviceViewEditView(generic.ObjectEditView):
    queryset = models.DeviceView.objects
    form = forms.DeviceViewForm
    template_name = "netbox_device_view/deviceview_edit.html"


def _parse_pk(value):
    """Return value as a primary key, or None; isdigit() alone also accepts "²"."""
    if value.isascii() and value.isdigit() and len(value) <= 19:
        return int(value)
    return None


@method_decorator(login_required, name="dispatch")
class DeviceTypeInterfacesView(View):
    """Return JSON list of component templates with their CSS stylenames for
    the given device type.  Used by the Add/Edit form to preview grid-area
    names before saving."""

    def get(self, request):
        device_type_id = request.GET.get("device_type_id", "").strip()
        device_type_pk = _parse_pk(device_type_id)
        if device_type_pk is None:
            return JsonResponse([], safe=False)
        return JsonResponse(get_stylenames_for_device_type(device_type_pk), safe=False)


class DeviceViewBulkImportView(BulkImportView):
    queryset = models.DeviceView.objects.all()
    model_form = DeviceViewImportForm


class DataSourceImportPermissionMixin(ContentTypePermissionRequiredMixin):
    def get_required_permission(self):
        return "netbox_device_view.add_deviceview"


class DeviceViewDataSourceImportView(DataSourceImportPermissionMixin, View):
    """Select a Data Source and directory, then list the importable layout files."""

    def get(self, request):
        form = forms.DataSourceImportSourceForm(
            request.GET if "source" in request.GET else None, user=request.user
        )
        context = {"form": form}
        if form.is_bound and form.is_valid():
            paginator = EnhancedPaginator(
                datasource_import.catalog(
                    request.user,
                    form.cleaned_data["source"],
                    form.cleaned_data["prefix"],
                ),
                get_paginate_count(request),
            )
            context["paginator"] = paginator
            context["page"] = paginator.get_page(request.GET.get("page"))
        return render(request, "netbox_device_view/datasource_import.html", context)


class DeviceViewDataSourceImportReviewView(DataSourceImportPermissionMixin, View):
    """Assign device types to the selected files and review the proposed records."""

    def post(self, request):
        source_form = forms.DataSourceImportSourceForm(request.POST, user=request.user)
        if not source_form.is_valid():
            return render(
                request,
                "netbox_device_view/datasource_import.html",
                {"form": source_form},
                status=400,
            )
        source = source_form.cleaned_data["source"]
        prefix = source_form.cleaned_data["prefix"]
        catalog_url = (
            reverse("plugins:netbox_device_view:deviceview_datasource_import")
            + "?"
            + urlencode({"source": source.pk, "prefix": prefix})
        )
        values = request.POST.getlist("file")
        file_ids = {pk for pk in map(_parse_pk, values) if pk is not None}
        datafiles = datasource_import.catalog(request.user, source, prefix).filter(
            pk__in=file_ids
        )
        if not values or len(file_ids) != len(values) or len(datafiles) != len(values):
            messages.error(request, _("Select one or more files from the catalog."))
            return redirect(catalog_url)

        target_form = forms.DataSourceImportTargetForm(
            request.POST if request.POST.get("step") == "review" else None,
            user=request.user,
            datafiles=datafiles,
        )
        review = None
        if target_form.is_bound and target_form.is_valid():
            review = datasource_import.review(
                request.user, source, target_form.selections()
            )
        return render(
            request,
            "netbox_device_view/datasource_import_review.html",
            {
                "source_form": source_form,
                "target_form": target_form,
                "review": review,
                "catalog_url": catalog_url,
            },
        )


class DeviceViewDataSourceImportApplyView(DataSourceImportPermissionMixin, View):
    """Apply a reviewed import."""

    def post(self, request):
        try:
            created = datasource_import.apply(
                request.user, request.POST.get("token", "")
            )
        except datasource_import.ImportRejected as exc:
            return render(
                request,
                "netbox_device_view/datasource_import_result.html",
                {"error": str(exc)},
                status=400,
            )
        return render(
            request,
            "netbox_device_view/datasource_import_result.html",
            {"created": created},
        )


class DeviceViewDeleteView(generic.ObjectDeleteView):
    queryset = models.DeviceView.objects


@register_model_view(Device, "deviceview", path="device-view")
class DeviceDeviceView(generic.ObjectView):
    queryset = models.DeviceView.objects

    tab = ViewTab(
        label="Device View",
        badge=lambda obj: models.DeviceView.objects.filter(
            device_type=obj.device_type
        ).count(),
        hide_if_empty=True,
    )

    def get_extra_context(self, request, instance):
        height = device_height_px(instance.device_type)

        # Determine render mode from the device view record (if it exists)
        try:
            device_view_check = models.DeviceView.objects.get(
                device_type=instance.device_type
            )
            use_svg = device_view_check.use_svg
        except models.DeviceView.DoesNotExist:
            use_svg = False

        # Initialise all context variables to safe defaults
        dv = None
        svg_views = None
        modules = None
        ports_chassis = None
        device_view = None

        if use_svg:
            svg_views, modules, ports_chassis, device_view = prepare_svg(instance)
            if svg_views is None:
                # SVG preparation failed (e.g. virtual chassis) — fall back to CSS
                use_svg = False

        if not use_svg:
            dv, modules, ports_chassis, device_view = prepare(instance)

        # Build an ordered list of (pos, svg_markup).
        # For standard devices svg_views is keyed by device name matching
        # ports_chassis.  For patch panels ports are split into "Front"/"Rear"
        # keys that don't exist in svg_views, so we fall back to rendering all
        # svg_views entries directly (the SVG already contains both faces).
        svg_panels = []
        if use_svg and svg_views and ports_chassis:
            for pos in ports_chassis:
                if pos in svg_views:
                    svg_panels.append((pos, svg_views[pos]))
            # Fallback: no panel matched (e.g. patch panel with Front/Rear port keys)
            # — render all svg_views entries once
            if not svg_panels:
                for pos, markup in svg_views.items():
                    svg_panels.append((pos, markup))

        return {
            "device_view": device_view,
            "dv": dv,
            "svg_views": svg_views,
            "svg_panels": svg_panels,
            "use_svg": use_svg,
            "modules": modules,
            "height": height,
            "ports_chassis": ports_chassis,
            "cable_colors": request.GET.get("cable_colors", "off"),
            "active_panel": active_panel_key(instance),
        }

    def get_object(self, **kwargs):
        return Device.objects.get(pk=kwargs.get("pk"))
