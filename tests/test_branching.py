"""Plugin behaviour in an active NetBox Branching branch; skipped when netbox-branching is not enabled."""

import io
import re
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path

from core.models import DataFile, DataSource, ObjectChange, ObjectType
from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.apps import apps
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import ProtectedError
from django.test import (
    RequestFactory,
    SimpleTestCase,
    TransactionTestCase,
    override_settings,
)
from django.urls import reverse
from netbox.context_managers import event_tracking
from rest_framework.test import APIClient
from users.constants import TOKEN_PREFIX
from users.models import Token, User

from netbox_device_view import datasource_import
from netbox_device_view.artwork import pins
from netbox_device_view.artwork.branching import CATALOGUE
from netbox_device_view.artwork.capture import CHANGED_FILE
from netbox_device_view.artwork.importing import import_bundle
from netbox_device_view.artwork.manifest import MAX_CONNECTOR_KEY_LENGTH
from netbox_device_view.models import (
    MAX_REPR,
    Association,
    BindingOverride,
    DefinitionRevision,
    DeviceView,
    InstancePin,
    VisualDefinition,
    active_revisions,
)

from .artwork_samples import DEVICE_FILES, FRONT_SVG, bundle, definition
from .permissions import grant
from .test_artwork_activation import activate_through_review
from .test_artwork_import import DEVICE_DIR
from .test_artwork_import import IMPORT_URL as ARTWORK_IMPORT_URL
from .test_artwork_key_transfer import CHANGELOG_URL as DEFINITION_CHANGELOG_URL
from .test_artwork_key_transfer import EDIT_URL as DEFINITION_EDIT_URL
from .test_artwork_source_import import SOURCE_IMPORT_URL
from .test_binding_override_api import LIST as OVERRIDE_API_LIST
from .test_binding_override_pages import ADD_URL as OVERRIDE_ADD_URL
from .test_binding_override_pages import rows
from .test_datasource_import import APPLY_URL, EXAMPLE_PATH, EXAMPLE_YAML
from .test_datasource_import_concurrency import ConcurrentApplyTestBase

BRANCHING = "netbox_branching" in settings.PLUGINS
REVISION_DELETE_URL = "plugins:netbox_device_view:definitionrevision_delete"
REVISION_API_URL = "plugins-api:netbox_device_view-api:definitionrevision-detail"


def provision_branch(name):
    from netbox_branching.models import Branch

    branch = Branch(name=name)
    branch.save(provision=False)
    branch.provision(user=None)
    branch.refresh_from_db()
    return branch


@unittest.skipUnless(BRANCHING, "netbox-branching is not enabled")
class DataSourceImportInBranchTests(ConcurrentApplyTestBase):
    def setUp(self):
        super().setUp()
        self.branch = provision_branch("Layouts")
        self.addCleanup(self.branch.deprovision)
        self.clients[0].cookies["active_branch"] = self.branch.schema_id

    def test_import_creates_the_device_view_in_the_branch_only(self):
        from netbox_branching.utilities import activate_branch

        response = self.clients[0].post(
            reverse(APPLY_URL), {"token": self.token(EXAMPLE_PATH)}
        )

        self.assertContains(response, "Created: 1")
        self.assertFalse(DeviceView.objects.exists())
        with activate_branch(self.branch):
            self.assertEqual(
                DeviceView.objects.get().yaml_layout, EXAMPLE_YAML.read_text()
            )

    def test_a_review_in_a_branch_does_not_apply_in_main(self):
        from netbox_branching.utilities import activate_branch

        token = self.token(EXAMPLE_PATH)
        del self.clients[0].cookies["active_branch"]

        response = self.clients[0].post(reverse(APPLY_URL), {"token": token})

        self.assertContains(response, datasource_import.OTHER_BRANCH, status_code=400)
        self.assertFalse(DeviceView.objects.exists())
        with activate_branch(self.branch):
            self.assertFalse(DeviceView.objects.exists())

    def test_a_rejected_import_leaves_no_change_records(self):
        from netbox_branching.models import ChangeDiff
        from netbox_branching.utilities import activate_branch

        with activate_branch(self.branch):
            other_type = DeviceType.objects.create(
                manufacturer=self.device_type.manufacturer, model="Other", slug="other"
            )
        token = self.token_for(
            [
                (EXAMPLE_PATH, self.device_type),
                ("examples/yaml/Cisco/Other.yaml", other_type),
            ]
        )
        # The second target gets a device view after the review, so its row is stale.
        with activate_branch(self.branch):
            DeviceView.objects.create(device_type=other_type)

        response = self.clients[0].post(reverse(APPLY_URL), {"token": token})

        self.assertEqual(response.status_code, 400)
        with activate_branch(self.branch):
            self.assertEqual(DeviceView.objects.count(), 1)
        self.assertFalse(ChangeDiff.objects.exists())
        self.assertFalse(ObjectChange.objects.using("default").exists())


