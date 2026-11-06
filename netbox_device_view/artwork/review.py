"""Review the impact of an activation, and activate a reviewed selection: the one writer of associations.

A review compares two states of each affected device. The active state
uses the pins, then the associations. The candidate state uses the pins,
then the selected revisions, then the associations. Each state composes
the device as rendering does. A signed token holds the selection and a
fingerprint of the impact set. Activation locks the dependency set, makes
the review again and writes every association of the selection, or none.
"""

import functools
import hashlib
import json
import operator
from collections import defaultdict
from dataclasses import dataclass, field

from core.choices import ObjectChangeActionChoices
from core.models import ObjectChange, ObjectType
from core.signals import clear_events
from dcim.models import Device, DeviceType, Module, ModuleBay, ModuleType
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.db import (
    DEFAULT_DB_ALIAS,
    IntegrityError,
    OperationalError,
    connections,
    router,
    transaction,
)
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from ..components import BINDABLE_MODELS, component_label
from ..details import FOUND, MISSING
from ..models import (
    OVERRIDE_RELATIONS,
    SCOPE_FIELDS,
    SCOPES,
    TYPE_SCOPES,
    Association,
    BindingOverride,
    DefinitionRevision,
    InstancePin,
    active_revisions,
    instance_scope,
    type_scope,
)
from .overrides import depending_on
from .render import ARTWORK, part_label, resolve_device, resolve_schematic
from .type_lock import lock_types

ADD_PERMISSION = "netbox_device_view.add_association"
CHANGE_PERMISSION = "netbox_device_view.change_association"
SIGNING_SALT = "netbox_device_view.artwork.review"
REVIEW_MAX_AGE_SECONDS = 15 * 60
MAX_PAIRS = 100
# The SQLSTATE of a PostgreSQL deadlock.
DEADLOCK = "40P01"

HIDDEN = _("Your permissions do not cover every affected object.")
EXPIRED = _("The review has expired. Review the activation again.")
INVALID_TOKEN = _("The review token is not valid.")
OTHER_USER = _("The review token belongs to a different user.")
OTHER_BRANCH = _("The review was made in another branch. Review the activation again.")
CHANGED = _(
    "The affected objects, the associations or your permissions changed after the"
    " review. Review the activation again."
)
CONCURRENT = _(
    "Another change of the affected objects ran at the same time. Review the"
    " activation again."
)
NOT_VIEWABLE = _("Select a type and a revision that you can view.")
NEEDS_ADD = _("A first activation needs the add permission on artwork associations.")
NEEDS_CHANGE = _("An activation needs the change permission on this association.")
# The reason of each blocker.
NOT_BOUND = _("The connector does not bind the component.")
NO_CONNECTOR = _("The candidate has no connector {key}.")
NOT_ARTWORK = _("The candidate does not draw the part with artwork.")
NOT_DRAWN = _("The candidate does not draw the installed module.")


class ActivationRejected(Exception):
    """The activation did not apply; the message says why."""


def can_activate(user) -> bool:
    """A first association needs add permission, a later one change permission."""
    return user.has_perm(ADD_PERMISSION) or user.has_perm(CHANGE_PERMISSION)


@dataclass(frozen=True)
class Pair:
    """A device type or a module type, and the revision that a selection activates for it."""

    hardware_type: DeviceType | ModuleType
    revision: DefinitionRevision

    @property
    def scope(self):
        return type_scope(self.hardware_type)

    @property
    def key(self) -> tuple[str, int]:
        """Return the key of the type in a selection: see active_revisions()."""
        return (self.scope.field, self.hardware_type.pk)


@dataclass(frozen=True)
class Blocker:
    """A binding or an installed module that resolves or draws now, and not under the candidate."""

    device: Device
    # The device or the installed module that has the connector, or the module that is not drawn.
    part: str
    # None for a module that is not drawn.
    connector: str | None
    component: str | None
    reason: str


@dataclass(frozen=True)
class Unresolved:
    """A binding that is missing under the candidate."""

    device: Device
    part: str
    connector: str
    component: str


@dataclass(frozen=True)
class ReviewWarning:
    """A warning of an installed module that the candidate adds, for example a bay size change."""

    device: Device
    message: str


