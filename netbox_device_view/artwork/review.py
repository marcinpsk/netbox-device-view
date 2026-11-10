"""Review the impact of an activation, and activate a reviewed selection: the one writer of associations."""

import functools
import hashlib
import json
import operator
from collections import defaultdict
from dataclasses import dataclass, field

from core.choices import ObjectChangeActionChoices
from core.models import ObjectChange, ObjectType
from dcim.models import Device, DeviceType, Module, ModuleBay, ModuleType
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, OperationalError, connections, router
from django.db.models import BooleanField, ExpressionWrapper, Q
from django.utils.text import capfirst
from django.utils.translation import gettext_lazy as _

from ..components import BINDABLE_MODELS, component_label, findable
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
    share_rows,
    type_key,
    type_scope,
    write_transaction,
)
from .overrides import depending_on, of_parts
from .render import (
    ARTWORK,
    DAMAGED,
    Part,
    part_label,
    resolve_device,
    resolve_schematic,
)
from .type_lock import lock_bulk_writes, lock_types

ADD_PERMISSION = "netbox_device_view.add_association"
CHANGE_PERMISSION = "netbox_device_view.change_association"
SIGNING_SALT = "netbox_device_view.artwork.review"
REVIEW_MAX_AGE_SECONDS = 15 * 60
MAX_PAIRS = 100
# The SQLSTATE of a PostgreSQL deadlock.
DEADLOCK = "40P01"
# The SQLSTATE of a lock wait that reached lock_timeout.
LOCK_NOT_AVAILABLE = "55P03"
# The longest wait of the activation for one lock, in milliseconds.
LOCK_TIMEOUT_MS = 10_000
# The compositions of one activation: the second follows a composition that the locked state shows as stale.
COMPOSITIONS = 2

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
LOCKED = _(
    "Another change held a lock on the affected objects for too long. Try the"
    " activation again."
)
_LOCK_ERRORS = {DEADLOCK: CONCURRENT, LOCK_NOT_AVAILABLE: LOCKED}
NOT_VIEWABLE = _("Select a type and a revision that you can view.")
NEEDS_ADD = _("A first activation needs the add permission on artwork associations.")
NEEDS_CHANGE = _("An activation needs the change permission on this association.")
# The reason of each blocker.
NOT_BOUND = _("The connector does not bind the component.")
NO_CONNECTOR = _("The candidate has no connector {key}.")
NOT_ARTWORK = _("The candidate does not draw the part with artwork.")
NOT_DRAWN = _("The candidate does not draw the installed module.")
DAMAGED_CANDIDATE = _("The stored artwork of the candidate is damaged.")
UNSTABLE = _(
    "The affected objects changed while the review read them. Review the activation"
    " again."
)


class ActivationRejected(Exception):
    """The activation did not apply; the message says why."""


class _StaleReview(Exception):
    """The locked impact set is the reviewed one, and the composition of the activation read another."""


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
        return type_key(self.hardware_type)

    def to_token(self) -> list:
        """Return the pair as it is in a review token: from_token() reads it."""
        return [
            self.scope.field,
            self.hardware_type.pk,
            self.revision.pk,
            self.revision.digest,
        ]

    @classmethod
    def from_token(cls, value) -> "Pair | None":
        """Return the pair of a review token, or None when its type or revision does not exist any more."""
        scope_field, type_pk, revision_pk, digest = value
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
            return None
        return cls(hardware_type, revision)


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
    # The overrides of the active definition key, which stop applying under a candidate of another key.
    stopping_overrides: tuple[BindingOverride, ...]
    # Why the user cannot activate the pair, or None.
    error: str | None

    @property
    def unchanged(self) -> bool:
        return _unchanged(self.association, self.pair)

    @property
    def instance_label(self) -> str:
        """Return the name of the instances of the type: "Device" or "Installed module"."""
        field = instance_scope(type(self.pair.hardware_type)).field
        return capfirst(InstancePin._meta.get_field(field).verbose_name)

    @property
    def names_device(self) -> bool:
        """Return True when an instance is an installed module: its row names its device."""
        return instance_scope(type(self.pair.hardware_type)).names_device

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