class CatalogueModelTests(SimpleTestCase):
    def test_every_plugin_model_that_refers_to_no_type_is_in_the_catalogue(self):
        labels = {
            model._meta.label_lower
            for model in apps.get_app_config("netbox_device_view").get_models()
        }

        # A branch can change device types, module types, devices and modules, so their links are branched too.
        self.assertEqual(
            labels - CATALOGUE,
            {
                "netbox_device_view.deviceview",
                "netbox_device_view.association",
                "netbox_device_view.bindingoverride",
                "netbox_device_view.instancepin",
            },
        )


def long_device_type(save=False, manufacturer_name="M" * 100):
    """Return a device type whose manufacturer name and model have the maximum length."""
    manufacturer = Manufacturer(name=manufacturer_name, slug="m" * 100)
    device_type = DeviceType(manufacturer=manufacturer, model="X" * 100, slug="x" * 100)
    if save:
        manufacturer.save()
        device_type.manufacturer = manufacturer
        device_type.save()
    return device_type


class BranchObjectTextTests(SimpleTestCase):
    def test_the_text_of_each_branch_object_fits_its_change_record(self):
        self.assertTextsFit(long_device_type())

    def test_a_combining_mark_counts_in_the_text_of_a_branch_object(self):
        # The column counts code points, and a combining mark is one.
        self.assertTextsFit(
            long_device_type(manufacturer_name="Q\u0301" * 50),
            device_name="D\u0301" * 32,
        )

    def assertTextsFit(self, device_type, device_name="D" * 64):
        # NetBox Branching copies str() of a branch object to ChangeDiff.object_repr without a cut.
        # An override names its scope by the pk of the scope.
        device_type.pk = 1
        revision = DefinitionRevision(
            definition=VisualDefinition(key="acme/" + "a" * 123), digest="0" * 64
        )
        device = Device(pk=1, name=device_name, device_type=device_type)
        for obj, start in (
            (DeviceView(device_type=device_type), device_type.full_name),
            (
                Association(device_type=device_type, revision=revision),
                device_type.full_name,
            ),
            (
                BindingOverride(
                    device_type=device_type,
                    connector_key="con-" + "a" * (MAX_CONNECTOR_KEY_LENGTH - 4),
                ),
                device_type.full_name,
            ),
            (InstancePin(device=device, revision=revision), device.name),
        ):
            with self.subTest(type(obj).__name__):
                self.assertLessEqual(len(str(obj)), MAX_REPR)
                self.assertTrue(str(obj).startswith(start[: MAX_REPR - 1]))


@unittest.skipUnless(BRANCHING, "netbox-branching is not enabled")
class ArtworkBranchTestCase(TransactionTestCase):
    """An artwork importer whose test client works in a new branch."""

    def setUp(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media)
        media_override = override_settings(MEDIA_ROOT=media)
        media_override.enable()
        self.addCleanup(media_override.disable)
        self.user = User.objects.create_user(username="importer")
        self.client.force_login(self.user)
        grant(self.user, DefinitionRevision, "view", "add")
        self.branch = provision_branch("Artwork")
        self.addCleanup(self.branch.deprovision)
        self.client.cookies["active_branch"] = self.branch.schema_id

    def upload(self):
        return self.client.post(
            reverse(ARTWORK_IMPORT_URL),
            {"bundle": SimpleUploadedFile("sw24.zip", bundle())},
            follow=True,
        )


