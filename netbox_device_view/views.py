from core.models import DataSource
from dcim.models import Device
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import router, transaction
from django.db.models import Count, OuterRef, Subquery
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.http import urlencode
from django.utils.text import capfirst
from django.utils.translation import gettext_lazy as _
from django.views import View
from netbox.context import current_request
from netbox.object_actions import (
    AddObject,
    BulkDelete,
    BulkExport,
    BulkImport,
    DeleteObject,
    EditObject,
    ObjectAction,
)
from netbox.views import generic
from netbox.views.generic import BulkImportView
from utilities.paginator import EnhancedPaginator, get_paginate_count
from utilities.views import (
    ContentTypePermissionRequiredMixin,
    GetReturnURLMixin,
    ViewTab,
    register_model_view,
)

from netbox_device_view.forms import DeviceViewImportForm

from . import datasource_import, filtersets, forms, models, tables
from .artwork import importing as artwork_importing
from .artwork import overrides, review
from .artwork.errors import ArtworkError
from .artwork.type_lock import lock_bulk_writes
from .components import binding_labels
from .utils import (
    device_view_context,
    get_stylenames_for_device_type,
    has_device_view,
)


class DeviceViewView(generic.ObjectView):
    queryset = models.DeviceView.objects


class DataSourceImport(ObjectAction):
    name = "datasource_import"
    label = _("Import from Data Source")
    template_name = "netbox_device_view/buttons/datasource_import.html"

    @classmethod
    def render(cls, context, obj, **kwargs):
        # permissions_required needs every permission; the import needs add or change.
        if not datasource_import.can_import(context["request"].user):
            return ""
        return super().render(context, obj, **kwargs)


class DeviceViewListView(generic.ObjectListView):
    queryset = models.DeviceView.objects.annotate(
        device_count=Count("device_type__instances")
    )
    table = tables.DeviceViewTable
    filterset = filtersets.DeviceViewFilterSet
    filterset_form = forms.DeviceViewFilterForm
    actions = (
        AddObject,
        BulkImport,
        DataSourceImport,
        BulkExport,
    )


class DeviceViewEditView(generic.ObjectEditView):
    queryset = models.DeviceView.objects
    form = forms.DeviceViewForm
    template_name = "netbox_device_view/deviceview_edit.html"


# The largest PostgreSQL bigint; a larger value makes the id comparison skip the index.
MAX_PK = 2**63 - 1


def _parse_pk(value):
    """Return value as a primary key, or None; isdigit() alone also accepts "²"."""
    if (
        value.isascii()
        and value.isdigit()
        and len(value) <= 19
        and int(value) <= MAX_PK
    ):
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


def _selected_datafiles(user, source_form, data):
    """Return the selected catalog DataFiles; raise ValidationError for any other selection."""
    values = data.getlist("file")
    if len(values) > datasource_import.MAX_BATCH_ROWS:
        raise ValidationError(datasource_import.TOO_MANY_ROWS)
    file_ids = {pk for pk in map(_parse_pk, values) if pk is not None}
    datafiles = datasource_import.catalog(
        user, source_form.cleaned_data["source"], source_form.cleaned_data["prefix"]
    ).filter(pk__in=file_ids)
    if not values or len(file_ids) != len(values) or len(datafiles) != len(values):
        raise ValidationError(_("Select one or more files from the catalog."))
    return datafiles


class DataSourceImportPermissionMixin(ContentTypePermissionRequiredMixin):
    def has_permission(self):
        return datasource_import.can_import(self.request.user)


def _ticked_file_ids(request, source, prefix) -> set[int]:
    """Return the ids of the ticked files that are still in the catalog."""
    values = request.GET.getlist("file")
    if len(values) > datasource_import.MAX_BATCH_ROWS:
        messages.error(request, datasource_import.TOO_MANY_ROWS)
        return set()
    file_ids = {pk for pk in map(_parse_pk, values) if pk is not None}
    return set(
        datasource_import.catalog(request.user, source, prefix)
        .filter(pk__in=file_ids)
        .values_list("pk", flat=True)
    )