def _unchanged(association, pair) -> bool:
    """Return True when ``association`` exists and has the revision of ``pair``."""
    return association is not None and association.revision_id == pair.revision.pk


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
    # {(Device or Module, pk)} of the affected and pinned instances that the review read.
    listed_rows: frozenset = frozenset()
    # False when the impact set changed while the review read it.
    stable: bool = True
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
        return (
            not self.hidden
            and self.stable
            and self.permitted
            and not self.blocked
            and self.changes
        )


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
                "pairs": [pair.to_token() for pair in pairs],
                "fingerprint": review.fingerprint,
            },
            salt=SIGNING_SALT,
        )
    return review


def apply(user, token) -> list[Association]:
    """Activate the reviewed selection of ``token`` in one transaction; return the changed associations.

    The activation composes the review before it takes any lock. Under the
    locks it reads only the fingerprint again, checks the permissions and
    writes. Raise ActivationRejected when the review is not valid any more, and
    PermissionDenied when the permissions of ``user`` do not cover a saved
    association. Nothing changes then.
    """
    pairs, fingerprint = _load(user, token)
    for _composition in range(COMPOSITIONS):
        # The composition is the slow part, so it runs before the locks.
        reviewed = _inspect(user, pairs)
        try:
            with write_transaction(Association):
                return _apply(user, pairs, fingerprint, reviewed)
        except _StaleReview:
            continue
        except OperationalError as exc:
            rejection = _LOCK_ERRORS.get(getattr(exc.__cause__, "sqlstate", None))
            if rejection is None:
                raise
            raise ActivationRejected(rejection) from exc
    raise ActivationRejected(CONCURRENT)


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
    pairs = [Pair.from_token(value) for value in payload["pairs"]]
    if None in pairs:
        raise ActivationRejected(CHANGED)
    return pairs, payload["fingerprint"]


def _apply(user, pairs, fingerprint, reviewed) -> list[Association]:
    """Lock the impact set, check it against the token and the composition ``reviewed``, and write the associations."""
    using = router.db_for_write(Association)
    catalogue = router.db_for_write(DefinitionRevision)
    _bound_lock_waits({using, catalogue})
    locked = reviewed.dependency_types
    # The writers of binding overrides and instance pins take the same locks.
    # A bulk writer takes its type locks in row order, so the activation first waits for it.
    lock_bulk_writes(using)
    lock_types(locked, using)
    # Plain querysets lock first: a restricted queryset is not read again after a lock wait.
    for spec in TYPE_SCOPES.values():
        targets = [p.hardware_type.pk for p in pairs if p.scope == spec]
        _lock(spec.model.objects.filter(pk__in=targets))
    _lock(Association.objects.filter(_of_types(p.hardware_type for p in pairs)))
    # The catalogue is on main; FOR SHARE keeps a candidate from a delete until the commit.
    candidates = functools.reduce(
        operator.or_, (Q(pk=p.revision.pk, digest=p.revision.digest) for p in pairs)
    )
    shared = share_rows(DefinitionRevision.objects.filter(candidates), catalogue)
    if shared != {pair.revision.pk for pair in pairs}:
        raise ActivationRejected(CHANGED)
    # A pin or an instance can change while the activation waits, so the row locks come from a new read.
    affected = _Affected.read(pairs)
    _lock(Device.objects.filter(pk__in=affected.listed_device_ids))
    _lock(Module.objects.filter(pk__in=affected.listed_module_ids))
    # The review reads the components and the module bays of each device that it names.
    for model in (*BINDABLE_MODELS, ModuleBay):
        share_rows(model.objects.filter(device__in=affected.listed_device_ids), using)
    # The locks keep every input of the fingerprint until the commit, so one read is enough.
    current, associations, current_fingerprint = _snapshot(pairs)
    if (
        current_fingerprint != fingerprint
        or current.listed_rows != affected.listed_rows
    ):
        raise ActivationRejected(CHANGED)
    if (
        not reviewed.stable
        or reviewed.fingerprint != current_fingerprint
        or reviewed.dependency_types != current.dependency_types
    ):
        raise _StaleReview
    if (
        not reviewed.ready
        or not _visible(user, pairs, current)
        or any(_permission_error(user, p, associations.get(p.key)) for p in pairs)
    ):
        raise ActivationRejected(CHANGED)
    # The write uses the associations that the locks hold, not those of the composition.
    changed = [_write(user, pair, associations.get(pair.key)) for pair in pairs]
    return [association for association in changed if association is not None]