class ArtworkImportInBranchTests(ArtworkBranchTestCase):
    def test_a_bundle_import_in_a_branch_stores_the_revision_in_main(self):
        response = self.upload()

        self.assertContains(response, "Imported")
        revision = DefinitionRevision.objects.get()
        self.assertEqual(revision.definition.key, "acme/sw-24")
        self.assertEqual(
            sorted(revision.files.values_list("path", flat=True)),
            ["front.svg", "manifest.yaml", "rear.svg"],
        )
        self.assertEqual(revision.imports.get().source_name, "sw24.zip")

    def test_a_branch_merge_leaves_the_imported_revision_in_main(self):
        from netbox_branching.choices import BranchStatusChoices
        from netbox_branching.utilities import activate_branch

        self.upload()
        revision = DefinitionRevision.objects.get()
        # Branch.merge() replays nothing without a staged change, so stage one.
        request = RequestFactory().get(reverse("home"))
        request.id, request.user = uuid.uuid4(), self.user
        with activate_branch(self.branch), event_tracking(request):
            Manufacturer.objects.create(name="Branch Only", slug="branch-only")

        self.branch.merge(user=self.user)

        self.branch.refresh_from_db()
        self.assertEqual(self.branch.status, BranchStatusChoices.MERGED)
        self.assertTrue(Manufacturer.objects.filter(slug="branch-only").exists())
        self.assertEqual(DefinitionRevision.objects.get(), revision)
        self.assertEqual(
            sorted(revision.files.values_list("path", flat=True)),
            ["front.svg", "manifest.yaml", "rear.svg"],
        )
        self.assertEqual(revision.imports.get().source_name, "sw24.zip")

    def test_the_revision_change_log_reads_main_in_a_branch(self):
        del self.client.cookies["active_branch"]
        self.upload()
        self.client.cookies["active_branch"] = self.branch.schema_id
        grant(self.user, ObjectChange, "view")
        revision = DefinitionRevision.objects.get()
        change = ObjectChange.objects.using("default").get(
            changed_object_type=ObjectType.objects.get_for_model(DefinitionRevision),
            changed_object_id=revision.pk,
        )

        response = self.client.get(
            reverse(
                "plugins:netbox_device_view:definitionrevision_changelog",
                args=[revision.pk],
            )
        )

        self.assertContains(response, change.get_absolute_url())
        # The selector button shows the active branch; its dropdown lists every branch.
        selected = re.search(
            r'mdi-source-branch"></i>\s*(.*?)\s*</button>', response.content.decode()
        )
        self.assertEqual(selected[1], self.branch.name)


class ArtworkSourceImportInBranchTests(ArtworkBranchTestCase):
    def setUp(self):
        super().setUp()
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        for path, content in definition(DEVICE_DIR, DEVICE_FILES).items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text(content)
        self.source = DataSource.objects.create(
            name="Artwork", type="local", source_url=f"file://{root}", enabled=True
        )
        self.source.sync()
        grant(self.user, DataSource, "view")
        grant(self.user, DataFile, "view")

    def import_source(self):
        return self.client.post(
            reverse(SOURCE_IMPORT_URL),
            {"data_source": self.source.pk, "prefix": ""},
            follow=True,
        )

    def test_a_data_source_import_in_a_branch_stores_the_revision_in_main(self):
        response = self.import_source()

        self.assertContains(response, "Imported")
        revision = DefinitionRevision.objects.get()
        self.assertEqual(revision.definition.key_owner, self.source)
        self.assertEqual(revision.imports.get().data_source, self.source)

    def test_a_torn_file_found_in_a_branch_is_read_again_by_the_next_sync(self):
        front = DataFile.objects.filter(path=f"{DEVICE_DIR}/front.svg")
        front.update(data=FRONT_SVG.replace("#333", "#222").encode())

        self.assertContains(self.import_source(), CHANGED_FILE, status_code=400)
        self.assertEqual(front.get().hash, "")

        self.source.sync()

        self.assertContains(self.import_source(), "Imported")