@dataclass(frozen=True)
class Target:
    """One pair of the selection, with its association and the instances of its type."""

    pair: Pair
    # The association of the type, or None for a first activation.
    association: Association | None
    # The devices or installed modules of the type without a pin, which change.
    instances: tuple
    # The pinned devices or installed modules of the type, which do not change.
    pinned: tuple
    # The overrides that name a connector that the candidate does not have.
    overrides: tuple[BindingOverride, ...]
    # Why the user cannot activate the pair, or None.
    error: str | None

    @property
    def unchanged(self) -> bool:
        return (
            self.association is not None
            and self.association.revision_id == self.pair.revision.pk
        )

    @property
    def rows(self) -> list[tuple]:
        """Return (instance, label, device, pinned) for each instance of the type."""
        return [
            (i, part_label(i) if isinstance(i, Module) else str(i), _device(i), pin)
            for instances, pin in ((self.instances, False), (self.pinned, True))
            for i in instances
        ]


def _device(part) -> Device:
    return part.device if isinstance(part, Module) else part


@dataclass
class Review:
    """The impact of a selection. A hidden review holds no names and no counts."""

    hidden: bool
    # The device types and module types on the placement paths of the affected devices, and the targets.
    dependency_types: frozenset
    targets: tuple[Target, ...] = ()
    blockers: tuple[Blocker, ...] = ()
    unresolved: tuple[Unresolved, ...] = ()
    warnings: tuple[ReviewWarning, ...] = ()
    fingerprint: str | None = None
    token: str | None = field(default=None, compare=False)

    @property
    def blocked(self) -> bool:
        return bool(self.blockers) or any(target.overrides for target in self.targets)

    @property
    def permitted(self) -> bool:
        return all(target.error is None for target in self.targets)

    @property
    def changes(self) -> bool:
        return any(not target.unchanged for target in self.targets)

    @property
    def ready(self) -> bool:
        """Return True when the selection can be activated."""
        return not self.hidden and self.permitted and not self.blocked and self.changes


def build(user, pairs) -> Review:
    """Return the review of ``pairs``, with a token when the selection can be activated.

    The review writes nothing.
    """
    review = _inspect(user, pairs)
    if review.ready:
        review.token = signing.dumps(
            {
                "user": user.pk,
                # NetBox Branching gives each branch its own connection.
                "connection": router.db_for_write(Association),
                "pairs": [
                    [
                        pair.scope.field,
                        pair.hardware_type.pk,
                        pair.revision.pk,
                        pair.revision.digest,
                    ]
                    for pair in pairs
                ],
                "fingerprint": review.fingerprint,
            },
            salt=SIGNING_SALT,
        )
    return review


def apply(user, token) -> list[Association]:
    """Activate the reviewed selection of ``token`` in one transaction; return the changed associations.

    Raise ActivationRejected when the review is not valid any more, and
    PermissionDenied when the permissions of ``user`` do not cover a saved
    association. Nothing changes then.
    """
    pairs, fingerprint = _load(user, token)
    try:
        try:
            # NetBox Branching routes Association to the branch and its ChangeDiff rows to main.
            with (
                transaction.atomic(using=DEFAULT_DB_ALIAS),
                transaction.atomic(using=router.db_for_write(Association)),
            ):
                return _apply(user, pairs, fingerprint)
        except OperationalError as exc:
            if getattr(exc.__cause__, "sqlstate", None) != DEADLOCK:
                raise
            raise ActivationRejected(CONCURRENT) from exc
    except Exception:
        # The events of a write that rolls back must not reach event rules.
        clear_events.send(sender=Association)
        raise


def previous_revision(association) -> DefinitionRevision | None:
    """Return the revision before the last change of ``association`` while it exists, else None."""
    change = (
        ObjectChange.objects.filter(
            changed_object_type=ObjectType.objects.get_for_model(Association),
            changed_object_id=association.pk,
            action=ObjectChangeActionChoices.ACTION_UPDATE,
        )
        .order_by("-time", "-pk")
        .first()
    )
    if change is None or not change.prechange_data:
        return None
    return (
        DefinitionRevision.objects.filter(pk=change.prechange_data.get("revision"))
        .exclude(pk=association.revision_id)
        .select_related("definition")
        .first()
    )