def _bound_lock_waits(aliases) -> None:
    """Stop each lock wait of the current transaction on ``aliases`` after LOCK_TIMEOUT_MS."""
    for alias in aliases:
        with connections[alias].cursor() as cursor:
            # A transaction-local setting: it ends with the activation.
            cursor.execute(
                "SELECT set_config('lock_timeout', %s, true)", [f"{LOCK_TIMEOUT_MS}ms"]
            )


def _lock(queryset) -> None:
    """Lock the rows of a plain ``queryset`` FOR UPDATE on the connection of associations, in primary key order."""
    list(
        queryset.using(router.db_for_write(Association))
        .select_for_update()
        .order_by("pk")
        .values_list("pk", flat=True)
    )


def _write(user, pair, association) -> Association | None:
    """Write the ``association`` of one pair, or a new one when it is None; return it, or None when it keeps its revision."""
    if association is None:
        association = Association(**{pair.scope.field: pair.hardware_type})
        action = "add"
    elif _unchanged(association, pair):
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
        with write_transaction(Association):
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
                InstancePin.objects.filter(_pins_of(pair.hardware_type)).values_list(
                    f"{spec.field}_id", flat=True
                )
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
    def listed_rows(self) -> frozenset:
        """Return (Device or Module, pk) of each device and module that the review names."""
        return frozenset(
            {
                *((Device, pk) for pk in self.listed_device_ids),
                *((Module, pk) for pk in self.listed_module_ids),
            }
        )

    @property
    def listed_module_ids(self) -> set[int]:
        """Return the modules of the affected devices and the pinned modules: the review names them."""
        ids = {module.pk for module in self.modules}
        for pinned in self.pinned.values():
            ids |= {i.pk for i in pinned if isinstance(i, Module)}
        return ids

    @property
    def listed_device_ids(self) -> set[int]:
        """Return the affected devices and the devices of the pinned instances: the review names them."""
        ids = {device.pk for device in self.devices}
        for pinned in self.pinned.values():
            ids |= {_device(i).pk for i in pinned}
        return ids


def _pins_of(hardware_type) -> Q:
    """Return the filter of the pins of the instances of a device type or a module type."""
    spec = instance_scope(type(hardware_type))
    return Q(**{f"{spec.field}__{spec.type_field}": hardware_type})


def _read_pins(affected) -> Q:
    """Return the filter of the pins that the review reads: the pins of the types and of the affected devices."""
    devices = [device.pk for device in affected.devices]
    return functools.reduce(
        operator.or_,
        (_pins_of(pair.hardware_type) for pair in affected.pairs),
        Q(device__in=devices) | Q(module__device__in=devices),
    )


def _read_overrides(affected) -> Q:
    """Return the filter of the overrides that the review reads: those of the types and of the composed parts."""
    return _depending_on(pair.hardware_type for pair in affected.pairs) | of_parts(
        [*affected.devices, *affected.modules]
    )


# The objects that the labels of a device and an installed module read.
_RELATIONS = {
    Device: ("device_type__manufacturer",),
    Module: ("device", "module_bay", "module_type__manufacturer"),
}