class LongNameActivationInBranchTests(ArtworkBranchTestCase):
    def setUp(self):
        # The branch copies main when it is provisioned.
        self.device_type = long_device_type(save=True)
        super().setUp()

    def test_an_activation_for_a_type_with_long_names_has_its_change_record(self):
        from netbox_branching.models import ChangeDiff

        self.upload()
        revision = DefinitionRevision.objects.get()
        grant(self.user, Association, "view", "add")
        grant(self.user, DeviceType, "view")

        response = activate_through_review(self.client, (revision, self.device_type))

        self.assertEqual(response.status_code, 302)
        diff = ChangeDiff.objects.using("default").get(
            object_type=ObjectType.objects.get_for_model(Association)
        )
        self.assertTrue(
            diff.object_repr.startswith(self.device_type.full_name[: MAX_REPR - 1])
        )


class PinInBranchTests(ArtworkBranchTestCase):
    def setUp(self):
        # The branch copies main when it is provisioned.
        device_type = DeviceType.objects.create(
            manufacturer=Manufacturer.objects.create(name="ACME", slug="acme"),
            model="SW-24",
            slug="sw-24",
        )
        self.device = Device.objects.create(
            name="sw1",
            device_type=device_type,
            role=DeviceRole.objects.create(name="Switch", slug="switch"),
            site=Site.objects.create(name="Site", slug="site"),
        )
        super().setUp()

    def test_a_revision_pinned_only_in_a_branch_cannot_be_deleted_in_main(self):
        from netbox_branching.utilities import activate_branch

        grant(self.user, InstancePin, "view", "add")
        grant(self.user, Device, "view")
        del self.client.cookies["active_branch"]
        self.upload()
        [pinned] = DefinitionRevision.objects.all()
        files = {**DEVICE_FILES, "front.svg": FRONT_SVG.replace("#333", "#222")}
        [newer] = import_bundle(
            self.user, "newer.zip", io.BytesIO(bundle(definition(DEVICE_DIR, files)))
        )
        with activate_branch(self.branch):
            Association.objects.create(
                device_type=self.device.device_type, revision=newer.revision
            )
            pins.save(self.user, InstancePin(device=self.device, revision=pinned))
            self.assertEqual(
                list(InstancePin.objects.values_list("device", "revision")),
                [(self.device.pk, pinned.pk)],
            )
        # Only the branch has the pin, so the foreign key of main does not protect the revision.
        self.assertFalse(InstancePin.objects.exists())

        with self.assertRaises(ProtectedError) as protected:
            pinned.delete()
        self.assertEqual(protected.exception.protected_objects, {self.branch})
        self.assertTrue(DefinitionRevision.objects.filter(pk=pinned.pk).exists())


class ActiveRevisionInBranchTests(ArtworkBranchTestCase):
    def setUp(self):
        # The branch copies main when it is provisioned.
        device_type = DeviceType.objects.create(
            manufacturer=Manufacturer.objects.create(name="ACME", slug="acme"),
            model="SW-24",
            slug="sw-24",
        )
        role = DeviceRole.objects.create(name="Switch", slug="switch")
        site = Site.objects.create(name="Site", slug="site")
        self.devices = [
            Device.objects.create(
                name=name, device_type=device_type, role=role, site=site
            )
            for name in ("sw1", "sw2")
        ]
        super().setUp()

    def test_a_device_shows_the_pin_and_the_association_of_the_branch(self):
        from netbox_branching.utilities import activate_branch

        del self.client.cookies["active_branch"]
        self.upload()
        [revision] = DefinitionRevision.objects.all()
        files = {**DEVICE_FILES, "front.svg": FRONT_SVG.replace("#333", "#222")}
        [pinned] = import_bundle(
            self.user, "pinned.zip", io.BytesIO(bundle(definition(DEVICE_DIR, files)))
        )
        sw1, sw2 = self.devices
        with activate_branch(self.branch):
            Association.objects.create(device_type=sw1.device_type, revision=revision)
            InstancePin.objects.create(device=sw2, revision=pinned.revision)
            in_branch = active_revisions(self.devices)

        self.assertEqual(in_branch, {sw1.pk: revision, sw2.pk: pinned.revision})
        self.assertEqual(active_revisions(self.devices), {})