def _load(user, token) -> tuple[list[Pair], str]:
    try:
        payload = signing.loads(
            token, salt=SIGNING_SALT, max_age=REVIEW_MAX_AGE_SECONDS
        )
    except signing.SignatureExpired as exc:
        raise ActivationRejected(EXPIRED) from exc
    except signing.BadSignature as exc:
        raise ActivationRejected(INVALID_TOKEN) from exc
    if not user.is_authenticated or payload["user"] != user.pk:
        raise ActivationRejected(OTHER_USER)
    if payload["connection"] != router.db_for_write(Association):
        raise ActivationRejected(OTHER_BRANCH)
    pairs = []
    for scope_field, type_pk, revision_pk, digest in payload["pairs"]:
        hardware_type = (
            SCOPES[scope_field]
            .model.objects.filter(pk=type_pk)
            .select_related("manufacturer")
            .first()
        )
        revision = (
            DefinitionRevision.objects.filter(pk=revision_pk, digest=digest)
            .select_related("definition")
            .first()
        )
        if hardware_type is None or revision is None:
            raise ActivationRejected(CHANGED)
        pairs.append(Pair(hardware_type, revision))
    return pairs, payload["fingerprint"]


def _apply(user, pairs, fingerprint) -> list[Association]:
    using = router.db_for_write(Association)
    affected = _Affected.read(pairs)
    locked = affected.dependency_types
    # The writers of binding overrides and instance pins take the same locks.
    lock_types(locked, using)
    # Plain querysets lock first: a restricted queryset is not read again after a lock wait.
    for spec in TYPE_SCOPES.values():
        targets = [p.hardware_type.pk for p in pairs if p.scope == spec]
        _lock(spec.model.objects.filter(pk__in=targets))
    _lock(Association.objects.filter(_of_types(p.hardware_type for p in pairs)))
    device_ids = [device.pk for device in affected.devices]
    _lock(Device.objects.filter(pk__in=device_ids))
    _lock(Module.objects.filter(pk__in=[module.pk for module in affected.modules]))
    for model in (*BINDABLE_MODELS, ModuleBay):
        _share(model, device_ids)
    review = _inspect(user, pairs)
    if (
        not review.ready
        or review.fingerprint != fingerprint
        or not review.dependency_types <= locked
    ):
        raise ActivationRejected(CHANGED)
    changed = [_write(user, target) for target in review.targets]
    return [association for association in changed if association is not None]


def _lock(queryset) -> None:
    """Lock the rows of a plain ``queryset`` FOR UPDATE on the connection of associations, in primary key order."""
    list(
        queryset.using(router.db_for_write(Association))
        .select_for_update()
        .order_by("pk")
        .values_list("pk", flat=True)
    )


def _share(model, device_ids) -> None:
    """Lock the rows of ``model`` on the devices FOR SHARE on the connection of associations, in primary key order."""
    if not device_ids:
        return
    using = router.db_for_write(Association)
    query = (
        model.objects.using(using)
        .filter(device__in=device_ids)
        .order_by("pk")
        .values_list("pk")
        .query
    )
    # Django has no FOR SHARE.
    sql, params = query.get_compiler(using=using).as_sql()
    with connections[using].cursor() as cursor:
        cursor.execute(f"{sql} FOR SHARE", params)


def _write(user, target) -> Association | None:
    """Write the association of one pair; return it, or None when it keeps its revision."""
    pair, association = target.pair, target.association
    if association is None:
        association = Association(**{pair.scope.field: pair.hardware_type})
        action = "add"
    elif target.unchanged:
        return None
    else:
        association.snapshot()
        action = "change"
    association.revision = pair.revision
    # A concurrent first association fails on the unique constraint instead.
    association.full_clean(validate_unique=False)
    if action == "add":
        _insert(association, pair)
    else:
        association.save()
    # Constraints of the permission apply to the saved row, as in NetBox's edit views.
    if not (
        Association.objects.restrict(user, action).filter(pk=association.pk).exists()
    ):
        raise PermissionDenied
    return association


def _insert(association, pair) -> None:
    try:
        # A savepoint keeps the transaction usable for the check below.
        with (
            transaction.atomic(using=DEFAULT_DB_ALIAS),
            transaction.atomic(using=router.db_for_write(Association)),
        ):
            association.save()
    except IntegrityError as exc:
        # Only a concurrent first association of the type is expected; other errors stay visible.
        if not Association.objects.filter(_of_types([pair.hardware_type])).exists():
            raise
        raise ActivationRejected(CONCURRENT) from exc


