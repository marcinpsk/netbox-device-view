"""Explicit activation of a definition revision for a device type, through the plugin pages."""

import io

from core.models import ObjectChange, ObjectType
from dcim.models import DeviceType
from django.urls import reverse
from users.models import ObjectPermission, User

from netbox_device_view.artwork.importing import import_bundle
from netbox_device_view.models import Association, DefinitionRevision

from .artwork_samples import DEVICE_FILES, FRONT_SVG, MODULE_FILES, bundle, definition
from .test_artwork_device_view import ArtworkTestCase, faces

ACTIVATE_URL = "plugins:netbox_device_view:definitionrevision_activate"
REVISION_URL = "plugins:netbox_device_view:definitionrevision"
ASSOCIATION_URL = "plugins:netbox_device_view:association"
LIST_URL = "plugins:netbox_device_view:association_list"
DELETE_URL = "plugins:netbox_device_view:association_delete"
NEW_FRONT = FRONT_SVG.replace("#333", "#222")


class ActivationTestCase(ArtworkTestCase):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.get(username="admin")
        self.grant_action(DefinitionRevision, "view")
        self.grant_action(DeviceType, "view")

    def grant_action(self, model, *actions, constraints=None):
        permission = ObjectPermission.objects.create(
            name=f"{model._meta.model_name} {actions} {ObjectPermission.objects.count()}",
            actions=list(actions),
            constraints=constraints,
        )
        permission.users.add(self.user)
        permission.object_types.add(ObjectType.objects.get_for_model(model))

    def import_files(self, files, directory="ACME/SW-24"):
        [imported] = import_bundle(
            self.admin, "more.zip", io.BytesIO(bundle(definition(directory, files)))
        )
        return imported.revision

    def post(self, revision=None, device_type=None):
        return self.client.post(
            reverse(ACTIVATE_URL, args=[(revision or self.revision).pk]),
            {"device_type": (device_type or self.device_type).pk},
        )


class ActivationTests(ActivationTestCase):
    def test_activation_needs_add_or_change_permission_on_the_association(self):
        self.assertEqual(
            self.client.get(reverse(ACTIVATE_URL, args=[self.revision.pk])).status_code,
            403,
        )
        self.assertEqual(self.post().status_code, 403)
        page = self.client.get(reverse(REVISION_URL, args=[self.revision.pk]))
        self.assertNotContains(page, reverse(ACTIVATE_URL, args=[self.revision.pk]))
        self.assertFalse(Association.objects.exists())

    def test_a_first_activation_needs_add_and_shows_the_artwork(self):
        self.grant_action(Association, "view", "add")
        page = self.client.get(reverse(REVISION_URL, args=[self.revision.pk]))
        self.assertContains(page, reverse(ACTIVATE_URL, args=[self.revision.pk]))

        response = self.post()

        association = Association.objects.get()
        self.assertRedirects(response, reverse(ASSOCIATION_URL, args=[association.pk]))
        self.assertEqual(association.device_type, self.device_type)
        self.assertEqual(association.revision, self.revision)
        self.assertEqual(len(faces(self.tab())), 2)
        change = ObjectChange.objects.get(
            changed_object_type=ObjectType.objects.get_for_model(Association)
        )
        self.assertEqual(change.action, "create")
        self.assertEqual(change.user, self.user)

    def test_a_later_activation_needs_change_permission(self):
        self.grant_action(Association, "view", "add")
        self.post()
        newer = self.import_files({**DEVICE_FILES, "front.svg": NEW_FRONT})

        self.assertEqual(self.post(newer).status_code, 403)
        self.assertEqual(Association.objects.get().revision, self.revision)

        self.grant_action(Association, "change")
        self.assertEqual(self.post(newer).status_code, 302)
        self.assertEqual(Association.objects.get().revision, newer)
        change = ObjectChange.objects.filter(
            changed_object_type=ObjectType.objects.get_for_model(Association)
        ).latest("time")
        self.assertEqual(change.action, "update")
        self.assertEqual(change.prechange_data["revision"], self.revision.pk)
        self.assertEqual(change.postchange_data["revision"], newer.pk)

    def test_the_same_revision_again_needs_change_permission_and_changes_nothing(self):
        self.grant_action(Association, "view", "add")
        self.post()
        self.assertEqual(self.post().status_code, 403)
        self.grant_action(Association, "change")
        self.assertEqual(self.post().status_code, 302)
        self.assertEqual(
            ObjectChange.objects.filter(
                changed_object_type=ObjectType.objects.get_for_model(Association)
            ).count(),
            1,
        )

    def test_permission_constraints_apply_to_the_activated_type(self):
        self.grant_action(
            Association, "add", constraints={"device_type__model": "Other"}
        )
        self.assertEqual(self.post().status_code, 403)
        self.assertFalse(Association.objects.exists())

    def test_a_module_definition_cannot_be_activated_for_a_device_type(self):
        self.grant_action(Association, "view", "add")
        module_revision = self.import_files(MODULE_FILES, "ACME/NM-2X")
        page = self.client.get(reverse(REVISION_URL, args=[module_revision.pk]))
        self.assertNotContains(page, reverse(ACTIVATE_URL, args=[module_revision.pk]))

        response = self.post(module_revision)

        self.assertContains(
            response,
            "The revision is a module definition. A device type needs a device definition.",
            status_code=400,
        )
        self.assertFalse(Association.objects.exists())

    def test_a_revision_or_a_device_type_that_the_user_cannot_view_is_rejected(self):
        self.grant_action(Association, "view", "add")
        ObjectPermission.objects.filter(name__startswith="devicetype").update(
            constraints={"model": "Other"}
        )
        self.assertEqual(self.post().status_code, 400)
        ObjectPermission.objects.filter(name__startswith="definitionrevision").delete()
        self.assertEqual(self.post().status_code, 404)
        self.assertFalse(Association.objects.exists())


class AssociationPageTests(ActivationTestCase):
    def setUp(self):
        super().setUp()
        self.association = Association.objects.create(
            device_type=self.device_type, revision=self.revision
        )
        self.grant_action(Association, "view", "delete")

    def test_the_list_and_the_detail_page_show_the_association(self):
        listing = self.client.get(reverse(LIST_URL))
        self.assertContains(listing, "SW-24")
        self.assertContains(listing, self.revision.digest[:10])
        detail = self.client.get(reverse(ASSOCIATION_URL, args=[self.association.pk]))
        self.assertContains(detail, self.revision.digest)
        self.assertContains(detail, self.device_type.get_absolute_url())

    def test_an_active_revision_cannot_be_deleted(self):
        self.grant_action(DefinitionRevision, "delete")
        response = self.client.post(
            reverse(
                "plugins:netbox_device_view:definitionrevision_delete",
                args=[self.revision.pk],
            ),
            {"confirm": True},
            follow=True,
        )
        self.assertContains(response, "Unable to delete")
        self.assertTrue(DefinitionRevision.objects.filter(pk=self.revision.pk).exists())

    def test_deleting_the_association_returns_the_device_to_its_layout(self):
        self.client.post(
            reverse(DELETE_URL, args=[self.association.pk]), {"confirm": True}
        )
        self.assertFalse(Association.objects.exists())
        self.assertEqual(faces(self.tab()), [])