class KeyTransferInBranchTests(ArtworkBranchTestCase):
    def test_a_key_transfer_in_a_branch_changes_main_at_once(self):
        self.upload()
        visual = VisualDefinition.objects.get()
        source = DataSource.objects.create(
            name="Artwork", type="local", source_url="file:///artwork", enabled=True
        )
        grant(self.user, VisualDefinition, "view", "change")
        grant(self.user, DataSource, "view")

        response = self.client.post(
            reverse(DEFINITION_EDIT_URL, args=[visual.pk]), {"key_owner": source.pk}
        )

        self.assertEqual(response.status_code, 302)
        visual.refresh_from_db()
        self.assertEqual(visual.key_owner, source)
        self.assertEqual(
            ObjectChange.objects.using("default")
            .filter(changed_object_id=visual.pk, action="update")
            .count(),
            1,
        )

    def test_the_definition_change_log_reads_main_in_a_branch(self):
        del self.client.cookies["active_branch"]
        self.upload()
        self.client.cookies["active_branch"] = self.branch.schema_id
        visual = VisualDefinition.objects.get()
        grant(self.user, VisualDefinition, "view")
        grant(self.user, ObjectChange, "view")
        change = ObjectChange.objects.using("default").get(
            changed_object_type=ObjectType.objects.get_for_model(VisualDefinition),
            changed_object_id=visual.pk,
        )

        response = self.client.get(reverse(DEFINITION_CHANGELOG_URL, args=[visual.pk]))

        self.assertContains(response, change.get_absolute_url())
        self.assertContains(response, self.branch.name)


