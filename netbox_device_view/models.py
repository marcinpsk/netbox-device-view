from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from netbox.models import ChangeLoggedModel, NetBoxModel

from .artwork.manifest import FACES
from .artwork.manifest import SCHEMA as MANIFEST_SCHEMA
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
                parse(self.yaml_layout)
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
        if not self._state.adding:
            raise ImmutableModelError(
                f"A stored {self._meta.verbose_name} cannot be changed."
            )
        super().save(*args, **kwargs)


class VisualDefinition(ChangeLoggedModel):
    """The stable definition key that all revisions of one definition share."""

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
