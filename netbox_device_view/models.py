import functools
import operator
from contextlib import contextmanager

from core.signals import clear_events
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS, models, router, transaction
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from netbox.models import ChangeLoggedModel, NetBoxModel

from .artwork.manifest import DEVICE_KIND, FACES, MODULE_KIND, binding_issues
from .artwork.manifest import SCHEMA as MANIFEST_SCHEMA
from .artwork.type_lock import lock_types
from .layout.parser import LayoutParseError, parse


class RenderMode(models.TextChoices):
    CSS = "css", "CSS Grid (default)"
    SVG = "svg", "SVG"


class DeviceView(NetBoxModel):
    device_type = models.OneToOneField(
        to="dcim.DeviceType",
        on_delete=models.PROTECT,
        related_name="+",
    )

    grid_template_area = models.TextField(blank=False)

    yaml_layout = models.TextField(
        blank=True,
        default="",
        help_text=(
            "YAML-based layout definition. When provided, this takes precedence over "
            "the legacy CSS grid_template_area field for rendering. "
            "See the documentation for the schema reference."
        ),
    )

    render_mode = models.CharField(
        max_length=10,
        choices=RenderMode.choices,
        default=RenderMode.CSS,
        help_text=(
            "Rendering engine to use for this device view. "
            "'CSS Grid' uses the existing HTML+CSS approach and works with both YAML and legacy CSS layouts. "
            "'SVG' produces a scalable vector graphic and requires a YAML layout."
        ),
    )

    class Meta:
        ordering = ("device_type",)

    def __str__(self):
        return f"{self.device_type.manufacturer} {self.device_type.model}"

    def clean_fields(self, exclude=None):
        # A field-level check, so full_clean(exclude=["yaml_layout"]) skips the parse.
        errors = {}
        try:
            super().clean_fields(exclude=exclude)
        except ValidationError as exc:
            errors = exc.update_error_dict(errors)
        if "yaml_layout" not in (exclude or ()) and self.has_yaml_layout:
            try:
                parse(self.yaml_layout, strict=True)
            except LayoutParseError as exc:
                errors.setdefault("yaml_layout", []).append(str(exc))
        if errors:
            raise ValidationError(errors)

    def get_render_mode_color(self):
        return {"css": "secondary", "svg": "info"}.get(self.render_mode, "secondary")

    @property
    def has_yaml_layout(self):
        """Return True if a YAML layout is defined for this device view."""
        return bool(self.yaml_layout and self.yaml_layout.strip())

    @property
    def use_svg(self):
        """Return True when SVG rendering is selected AND a YAML layout is available."""
        return self.render_mode == RenderMode.SVG and self.has_yaml_layout


class ImmutableModelError(TypeError):
    """An attempt to change a stored artwork record."""


class ImmutableMixin:
    """Refuse every save of a stored row; artwork records are written once."""

    def save(self, *args, **kwargs):
        if (
            not self._state.adding
            or kwargs.get("force_update")
            or kwargs.get("update_fields") is not None
        ):
            raise ImmutableModelError(
                f"A stored {self._meta.verbose_name} cannot be changed."
            )
        # A new record with the primary key of a stored row fails instead of updating it.
        kwargs["force_insert"] = True
        super().save(*args, **kwargs)