def _snapshot(pairs) -> tuple["_Affected", dict, str]:
    """Return the affected set of ``pairs``, the associations of its types and the fingerprint of the impact set."""
    affected = _Affected.read(pairs)
    associations = {
        type_key(a): a
        for a in Association.objects.filter(
            _of_types(affected.dependency_types)
        ).select_related("revision__definition")
    }
    return affected, associations, _fingerprint(affected, associations)


def _inspect(user, pairs) -> Review:
    affected, associations, fingerprint = _snapshot(pairs)
    types = affected.dependency_types
    if not _visible(user, pairs, affected):
        # The activation compares the fingerprint; the page does not show it.
        return Review(
            hidden=True,
            dependency_types=types,
            fingerprint=fingerprint,
            listed_rows=affected.listed_rows,
        )
    selection = {pair.key: pair.revision for pair in pairs}
    changes = _compare(user, affected, selection)
    targets = tuple(
        Target(
            pair,
            associations.get(pair.key),
            affected.instances[pair.key],
            affected.pinned[pair.key],
            _blocking_overrides(pair, affected.pinned[pair.key]),
            _stopping_overrides(
                pair, associations.get(pair.key), affected.pinned[pair.key]
            ),
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
        fingerprint=fingerprint,
        # The page shows the fingerprinted objects only when nothing changed while the review read them.
        stable=_snapshot(pairs)[2] == fingerprint,
        listed_rows=affected.listed_rows,
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
        BindingOverride.objects.filter(_read_overrides(affected)),
        InstancePin.objects.filter(_read_pins(affected)),
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
    """Return the overrides of the type and its instances that name a connector that the candidate does not have."""
    return tuple(
        _candidate_overrides(pair, pinned, pair.revision.definition_id).exclude(
            connector_key__in=list(pair.revision.manifest["connectors"])
        )
    )


def _stopping_overrides(pair, association, pinned) -> tuple[BindingOverride, ...]:
    """Return the overrides of the active definition key when the candidate has another key: they stop applying."""
    if association is None:
        return ()
    active = association.revision.definition_id
    if active == pair.revision.definition_id:
        return ()
    return tuple(_candidate_overrides(pair, pinned, active))


def _candidate_overrides(pair, pinned, definition_id):
    """Return the overrides of ``definition_id`` of the type and of its instances that use the candidate.

    The override of a pinned instance does not use the candidate.
    """
    spec = instance_scope(type(pair.hardware_type))
    return (
        BindingOverride.objects.filter(
            depending_on(pair.hardware_type), definition=definition_id
        )
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
    first_activations = {
        (type(part), part.pk)
        for part, revisions in (
            *((d, before) for d in devices),
            *((m, modules) for m in affected.modules),
        )
        if type_key(part) in selection and part.pk not in revisions
    }
    parents = {m.pk: m.module_bay.module_id for m in affected.modules}
    return [
        _DeviceChange(
            device,
            _compose(device, user, before.get(device.pk)),
            _compose(device, user, after.get(device.pk), selection),
            first_activations,
            parents,
        )
        for device in devices
    ]


def _compose(device, user, revision, selection=None) -> Part:
    """Compose ``device`` as rendering does: its artwork when the state gives it a revision, else its schematic."""
    if revision is None:
        return resolve_schematic(device, user, plain=True, selection=selection)
    return resolve_device(device, revision, user, plain=True, selection=selection)


@dataclass(frozen=True)
class _DeviceChange:
    """One affected device composed in the active state and in the candidate state."""

    device: Device
    active: Part
    candidate: Part
    # {(Device or Module, pk)} of the parts that compare from an empty state.
    first_activations: set
    # {module pk: the pk of the module that has its bay, or None}
    parents: dict

    def compares_from_empty(self, module_pk) -> bool:
        """Return True when the part (a module, or None for the device) or a part above it compares from an empty state."""
        if (Device, self.device.pk) in self.first_activations:
            return True
        while module_pk is not None:
            if (Module, module_pk) in self.first_activations:
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
        found = _bindings(self.candidate)
        with_bindings = _binding_parts(self.candidate)
        blockers = self.damaged_candidates()
        for (owner, key), connector in _bindings(self.active).items():
            if self.compares_from_empty(owner):
                continue
            kept = found.get((owner, key))
            if kept is not None:
                reason = NOT_BOUND
            elif owner in with_bindings:
                reason = NO_CONNECTOR.format(key=key)
            else:
                reason = NOT_ARTWORK
            lost = _bound_components(connector) - (
                set() if kept is None else _bound_components(kept)
            )
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
            if placed.module.pk in lost
            and not self.compares_from_empty(placed.module.pk)
        )
        return blockers

    def damaged_candidates(self) -> list[Blocker]:
        """Return a blocker for each part that the candidate state changes to a revision with damaged artwork."""
        blockers = []
        if self.candidate.damaged and self.candidate.revision != self.active.revision:
            blockers.append(
                Blocker(self.device, str(self.device), None, None, DAMAGED_CANDIDATE)
            )
        before = {placed.module.pk: placed.revision for placed in self.active.modules}
        blockers.extend(
            Blocker(self.device, placed.label, None, None, DAMAGED_CANDIDATE)
            for placed in self.candidate.modules
            if placed.damaged and before.get(placed.module.pk) != placed.revision
        )
        return blockers

    def unresolved(self) -> list[Unresolved]:
        return [
            Unresolved(
                self.device, self.label(owner), key, component_label(b.model, b.name)
            )
            for (owner, key), connector in _bindings(self.candidate).items()
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


def _bindings(part) -> dict:
    """Return {(module pk, or None for the device, connector key): connector} of each part of ``part`` with revision bindings.

    A part drawn with artwork has them, and so has a part whose stored
    artwork is damaged: its bindings come from the manifest. The components
    of a schematic are not bindings of a revision.
    """
    found = {}
    if part.representation in (ARTWORK, DAMAGED):
        found.update(((None, c.key), c) for c in part.connectors)
    for placed in part.modules:
        if placed.representation == ARTWORK:
            found.update(((placed.module.pk, c.key), c) for c in placed.connectors)
        elif placed.damaged:
            found.update(
                ((placed.module.pk, c.key), c) for c in placed.revision_connectors
            )
    return found


def _binding_parts(part) -> set:
    """Return the module pk, or None for the device, of each part of ``part`` that _bindings() reads."""
    parts = {
        p.module.pk for p in part.modules if p.representation == ARTWORK or p.damaged
    }
    if part.representation in (ARTWORK, DAMAGED):
        parts.add(None)
    return parts


def _drawn_modules(part) -> set[int]:
    return {p.module.pk for p in part.modules if p.element_id is not None}


def _bound_components(connector) -> set:
    """Return (model, name) of each component that ``connector`` finds."""
    return {(b.model, b.name) for b in connector.bindings if b.status == FOUND}


def _fingerprint(affected, associations) -> str:
    """Return the SHA-256 of the impact set; cable state is not in it."""
    devices = [device.pk for device in affected.devices]
    types = affected.dependency_types
    pins = _read_pins(affected)
    states = []
    for hardware_type in types:
        key = type_key(hardware_type)
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
        # The pinned instances that the review names.
        "pinned": sorted(
            (
                ["device", i.pk, i.name]
                if isinstance(i, Device)
                else ["module", i.pk, i.module_bay.name, i.device_id, i.device.name]
            )
            for pinned in affected.pinned.values()
            for i in pinned
        ),
        "modules": [
            [m.pk, m.module_type_id, m.module_bay_id] for m in affected.modules
        ],
        # The name, the module and the fields that decide whether a binding can find the component.
        "components": sorted(
            [model._meta.model_name, *values]
            for model in BINDABLE_MODELS
            # The default order of a component joins its device; sorted() orders the rows.
            for values in model.objects.filter(device__in=devices)
            .order_by()
            .annotate(
                findable=ExpressionWrapper(findable(model), output_field=BooleanField())
            )
            .values_list("pk", "name", "module_id", "findable")
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
            BindingOverride.objects.filter(_read_overrides(affected))
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