def _of_types(types) -> Q:
    """Return the filter of the rows (associations, overrides, types) whose type field names one of ``types``."""
    ids = defaultdict(list)
    for hardware_type in types:
        ids[type_scope(hardware_type)].append(hardware_type.pk)
    return functools.reduce(
        operator.or_, (spec.among(pks) for spec, pks in ids.items()), Q(pk__in=[])
    )


@dataclass(frozen=True)
class _Affected:
    """The instances that a selection affects, read without permissions."""

    pairs: tuple[Pair, ...]
    # {pair key: the devices or installed modules of the type without a pin}
    instances: dict
    # {pair key: the pinned devices or installed modules of the type}
    pinned: dict
    # The affected devices, and every module installed in them, in primary key order.
    devices: tuple[Device, ...]
    modules: tuple[Module, ...]

    @classmethod
    def read(cls, pairs) -> "_Affected":
        instances, pinned, device_ids = {}, {}, set()
        for pair in pairs:
            spec = instance_scope(type(pair.hardware_type))
            pins = set(
                InstancePin.objects.filter(
                    **{f"{spec.field}__{spec.type_field}": pair.hardware_type}
                ).values_list(f"{spec.field}_id", flat=True)
            )
            found = list(
                spec.model.objects.filter(**{spec.type_field: pair.hardware_type})
                .select_related(*_RELATIONS[spec.model])
                .order_by("pk")
            )
            instances[pair.key] = tuple(i for i in found if i.pk not in pins)
            pinned[pair.key] = tuple(i for i in found if i.pk in pins)
            device_ids |= {_device(i).pk for i in instances[pair.key]}
        devices = Device.objects.filter(pk__in=device_ids)
        return cls(
            tuple(pairs),
            instances,
            pinned,
            tuple(devices.select_related(*_RELATIONS[Device]).order_by("pk")),
            tuple(
                Module.objects.filter(device__in=devices)
                .select_related(*_RELATIONS[Module])
                .order_by("pk")
            ),
        )

    @property
    def dependency_types(self) -> frozenset:
        """Return the targets and the type of each part on a placement path of an affected device."""
        return frozenset(
            {
                *(pair.hardware_type for pair in self.pairs),
                *(device.device_type for device in self.devices),
                *(module.module_type for module in self.modules),
            }
        )

    @property
    def listed_device_ids(self) -> set[int]:
        """Return the affected devices and the devices of the pinned instances: the review names them."""
        ids = {device.pk for device in self.devices}
        for pinned in self.pinned.values():
            ids |= {_device(i).pk for i in pinned}
        return ids


# The objects that the labels of a device and an installed module read.
_RELATIONS = {
    Device: ("device_type__manufacturer",),
    Module: ("device", "module_bay", "module_type__manufacturer"),
}


def _inspect(user, pairs) -> Review:
    affected = _Affected.read(pairs)
    types = affected.dependency_types
    if not _visible(user, pairs, affected):
        return Review(hidden=True, dependency_types=types)
    associations = {
        (TYPE_SCOPES[a.kind].field, TYPE_SCOPES[a.kind].id_of(a)): a
        for a in Association.objects.filter(_of_types(types)).select_related(
            "revision__definition"
        )
    }
    selection = {pair.key: pair.revision for pair in pairs}
    changes = _compare(user, affected, selection)
    targets = tuple(
        Target(
            pair,
            associations.get(pair.key),
            affected.instances[pair.key],
            affected.pinned[pair.key],
            _blocking_overrides(pair, affected.pinned[pair.key]),
            _permission_error(user, pair, associations.get(pair.key)),
        )
        for pair in pairs
    )
    return Review(
        hidden=False,
        dependency_types=types,
        targets=targets,
        blockers=tuple(b for change in changes for b in change.blockers()),
        unresolved=tuple(u for change in changes for u in change.unresolved()),
        warnings=tuple(w for change in changes for w in change.warnings()),
        fingerprint=_fingerprint(affected, associations),
    )