class VisualDefinition(ChangeLoggedModel):
    """The stable definition key that all revisions of one definition share."""

    # The source that a null key_owner stands for.
    OFFLINE_UPLOADS = _("offline uploads")

    key = models.CharField(
        max_length=MANIFEST_SCHEMA["properties"]["key"]["maxLength"], unique=True
    )
    kind = models.CharField(
        max_length=10,
        choices=[
            (kind, kind.title())
            for kind in MANIFEST_SCHEMA["properties"]["kind"]["enum"]
        ],
    )
    # The only source that can import the key; null means the offline uploads.
    key_owner = models.ForeignKey(
        to="core.DataSource",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        ordering = ("key",)

    def __str__(self):
        return self.key

    def get_absolute_url(self):
        return reverse("plugins:netbox_device_view:visualdefinition", args=[self.pk])

    @classmethod
    def lock(cls, pks) -> None:
        """Wait for concurrent changes of the definitions; a later query reads their result."""
        # NetBox restricts a queryset with a pk IN subquery, which a lock wait does not read again.
        list(cls.objects.select_for_update().filter(pk__in=pks).values_list("pk"))


class DefinitionRevision(ImmutableMixin, ChangeLoggedModel):
    """An immutable edition of a visual definition, identified by its content digest."""

    definition = models.ForeignKey(
        to=VisualDefinition, on_delete=models.PROTECT, related_name="revisions"
    )
    digest = models.CharField(max_length=64, unique=True)
    manifest = models.JSONField()

    class Meta:
        ordering = ("definition", "-created", "pk")
        verbose_name = "definition revision"

    def __str__(self):
        return f"{self.definition.key} @ {self.digest[:12]}"

    def get_absolute_url(self):
        return reverse("plugins:netbox_device_view:definitionrevision", args=[self.pk])

    @property
    def face_names(self):
        """The faces of the revision in physical order; JSON storage does not keep key order."""
        return [face for face in FACES if face in self.manifest["faces"]]


class Association(ChangeLoggedModel):
    """The explicit link of one device type or module type to its active definition revision."""

    device_type = models.OneToOneField(
        to="dcim.DeviceType",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="+",
    )
    module_type = models.OneToOneField(
        to="dcim.ModuleType",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="+",
    )
    revision = models.ForeignKey(
        to=DefinitionRevision, on_delete=models.PROTECT, related_name="associations"
    )

    class Meta:
        ordering = ("pk",)
        verbose_name = "artwork association"
        constraints = (
            models.CheckConstraint(
                condition=models.Q(device_type__isnull=False, module_type__isnull=True)
                | models.Q(device_type__isnull=True, module_type__isnull=False),
                name="%(app_label)s_association_one_type",
            ),
        )

    def __str__(self):
        return f"{self.hardware_type.full_name} → {self.revision}"

    def get_absolute_url(self):
        return reverse("plugins:netbox_device_view:association", args=[self.pk])

    @property
    def hardware_type(self):
        return self.device_type or self.module_type

    @property
    def kind(self):
        """The definition kind that the hardware type needs."""
        return DEVICE_KIND if self.device_type_id is not None else MODULE_KIND

    def clean(self):
        super().clean()
        if self.revision_id is None:
            return
        if self.revision.definition.kind != self.kind:
            raise ValidationError(
                {
                    "revision": _(
                        "The revision is a {revision} definition. A {kind} type needs a {kind} definition."
                    ).format(revision=self.revision.definition.kind, kind=self.kind)
                }
            )


SCOPE_FIELDS = ("device_type", "module_type", "device", "module")
ONE_SCOPE = _(
    "Select one scope: a device type, a module type, a device or an installed module."
)


def _only_scope(field):
    return models.Q(**{f"{name}__isnull": name != field for name in SCOPE_FIELDS})


class BindingOverride(ChangeLoggedModel):
    """A local replacement of the bindings of one connector key, for one scope and definition key."""

    device_type = models.ForeignKey(
        to="dcim.DeviceType",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="+",
    )
    module_type = models.ForeignKey(
        to="dcim.ModuleType",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="+",
    )
    device = models.ForeignKey(
        to="dcim.Device",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="+",
    )
    module = models.ForeignKey(
        to="dcim.Module",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("installed module"),
    )
    # clean() sets it from the active association of the scope's type.
    definition = models.ForeignKey(
        to=VisualDefinition,
        on_delete=models.PROTECT,
        blank=True,
        related_name="binding_overrides",
    )
    # A key that matches the connector key pattern of the manifest schema is at most 64 characters.
    connector_key = models.CharField(max_length=64)
    # The replacement binding list, with the schema of the bindings of a manifest connector.
    bindings = models.JSONField(default=list, blank=True)

    class Meta:
        # Bulk writers lock the types in this order, so two bulk writes cannot deadlock.
        ordering = (
            Coalesce("device_type", "device__device_type").asc(nulls_last=True),
            Coalesce("module_type", "module__module_type").asc(nulls_last=True),
            "connector_key",
            "pk",
        )
        verbose_name = "binding override"
        constraints = (
            models.CheckConstraint(
                condition=functools.reduce(
                    operator.or_, (_only_scope(field) for field in SCOPE_FIELDS)
                ),
                name="%(app_label)s_bindingoverride_one_scope",
                violation_error_message=ONE_SCOPE,
            ),
            *(
                models.UniqueConstraint(
                    fields=(field, "definition", "connector_key"),
                    condition=models.Q(**{f"{field}__isnull": False}),
                    name=f"%(app_label)s_bindingoverride_unique_{field}",
                    violation_error_message=_(
                        "This scope already has an override of this connector."
                    ),
                )
                for field in SCOPE_FIELDS
            ),
        )

    def __str__(self):
        return f"{self.scope_label} · {self.connector_key}"

    def get_absolute_url(self):
        return reverse("plugins:netbox_device_view:bindingoverride", args=[self.pk])

    @property
    def scope_field(self) -> str | None:
        """Return the name of the one scope field that is set, or None."""
        names = [n for n in SCOPE_FIELDS if getattr(self, f"{n}_id") is not None]
        return names[0] if len(names) == 1 else None

    @property
    def scope(self):
        return getattr(self, self.scope_field)

    @property
    def scope_label(self) -> str:
        scope = self.scope
        if self.scope_field in ("device_type", "module_type"):
            return scope.full_name
        if self.scope_field == "module":
            return f"{scope.device} {scope}"
        return str(scope)

    @property
    def hardware_type(self):
        """Return the device type or module type that the override depends on."""
        scope = self.scope
        if self.scope_field == "device":
            return scope.device_type
        if self.scope_field == "module":
            return scope.module_type
        return scope

    @property
    def kind(self):
        """Return the definition kind of the scope."""
        return (
            MODULE_KIND
            if self.scope_field in ("module_type", "module")
            else DEVICE_KIND
        )

    def clean(self):
        super().clean()
        # The check constraint reports a missing or a second scope.
        if self.scope_field is None:
            return
        issues = binding_issues(self.bindings, self.kind)
        if issues:
            raise ValidationError({"bindings": issues})
        if not self._state.adding:
            self._check_identity()
        hardware_type = self.hardware_type
        type_field = "device_type" if self.kind == DEVICE_KIND else "module_type"
        association = (
            Association.objects.filter(**{type_field: hardware_type})
            .select_related("revision__definition")
            .first()
        )
        if association is None:
            raise ValidationError(
                {
                    self.scope_field: _(
                        "{type} has no active artwork. Activate a revision for it first."
                    ).format(type=hardware_type.full_name)
                }
            )
        revision = association.revision
        if self._state.adding:
            self.definition = revision.definition
        elif revision.definition_id != self.definition_id:
            raise ValidationError(
                _("The active artwork of {type} is {active}, not {key}.").format(
                    type=hardware_type.full_name,
                    active=revision.definition.key,
                    key=self.definition.key,
                )
            )
        if self.connector_key not in revision.manifest["connectors"]:
            raise ValidationError(
                {
                    "connector_key": _(
                        "The active revision of {key} has no connector {connector}."
                    ).format(key=revision.definition.key, connector=self.connector_key)
                }
            )

    def _check_identity(self):
        """An override is its scope, definition key and connector key; only its bindings change."""
        identity = (
            *(f"{n}_id" for n in SCOPE_FIELDS),
            "definition_id",
            "connector_key",
        )
        stored = BindingOverride.objects.filter(pk=self.pk).values(*identity).first()
        if stored is None:
            raise ValidationError(_("The override does not exist any more."))
        if any(getattr(self, n) != stored[n] for n in identity):
            raise ValidationError(
                _(
                    "The scope and the connector of an override cannot change. Add a new"
                    " override instead."
                )
            )

    @contextmanager
    def locked(self):
        """Hold the lock of the override's type in one transaction on main and on the routed connection."""
        # NetBox Branching routes the override to the branch and its ChangeDiff rows to main.
        try:
            with (
                transaction.atomic(using=DEFAULT_DB_ALIAS),
                transaction.atomic(using=router.db_for_write(BindingOverride)),
            ):
                lock_types([self.hardware_type], router.db_for_write(BindingOverride))
                yield
        except Exception:
            # The events of a write that rolls back must not reach event rules.
            clear_events.send(sender=BindingOverride)
            raise

    def delete(self, *args, **kwargs):
        with self.locked():
            return super().delete(*args, **kwargs)


class ArtworkBlob(ImmutableMixin, models.Model):
    """One content-addressed file in media storage."""

    digest = models.CharField(max_length=64, primary_key=True)
    size = models.PositiveIntegerField()
    extension = models.CharField(max_length=8)
    created = models.DateTimeField(auto_now_add=True)

    ROOT = "artwork"

    @property
    def name(self):
        return f"{self.ROOT}/{self.digest[:2]}/{self.digest}.{self.extension}"


class RevisionFile(ImmutableMixin, models.Model):
    """A file of a revision: its path in the definition directory and its blob."""

    revision = models.ForeignKey(
        to=DefinitionRevision, on_delete=models.CASCADE, related_name="files"
    )
    path = models.CharField(max_length=255)
    blob = models.ForeignKey(to=ArtworkBlob, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ("revision", "path")
        constraints = (
            models.UniqueConstraint(
                fields=("revision", "path"),
                name="%(app_label)s_revisionfile_unique_path",
            ),
        )


class RevisionImport(ImmutableMixin, models.Model):
    """Provenance: one import of a revision. It is not part of the revision identity."""

    revision = models.ForeignKey(
        to=DefinitionRevision, on_delete=models.CASCADE, related_name="imports"
    )
    created = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey(
        to=settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
    )
    user_name = models.CharField(max_length=150)
    # The bundle file name, or the name of the Data Source at import time.
    source_name = models.CharField(max_length=255)
    data_source = models.ForeignKey(
        to="core.DataSource",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    last_synced = models.DateTimeField(null=True, blank=True)
    path = models.CharField(max_length=255)

    class Meta:
        ordering = ("revision", "-created", "pk")


class SourceSyncRun(models.Model):
    """The latest guarded sync of a Data Source; capture needs it finished."""

    source = models.OneToOneField(
        to="core.DataSource", on_delete=models.CASCADE, related_name="+"
    )
    started = models.DateTimeField()
    finished = models.DateTimeField(null=True, blank=True)
