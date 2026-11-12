"""Object permissions for the tests."""

from core.models import ObjectType
from users.models import ObjectPermission


def grant(user, model, *actions, constraints=None):
    """Give ``user`` the ``actions`` on ``model``; the name starts with the model label."""
    permission = ObjectPermission.objects.create(
        name=f"{model._meta.label_lower} {' '.join(actions)} {ObjectPermission.objects.count()}",
        actions=list(actions),
        constraints=constraints,
    )
    permission.users.add(user)
    permission.object_types.add(ObjectType.objects.get_for_model(model))
    return permission