def _visible(user, pairs, affected) -> bool:
    """Return True when ``user`` can view every object that the review names or composes."""
    devices = list(affected.listed_device_ids)
    querysets = (
        Device.objects.filter(pk__in=devices),
        *(
            model.objects.filter(device__in=devices)
            for model in (Module, ModuleBay, *BINDABLE_MODELS)
        ),
        BindingOverride.objects.filter(_depending_on(p.hardware_type for p in pairs)),
    )
    return all(qs.restrict(user, "view").count() == qs.count() for qs in querysets)


def _depending_on(types) -> Q:
    """Return the overrides that depend on one of ``types``."""
    return functools.reduce(operator.or_, map(depending_on, types), Q(pk__in=[]))


def _permission_error(user, pair, association) -> str | None:
    viewable = (
        pair.scope.model.objects.restrict(user, "view")
        .filter(pk=pair.hardware_type.pk)
        .exists()
        and DefinitionRevision.objects.restrict(user, "view")
        .filter(pk=pair.revision.pk)
        .exists()
    )
    if not viewable:
        return NOT_VIEWABLE
    if association is None:
        return None if user.has_perm(ADD_PERMISSION) else NEEDS_ADD
    if Association.objects.restrict(user, "change").filter(pk=association.pk).exists():
        return None
    return NEEDS_CHANGE


def _blocking_overrides(pair, pinned) -> tuple[BindingOverride, ...]:
    """Return the overrides of the type and its instances that name a connector that the candidate does not have.

    The override of a pinned instance does not use the candidate.
    """
    spec = instance_scope(type(pair.hardware_type))
    return tuple(
        BindingOverride.objects.filter(
            depending_on(pair.hardware_type), definition=pair.revision.definition_id
        )
        .exclude(connector_key__in=list(pair.revision.manifest["connectors"]))
        .exclude(**{f"{spec.field}__in": [i.pk for i in pinned]})
        .select_related(*OVERRIDE_RELATIONS)
    )


def _compare(user, affected, selection) -> list["_DeviceChange"]:
    """Compose each affected device in the active and in the candidate state."""
    devices = list(affected.devices)
    before = active_revisions(devices)
    after = active_revisions(devices, selection)
    modules = active_revisions(list(affected.modules))
    # A part whose type is selected and that has no revision now compares from an empty state.
    new = {
        *(
            (Device, d.pk)
            for d in devices
            if ("device_type", d.device_type_id) in selection and d.pk not in before
        ),
        *(
            (Module, m.pk)
            for m in affected.modules
            if ("module_type", m.module_type_id) in selection and m.pk not in modules
        ),
    }
    parents = {m.pk: m.module_bay.module_id for m in affected.modules}
    return [
        _DeviceChange(
            device,
            _compose(device, user, before.get(device.pk)),
            _compose(device, user, after.get(device.pk), selection),
            new,
            parents,
        )
        for device in devices
    ]


def _compose(device, user, revision, selection=None):
    """Compose ``device`` as rendering does: its artwork when the state gives it a revision, else its schematic."""
    if revision is None:
        return resolve_schematic(device, user, plain=True, selection=selection)
    return resolve_device(device, revision, user, plain=True, selection=selection)