class DeviceViewDataSourceImportView(DataSourceImportPermissionMixin, View):
    """Select a Data Source and directory, then list the importable layout files."""

    def get(self, request):
        form = forms.DataSourceImportSourceForm(
            request.GET if "source" in request.GET else None, user=request.user
        )
        context = {"form": form}
        if form.is_bound and form.is_valid():
            source = form.cleaned_data["source"]
            prefix = form.cleaned_data["prefix"]
            search = form.cleaned_data["q"]
            paginator = EnhancedPaginator(
                datasource_import.catalog(request.user, source, prefix, search),
                get_paginate_count(request),
            )
            page = paginator.get_page(request.GET.get("page"))
            ticked = _ticked_file_ids(request, source, prefix)
            context["paginator"] = paginator
            context["page"] = page
            context["rows"] = datasource_import.catalog_rows(
                request.user, source, prefix, page.object_list
            )
            context["ticked"] = ticked
            context["ticked_elsewhere"] = sorted(
                ticked - {row.datafile.pk for row in context["rows"]}
            )
        return render(request, "netbox_device_view/datasource_import.html", context)

    def post(self, request):
        """Keep the ticked files when the user changes the page or the search."""
        form = forms.DataSourceImportSourceForm(request.POST, user=request.user)
        if not form.is_valid():
            return render(
                request,
                "netbox_device_view/datasource_import.html",
                {"form": form},
                status=400,
            )
        files = request.POST.getlist("file")
        if len(files) > datasource_import.MAX_BATCH_ROWS:
            messages.error(request, datasource_import.TOO_MANY_ROWS)
            files = []
        params = {
            "source": form.cleaned_data["source"].pk,
            "prefix": form.cleaned_data["prefix"],
            "q": form.cleaned_data["q"],
            "file": files,
        }
        for name in ("page", "per_page"):
            if name in request.POST:
                params[name] = request.POST[name]
        catalog = reverse("plugins:netbox_device_view:deviceview_datasource_import")
        return redirect(f"{catalog}?{urlencode(params, doseq=True)}")


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
        try:
            datafiles = _selected_datafiles(request.user, source_form, request.POST)
        except ValidationError as exc:
            messages.error(request, exc.message)
            return redirect(catalog_url)

        if request.POST.get("step") == "review":
            target_form = forms.DataSourceImportTargetForm(
                request.POST, user=request.user, datafiles=datafiles
            )
        else:
            target_form = forms.DataSourceImportTargetForm(
                user=request.user,
                datafiles=datafiles,
                catalog_rows=datasource_import.catalog_rows(
                    request.user, source, prefix, datafiles
                ),
            )
        review = None
        if target_form.is_bound and target_form.is_valid():
            try:
                review = datasource_import.review(
                    request.user, source, target_form.selections()
                )
            except datasource_import.ImportRejected as exc:
                return render(
                    request,
                    "netbox_device_view/datasource_import_result.html",
                    {
                        "error": str(exc),
                        "source_form": source_form,
                        "target_form": target_form,
                    },
                    status=400,
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


def _review_again_context(request):
    """Rebuild the review forms from a rejected apply POST; apply itself ignores them."""
    source_form = forms.DataSourceImportSourceForm(request.POST, user=request.user)
    if not source_form.is_valid():
        return {}
    try:
        datafiles = _selected_datafiles(request.user, source_form, request.POST)
    except ValidationError:
        return {}
    target_form = forms.DataSourceImportTargetForm(
        request.POST, user=request.user, datafiles=datafiles
    )
    return {"source_form": source_form, "target_form": target_form}


class DeviceViewDataSourceImportApplyView(DataSourceImportPermissionMixin, View):
    """Apply a reviewed import."""

    def post(self, request):
        try:
            result = datasource_import.apply(
                request.user, request.POST.get("token", "")
            )
        except datasource_import.ImportRejected as exc:
            return render(
                request,
                "netbox_device_view/datasource_import_result.html",
                {"error": str(exc), **_review_again_context(request)},
                status=400,
            )
        visible = set(
            models.DeviceView.objects.restrict(request.user, "view")
            .filter(pk__in=[device_view.pk for device_view in result.device_views])
            .values_list("pk", flat=True)
        )
        outcomes = (
            (_("Created"), result.created),
            (_("Updated"), result.updated),
            (_("Unchanged"), result.unchanged),
        )
        return render(
            request,
            "netbox_device_view/datasource_import_result.html",
            {
                "outcomes": [
                    (label, [(dv, dv.pk in visible) for dv in device_views])
                    for label, device_views in outcomes
                ]
            },
        )


class DeviceViewDeleteView(generic.ObjectDeleteView):
    queryset = models.DeviceView.objects


@register_model_view(Device, "deviceview", path="device-view")
class DeviceDeviceView(generic.ObjectView):
    # The tab needs view permission on the device; artwork needs no permission of its own.
    queryset = Device.objects
    template_name = "netbox_device_view/deviceview.html"

    tab = ViewTab(
        label="Device View",
        badge=lambda obj: int(has_device_view(obj, current_request.get().user)),
        hide_if_empty=True,
    )

    def get_extra_context(self, request, instance):
        cable_colors = request.GET.get("cable_colors", "off")
        context = device_view_context(
            instance, request.user, cable_colors=cable_colors == "on"
        )
        return {**(context or {}), "cable_colors": cable_colors}

    def get_object(self, **kwargs):
        return get_object_or_404(
            Device.objects.restrict(self.request.user, "view"), pk=kwargs.get("pk")
        )


class BundleImport(ObjectAction):
    name = "import"
    label = _("Import bundle")
    template_name = "netbox_device_view/buttons/artwork_import.html"
    permissions_required = frozenset({"add"})


class SourceImport(ObjectAction):
    name = "source_import"
    label = _("Import from Data Source")
    template_name = "netbox_device_view/buttons/artwork_import.html"
    permissions_required = frozenset({"add"})


def _first_import(field):
    return Subquery(
        models.RevisionImport.objects.filter(revision=OuterRef("pk"))
        .order_by("created", "pk")
        .values(field)[:1]
    )


class DefinitionRevisionListView(generic.ObjectListView):
    queryset = models.DefinitionRevision.objects.select_related("definition").annotate(
        imported_by=_first_import("user_name"), source_name=_first_import("source_name")
    )
    table = tables.DefinitionRevisionTable
    filterset = filtersets.DefinitionRevisionFilterSet
    actions = (BundleImport, SourceImport)


class ActivationAction(ObjectAction):
    """A button for a user who can add or change associations."""

    @classmethod
    def render(cls, context, obj, **kwargs):
        # permissions_required needs every permission; activation needs add or change.
        if not review.can_activate(context["request"].user):
            return ""
        return super().render(context, obj, **kwargs)


class ActivateRevision(ActivationAction):
    name = "activate"
    label = _("Activate")
    template_name = "netbox_device_view/buttons/activate.html"
    url_kwargs = ("pk",)


class DefinitionRevisionView(generic.ObjectView):
    queryset = models.DefinitionRevision.objects.select_related("definition__key_owner")
    actions = (ActivateRevision, DeleteObject)

    def get_extra_context(self, request, instance):
        return {
            "connectors": [
                {
                    "key": key,
                    "face": connector["face"],
                    "label": connector.get("label"),
                    "bindings": binding_labels(connector["bindings"]),
                }
                for key, connector in sorted(instance.manifest["connectors"].items())
            ],
            "files": instance.files.select_related("blob"),
            "imports": instance.imports.select_related("data_source"),
        }


class DefinitionRevisionDeleteView(generic.ObjectDeleteView):
    queryset = models.DefinitionRevision.objects.all()


class ActivationPermissionMixin(ContentTypePermissionRequiredMixin):
    def has_permission(self):
        return review.can_activate(self.request.user)


class ActivateRevisionView(ActivationPermissionMixin, View):
    """Select the types that a review activates a revision for; the form opens the review."""

    def get(self, request, pk):
        revision = get_object_or_404(
            models.DefinitionRevision.objects.restrict(
                request.user, "view"
            ).select_related("definition"),
            pk=pk,
        )
        return render(
            request,
            "netbox_device_view/artwork_activate.html",
            {
                "object": revision,
                "form": forms.ActivateRevisionForm(
                    user=request.user, revision=revision
                ),
            },
        )


class AssociationReviewView(ActivationPermissionMixin, GetReturnURLMixin, View):
    """Review the impact of a selection of (type, revision) pairs, and activate it."""

    template_name = "netbox_device_view/association_review.html"
    default_return_url = "plugins:netbox_device_view:association_list"

    def get(self, request):
        form = forms.ReviewForm(request.GET, user=request.user)
        context = {
            "form": form,
            "return_url": self.get_return_url(request),
            "hidden_message": review.HIDDEN,
        }
        if not form.is_valid():
            return render(request, self.template_name, context, status=400)
        if form.pairs:
            context["review"] = review.build(request.user, form.pairs)
        return render(request, self.template_name, context)

    def post(self, request):
        try:
            associations = review.apply(request.user, request.POST.get("token", ""))
        except review.ActivationRejected as exc:
            return render(
                request,
                self.template_name,
                {
                    "error": str(exc),
                    "return_url": self.get_return_url(request),
                    "review_url": request.get_full_path(),
                },
                status=409,
            )
        for association in associations:
            messages.success(
                request,
                _("{type} now shows {revision}.").format(
                    type=association.hardware_type.full_name,
                    revision=association.revision,
                ),
            )
        return redirect(self.get_return_url(request))


class ReviewUpdate(ActivationAction):
    name = "review_update"
    label = _("Review update")
    multi = True
    template_name = "netbox_device_view/buttons/review_update.html"


class AssociationReviewUpdateView(ActivationPermissionMixin, GetReturnURLMixin, View):
    """Open the review of the selected associations, each with the newest revision of its definition."""

    default_return_url = "plugins:netbox_device_view:association_list"

    def post(self, request):
        queryset = models.Association.objects.restrict(
            request.user, "view"
        ).select_related("device_type", "module_type", "revision")
        if request.POST.get("_all"):
            queryset = filtersets.AssociationFilterSet(
                request.GET, queryset, request=request
            ).qs
        else:
            pks = {
                pk
                for pk in map(_parse_pk, request.POST.getlist("pk"))
                if pk is not None
            }
            queryset = queryset.filter(pk__in=pks)
        revisions = models.DefinitionRevision.objects.restrict(request.user, "view")
        pairs, current = [], []
        for association in queryset:
            # The ordering of revisions puts the newest of a definition first.
            newest = revisions.filter(
                definition=association.revision.definition_id
            ).first()
            if newest is None or newest.pk == association.revision_id:
                current.append(association.pk)
            else:
                pairs.append((association.hardware_type, newest))
        if not pairs and not current:
            messages.warning(request, _("Select one or more associations."))
            return redirect(self.get_return_url(request))
        return redirect(
            forms.review_url(
                pairs, current=current, return_url=self.get_return_url(request)
            )
        )


class RollBack(ObjectAction):
    name = "rollback"
    label = _("Roll back")
    template_name = "netbox_device_view/buttons/rollback.html"

    @classmethod
    def render(cls, context, obj, **kwargs):
        # AssociationView gives the link only to a user who can change the association.
        if not context.get("rollback_url"):
            return ""
        return super().render(context, obj, url=context["rollback_url"], **kwargs)


class AssociationListView(generic.ObjectListView):
    queryset = models.Association.objects.select_related(
        "device_type__manufacturer",
        "module_type__manufacturer",
        "revision__definition",
    )
    table = tables.AssociationTable
    filterset = filtersets.AssociationFilterSet
    filterset_form = forms.AssociationFilterForm
    actions = (ReviewUpdate,)


class AssociationView(generic.ObjectView):
    queryset = models.Association.objects.select_related(
        "device_type__manufacturer",
        "module_type__manufacturer",
        "revision__definition",
    )
    actions = (RollBack, DeleteObject)

    def get_extra_context(self, request, instance):
        previous = review.previous_revision(instance)
        if previous is None or not (
            models.DefinitionRevision.objects.restrict(request.user, "view")
            .filter(pk=previous.pk)
            .exists()
        ):
            return {"previous_revision": None, "rollback_url": None}
        rollback_url = None
        if (
            models.Association.objects.restrict(request.user, "change")
            .filter(pk=instance.pk)
            .exists()
        ):
            rollback_url = forms.review_url(
                [(instance.hardware_type, previous)],
                return_url=instance.get_absolute_url(),
            )
        return {"previous_revision": previous, "rollback_url": rollback_url}


class AssociationDeleteView(generic.ObjectDeleteView):
    queryset = models.Association.objects.all()


class VisualDefinitionListView(generic.ObjectListView):
    queryset = models.VisualDefinition.objects.select_related("key_owner").annotate(
        revision_count=Count("revisions")
    )
    table = tables.VisualDefinitionTable
    filterset = filtersets.VisualDefinitionFilterSet
    filterset_form = forms.VisualDefinitionFilterForm
    actions = ()


class VisualDefinitionView(generic.ObjectView):
    queryset = models.VisualDefinition.objects.select_related("key_owner")
    actions = (EditObject,)

    def get_extra_context(self, request, instance):
        return {"revisions": instance.revisions.restrict(request.user, "view")}


class VisualDefinitionEditView(generic.ObjectEditView):
    """Transfer the key: the form changes only the owner."""

    queryset = models.VisualDefinition.objects.all()
    form = forms.VisualDefinitionForm

    def post(self, request, *args, **kwargs):
        with transaction.atomic(using=router.db_for_write(models.VisualDefinition)):
            return super().post(request, *args, **kwargs)

    def get_object(self, **kwargs):
        if self.request.method == "POST":
            models.VisualDefinition.lock([kwargs["pk"]])
        return super().get_object(**kwargs)


class BindingOverrideListView(generic.ObjectListView):
    queryset = models.BindingOverride.objects.select_related(*models.OVERRIDE_RELATIONS)
    table = tables.BindingOverrideTable
    filterset = filtersets.BindingOverrideFilterSet
    filterset_form = forms.BindingOverrideFilterForm
    actions = (AddObject, BulkExport, BulkDelete)


class BindingOverrideView(generic.ObjectView):
    queryset = models.BindingOverride.objects.select_related(*models.OVERRIDE_RELATIONS)
    actions = (EditObject, DeleteObject)

    def get_extra_context(self, request, instance):
        field = instance._meta.get_field(instance.scope_field)
        return {
            "scope_type": capfirst(field.verbose_name),
            "binding_labels": binding_labels(instance.bindings),
        }


class BindingOverrideEditView(generic.ObjectEditView):
    queryset = models.BindingOverride.objects.select_related(*models.OVERRIDE_RELATIONS)
    form = forms.BindingOverrideForm
    template_name = "netbox_device_view/bindingoverride_edit.html"

    def get_object(self, **kwargs):
        obj = super().get_object(**kwargs)
        # The checks of an override name its scope, so a hidden scope hides the override.
        if obj.pk and not overrides.can_view_scope(self.request.user, obj.scope):
            raise Http404
        return obj


class BindingOverrideDeleteView(generic.ObjectDeleteView):
    # BindingOverride.delete() takes the type lock.
    queryset = models.BindingOverride.objects.select_related(*models.OVERRIDE_RELATIONS)


class ScopedRowBulkDeleteView(generic.BulkDeleteView):
    """Delete several overrides or pins with the bulk lock of their writers."""

    def post(self, request, **kwargs):
        with models.write_transaction(self.queryset.model) as using:
            lock_bulk_writes(using)
            return super().post(request, **kwargs)


class BindingOverrideBulkDeleteView(ScopedRowBulkDeleteView):
    queryset = models.BindingOverride.objects.select_related(*models.OVERRIDE_RELATIONS)
    filterset = filtersets.BindingOverrideFilterSet
    table = tables.BindingOverrideTable


class InstancePinListView(generic.ObjectListView):
    queryset = models.InstancePin.objects.select_related(*models.PIN_RELATIONS)
    table = tables.InstancePinTable
    filterset = filtersets.InstancePinFilterSet
    filterset_form = forms.InstancePinFilterForm
    actions = (AddObject, BulkExport, BulkDelete)


class InstancePinView(generic.ObjectView):
    queryset = models.InstancePin.objects.select_related(*models.PIN_RELATIONS)
    actions = (EditObject, DeleteObject)

    def get_extra_context(self, request, instance):
        field = instance._meta.get_field(instance.scope_field)
        return {"instance_type": capfirst(field.verbose_name)}


class InstancePinEditView(generic.ObjectEditView):
    queryset = models.InstancePin.objects.select_related(*models.PIN_RELATIONS)
    form = forms.InstancePinForm

    def get_object(self, **kwargs):
        obj = super().get_object(**kwargs)
        # The checks of a pin name its instance, so a hidden instance hides the pin.
        if obj.pk and not overrides.can_view_scope(self.request.user, obj.scope):
            raise Http404
        return obj


class InstancePinDeleteView(generic.ObjectDeleteView):
    # InstancePin.delete() takes the type lock.
    queryset = models.InstancePin.objects.select_related(*models.PIN_RELATIONS)


class InstancePinBulkDeleteView(ScopedRowBulkDeleteView):
    queryset = models.InstancePin.objects.select_related(*models.PIN_RELATIONS)
    filterset = filtersets.InstancePinFilterSet
    table = tables.InstancePinTable


class ArtworkBundleImportView(ContentTypePermissionRequiredMixin, View):
    """Upload an offline artwork bundle and import its definitions as candidate revisions."""

    template_name = "netbox_device_view/artwork_import.html"

    def get_required_permission(self):
        return artwork_importing.PERMISSION

    def get(self, request):
        return render(
            request, self.template_name, {"form": forms.ArtworkBundleImportForm()}
        )

    def post(self, request):
        form = forms.ArtworkBundleImportForm(request.POST, request.FILES)
        if not form.is_valid():
            return render(request, self.template_name, {"form": form}, status=400)
        upload = form.cleaned_data["bundle"]
        try:
            results = artwork_importing.import_bundle(request.user, upload.name, upload)
        except ArtworkError as exc:
            return render(
                request,
                self.template_name,
                {"form": form, "issues": exc.issues},
                status=400,
            )
        return render(request, self.template_name, {"form": form, "results": results})


class ArtworkSourceImportView(ContentTypePermissionRequiredMixin, View):
    """Import the definitions below a prefix of a synchronized Data Source as candidate revisions."""

    template_name = "netbox_device_view/artwork_source_import.html"

    def get_required_permission(self):
        return artwork_importing.PERMISSION

    def _render(self, request, form, status=200, **extra):
        pk = _parse_pk(str(form["data_source"].value() or ""))
        queryset = form.fields["data_source"].queryset
        source = queryset.filter(pk=pk).first() if pk is not None else None
        context = {
            "form": form,
            "source": source,
            "sync_run": (
                models.SourceSyncRun.objects.filter(source=source).first()
                if source
                else None
            ),
            "can_sync": source is not None
            and DataSource.objects.restrict(request.user, "sync")
            .filter(pk=source.pk)
            .exists(),
            "sync_url": source and self._sync_url(source, form),
            **extra,
        }
        return render(request, self.template_name, context, status=status)

    @staticmethod
    def _sync_url(source, form):
        """Return NetBox's own sync URL; it comes back to this page with the same choice."""
        here = urlencode(
            {"data_source": source.pk, "prefix": form["prefix"].value() or ""}
        )
        page = reverse("plugins:netbox_device_view:definitionrevision_source_import")
        back = urlencode({"return_url": f"{page}?{here}"})
        return f"{reverse('core:datasource_sync', kwargs={'pk': source.pk})}?{back}"

    def get(self, request):
        form = forms.ArtworkSourceImportForm(
            initial=request.GET.dict(), user=request.user
        )
        return self._render(request, form)

    def post(self, request):
        form = forms.ArtworkSourceImportForm(request.POST, user=request.user)
        if not form.is_valid():
            return self._render(request, form, status=400)
        try:
            results = artwork_importing.import_from_source(
                request.user,
                form.cleaned_data["data_source"],
                form.cleaned_data["prefix"],
            )
        except ArtworkError as exc:
            return self._render(request, form, status=400, issues=exc.issues)
        return self._render(request, form, results=results)