class ActivationInBranchTests(ArtworkBranchTestCase):
    def setUp(self):
        # The branch copies main when it is provisioned.
        self.device_type = DeviceType.objects.create(
            manufacturer=Manufacturer.objects.create(name="ACME", slug="acme"),
            model="SW-24",
            slug="sw-24",
        )
        super().setUp()

    def test_an_activation_in_a_branch_changes_main_at_the_merge(self):
        from netbox_branching.utilities import activate_branch

        self.upload()
        revision = DefinitionRevision.objects.get()
        grant(self.user, Association, "view", "add")
        grant(self.user, DeviceType, "view")

        # The review and the activation run in the branch of the cookie.
        response = activate_through_review(self.client, (revision, self.device_type))

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Association.objects.exists())
        with activate_branch(self.branch):
            self.assertEqual(Association.objects.get().revision_id, revision.pk)

        self.branch.merge(user=self.user)

        self.assertEqual(Association.objects.get().device_type, self.device_type)

    def test_a_revision_active_only_in_a_branch_cannot_be_deleted_in_main(self):
        self.upload()
        revision = DefinitionRevision.objects.get()
        grant(self.user, Association, "view", "add")
        grant(self.user, DeviceType, "view")
        grant(self.user, DefinitionRevision, "delete")
        activation = activate_through_review(self.client, (revision, self.device_type))
        self.assertEqual(activation.status_code, 302)
        del self.client.cookies["active_branch"]

        page = self.client.post(
            reverse(REVISION_DELETE_URL, args=[revision.pk]),
            {"confirm": True},
            follow=True,
        )
        token = Token.objects.create(user=self.user)
        api = self.client.delete(
            reverse(REVISION_API_URL, args=[revision.pk]),
            HTTP_AUTHORIZATION=f"Bearer {TOKEN_PREFIX}{token.key}.{token.token}",
        )

        self.assertContains(page, "Unable to delete")
        self.assertContains(page, self.branch.get_absolute_url())
        self.assertEqual(api.status_code, 409)
        self.assertIn(f"{self.branch} ({self.branch.pk})", api.json()["detail"])
        self.assertTrue(DefinitionRevision.objects.filter(pk=revision.pk).exists())

    def test_a_revision_that_no_branch_uses_can_be_deleted(self):
        self.upload()
        revision = DefinitionRevision.objects.get()
        grant(self.user, DefinitionRevision, "delete")
        del self.client.cookies["active_branch"]

        response = self.client.post(
            reverse(REVISION_DELETE_URL, args=[revision.pk]), {"confirm": True}
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(DefinitionRevision.objects.filter(pk=revision.pk).exists())

    def test_a_rejected_activation_in_a_branch_leaves_no_change_record(self):
        from netbox_branching.models import ChangeDiff
        from netbox_branching.utilities import activate_branch

        self.upload()
        revision = DefinitionRevision.objects.get()
        grant(
            self.user,
            Association,
            "view",
            "add",
            constraints={"device_type__model": "Other"},
        )
        grant(self.user, DeviceType, "view")

        response = activate_through_review(self.client, (revision, self.device_type))

        self.assertEqual(response.status_code, 403)
        with activate_branch(self.branch):
            self.assertFalse(
                Association.objects.filter(device_type=self.device_type).exists()
            )
        self.assertFalse(
            ChangeDiff.objects.using("default")
            .filter(object_type=ObjectType.objects.get_for_model(Association))
            .exists()
        )


class BindingOverrideInBranchTests(ArtworkBranchTestCase):
    def setUp(self):
        # The branch copies main, with the artwork and its associations, when it is provisioned.
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media)
        media_override = override_settings(MEDIA_ROOT=media)
        media_override.enable()
        self.addCleanup(media_override.disable)
        admin = User.objects.create_user(username="admin", is_superuser=True)
        [imported] = import_bundle(
            admin, "sw24.zip", io.BytesIO(bundle(definition(DEVICE_DIR, DEVICE_FILES)))
        )
        manufacturer = Manufacturer.objects.create(name="ACME", slug="acme")
        self.device_type, other_type = (
            DeviceType.objects.create(
                manufacturer=manufacturer, model=model, slug=model
            )
            for model in ("SW-24", "SW-48")
        )
        for device_type in (self.device_type, other_type):
            Association.objects.create(
                device_type=device_type, revision=imported.revision
            )
        self.stored = [
            BindingOverride.objects.create(
                device_type=other_type,
                definition=imported.revision.definition,
                connector_key=key,
                bindings=[],
            )
            for key in ("con-eth1", "con-psu")
        ]
        super().setUp()
        grant(self.user, DeviceType, "view")

    def add_override(self):
        return self.client.post(
            reverse(OVERRIDE_ADD_URL),
            {
                "device_type": self.device_type.pk,
                "connector_key": "con-eth1",
                **rows(("interface", "eth2")),
            },
        )

    def change_diffs(self):
        from netbox_branching.models import ChangeDiff

        return ChangeDiff.objects.using("default").filter(
            object_type=ObjectType.objects.get_for_model(BindingOverride)
        )

    def test_an_override_in_a_branch_changes_main_at_the_merge(self):
        from netbox_branching.utilities import activate_branch

        grant(self.user, BindingOverride, "view", "add")

        response = self.add_override()

        added = BindingOverride.objects.filter(device_type=self.device_type)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(added.exists())
        with activate_branch(self.branch):
            self.assertTrue(added.exists())

        self.branch.merge(user=self.user)

        self.assertEqual(added.get().connector_key, "con-eth1")

    def test_a_rejected_override_in_a_branch_leaves_no_change_record(self):
        grant(
            self.user,
            BindingOverride,
            "view",
            "add",
            constraints={"connector_key": "con-psu"},
        )

        response = self.add_override()

        self.assertContains(response, "permissions violation")
        self.assertFalse(self.change_diffs().exists())

    def test_a_failed_bulk_api_change_in_a_branch_leaves_no_change_record(self):
        grant(self.user, BindingOverride, "view", "change")
        token = Token.objects.create(user=self.user)
        api = APIClient()
        api.credentials(
            HTTP_AUTHORIZATION=f"Bearer {TOKEN_PREFIX}{token.key}.{token.token}",
            HTTP_X_NETBOX_BRANCH=self.branch.schema_id,
        )
        first, second = self.stored

        response = api.patch(
            reverse(OVERRIDE_API_LIST),
            [
                {"id": first.pk, "bindings": [{"component": "interface", "name": "a"}]},
                {"id": second.pk, "bindings": [{"component": "fan", "name": "b"}]},
            ],
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.content[:500])
        self.assertFalse(self.change_diffs().exists())