@dataclass(frozen=True)
class _DeviceChange:
    """One affected device composed in the active state and in the candidate state."""

    device: Device
    active: object
    candidate: object
    # {(Device or Module, pk)} of the parts that compare from an empty state.
    new: set
    # {module pk: the pk of the module that has its bay, or None}
    parents: dict

    def is_new(self, module_pk) -> bool:
        """Return True when the part (a module, or None for the device) or a part above it compares from an empty state."""
        if (Device, self.device.pk) in self.new:
            return True
        while module_pk is not None:
            if (Module, module_pk) in self.new:
                return True
            module_pk = self.parents.get(module_pk)
        return False

    def label(self, module_pk) -> str:
        if module_pk is None:
            return str(self.device)
        return next(
            placed.label
            for part in (self.active, self.candidate)
            for placed in part.modules
            if placed.module.pk == module_pk
        )

    def blockers(self) -> list[Blocker]:
        found = _drawn_connectors(self.candidate)
        artwork = _artwork_parts(self.candidate)
        blockers = []
        for (owner, key), connector in _drawn_connectors(self.active).items():
            if self.is_new(owner):
                continue
            kept = found.get((owner, key))
            if kept is not None:
                reason = NOT_BOUND
            elif owner in artwork:
                reason = NO_CONNECTOR.format(key=key)
            else:
                reason = NOT_ARTWORK
            lost = _found(connector) - (set() if kept is None else _found(kept))
            blockers.extend(
                Blocker(
                    self.device,
                    self.label(owner),
                    key,
                    component_label(model, name),
                    reason,
                )
                for model, name in sorted(lost, key=lambda b: (b[0]._meta.label, b[1]))
            )
        lost = _drawn_modules(self.active) - _drawn_modules(self.candidate)
        blockers.extend(
            Blocker(self.device, placed.label, None, None, NOT_DRAWN)
            for placed in self.active.modules
            if placed.module.pk in lost and not self.is_new(placed.module.pk)
        )
        return blockers

    def unresolved(self) -> list[Unresolved]:
        return [
            Unresolved(
                self.device, self.label(owner), key, component_label(b.model, b.name)
            )
            for (owner, key), connector in _drawn_connectors(self.candidate).items()
            for b in connector.bindings
            if b.status == MISSING
        ]

    def warnings(self) -> list[ReviewWarning]:
        """Return the warnings of the installed modules that the candidate adds or changes."""
        seen = {
            (placed.module.pk, w.code, w.message)
            for placed in self.active.modules
            for w in placed.warnings
        }
        return [
            ReviewWarning(self.device, w.message)
            for placed in self.candidate.modules
            for w in placed.warnings
            if (placed.module.pk, w.code, w.message) not in seen
        ]


def _drawn_connectors(part) -> dict:
    """Return {(module pk, or None for the device, connector key): connector} of each part that ``part`` draws with artwork.

    The connectors of a schematic are not bindings of a revision.
    """
    found = {}
    if part.representation == ARTWORK:
        found.update(((None, c.key), c) for c in part.connectors)
    for placed in part.modules:
        if placed.representation == ARTWORK:
            found.update(((placed.module.pk, c.key), c) for c in placed.connectors)
    return found


def _artwork_parts(part) -> set:
    """Return the module pk of each module that ``part`` draws with artwork, and None when it draws the device so."""
    parts = {p.module.pk for p in part.modules if p.representation == ARTWORK}
    if part.representation == ARTWORK:
        parts.add(None)
    return parts


def _drawn_modules(part) -> set[int]:
    return {p.module.pk for p in part.modules if p.element_id is not None}


def _found(connector) -> set:
    return {(b.model, b.name) for b in connector.bindings if b.status == FOUND}


def _fingerprint(affected, associations) -> str:
    """Return the SHA-256 of the impact set; cable state is not in it."""
    devices = [device.pk for device in affected.devices]
    types = affected.dependency_types
    targets = [pair.hardware_type for pair in affected.pairs]
    pins = (
        Q(device__in=devices)
        | Q(module__device__in=devices)
        | Q(device__device_type__in=[t for t in targets if isinstance(t, DeviceType)])
        | Q(module__module_type__in=[t for t in targets if isinstance(t, ModuleType)])
    )
    states = []
    for hardware_type in types:
        key = (type_scope(hardware_type).field, hardware_type.pk)
        association = associations.get(key)
        state = (
            [None, None]
            if association is None
            else [association.pk, association.revision_id]
        )
        states.append([*key, *state])
    material = {
        "associations": sorted(states),
        "devices": [[d.pk, d.name, d.device_type_id] for d in affected.devices],
        "modules": [
            [m.pk, m.module_type_id, m.module_bay_id] for m in affected.modules
        ],
        "components": sorted(
            [model._meta.model_name, *values]
            for model in BINDABLE_MODELS
            for values in model.objects.filter(device__in=devices).values_list(
                "pk", "name", "module_id"
            )
        ),
        "bays": list(
            ModuleBay.objects.filter(device__in=devices)
            .order_by("pk")
            .values_list("pk", "name", "position", "module_id")
        ),
        "pins": list(
            InstancePin.objects.filter(pins)
            .order_by("pk")
            .values_list("pk", "device_id", "module_id", "revision_id")
        ),
        "overrides": list(
            BindingOverride.objects.filter(_depending_on(types))
            .order_by("pk")
            .values_list(
                "pk",
                *(f"{name}_id" for name in SCOPE_FIELDS),
                "definition_id",
                "connector_key",
                "bindings",
            )
        ),
    }
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
