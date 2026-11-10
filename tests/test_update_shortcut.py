"""The artwork-update shortcut on the device page opens the review of the newer revisions of the device and its installed modules."""

from dcim.models import Module, ModuleBay, ModuleType, Site
from django.utils.html import escape
from netbox.registry import registry
from users.models import ObjectPermission

from netbox_device_view.artwork import pins, review
from netbox_device_view.models import Association, InstancePin

from .permissions import grant
from .test_activation_review import NEW_MODULE, ReviewTestCase
from .test_artwork_activation import NEWER, review_url, token_of

SHORTCUT = "Review artwork update"
PLUGIN = "netbox_device_view."


class UpdateShortcutTests(ReviewTestCase):
    def page(self, device=None):
        response = self.client.get((device or self.device).get_absolute_url())
        self.assertEqual(response.status_code, 200)
        return response

    def assertShortcut(self, response, *pairs, device=None):
        url = review_url(*pairs, return_url=(device or self.device).get_absolute_url())
        self.assertContains(response, f'href="{escape(url)}"', count=1)
        return url

    def test_the_shortcut_opens_the_review_of_the_newer_revision_with_its_full_impact(
        self,
    ):
        sw2 = self.make_device("sw2")
        newer = self.import_files(NEWER)

        url = self.assertShortcut(self.page(), (newer, self.device_type))

        response = self.client.get(url)
        [target] = response.context["review"].targets
        self.assertEqual(target.pair.revision, newer)
        self.assertEqual(target.instances, (self.device, sw2))

    def test_no_shortcut_without_a_newer_revision(self):
        self.assertNotContains(self.page(), SHORTCUT)
        newer = self.import_files(NEWER)
        Association.objects.filter(pk=self.association.pk).update(revision=newer)

        self.assertNotContains(self.page(), SHORTCUT)

    def test_no_shortcut_for_a_type_without_artwork(self):
        self.association.delete()
        self.import_files(NEWER)

        self.assertNotContains(self.page(), SHORTCUT)

    def test_the_shortcut_offers_only_the_updates_that_the_user_can_review(self):
        self.activate_module_type()
        self.install()
        newer = self.import_files(NEWER)
        module_newer = self.import_files(NEW_MODULE, "ACME/NM-2X")
        device_pair, module_pair = (newer, self.device_type), (
            module_newer,
            self.module_type,
        )
        for label, change, pairs in (
            # Neither add nor change permission on associations.
            ("netbox_device_view.association", {"actions": ["view"]}, ()),
            ("netbox_device_view.association", {"constraints": {"pk": 0}}, ()),
            (
                "netbox_device_view.definitionrevision",
                {"constraints": {"pk": self.revision.pk}},
                (),
            ),
            ("dcim.devicetype", {"constraints": {"pk": 0}}, (module_pair,)),
            ("dcim.moduletype", {"constraints": {"pk": 0}}, (device_pair,)),
            ("dcim.module", {"constraints": {"pk": 0}}, (device_pair,)),
        ):
            with self.subTest(label=label, change=change):
                permissions = ObjectPermission.objects.filter(
                    name__startswith=f"{label} "
                )
                saved = list(permissions.values("pk", "actions", "constraints"))
                permissions.update(**change)
                try:
                    if pairs:
                        self.assertShortcut(self.page(), *pairs)
                    else:
                        self.assertNotContains(self.page(), SHORTCUT)
                finally:
                    for row in saved:
                        ObjectPermission.objects.filter(pk=row.pop("pk")).update(**row)
        self.assertShortcut(self.page(), device_pair, module_pair)

    def test_the_shortcut_reviews_the_installed_modules_once_per_type(self):
        self.activate_module_type()
        self.install(), self.install(name="Bay 2", position="2")
        module_newer = self.import_files(NEW_MODULE, "ACME/NM-2X")
        self.assertShortcut(self.page(), (module_newer, self.module_type))
        newer = self.import_files(NEWER)

        url = self.assertShortcut(
            self.page(), (newer, self.device_type), (module_newer, self.module_type)
        )

        self.assertEqual(
            {t.pair.revision for t in self.client.get(url).context["review"].targets},
            {newer, module_newer},
        )

    def test_a_device_with_more_updates_than_a_review_holds_offers_the_first_ones(
        self,
    ):
        module_newer = self.import_files(NEW_MODULE, "ACME/NM-2X")
        types = []
        for n in range(review.MAX_PAIRS + 1):
            module_type = ModuleType.objects.create(
                manufacturer=self.manufacturer, model=f"NM-{n:03}"
            )
            Association.objects.create(
                module_type=module_type, revision=self.module_revision
            )
            Module.objects.create(
                device=self.device,
                module_bay=ModuleBay.objects.create(
                    device=self.device, name=f"Bay {n}", position=str(n)
                ),
                module_type=module_type,
            )
            types.append(module_type)

        url = self.assertShortcut(
            self.page(), *((module_newer, t) for t in types[: review.MAX_PAIRS])
        )

        response = self.client.get(url)
        self.assertEqual(len(response.context["review"].targets), review.MAX_PAIRS)

    def test_a_pinned_part_gives_no_shortcut(self):
        self.activate_module_type()
        first = self.install()
        self.import_files(NEWER)
        module_newer = self.import_files(NEW_MODULE, "ACME/NM-2X")
        pins.save(self.admin, InstancePin(device=self.device, revision=self.revision))
        pins.save(self.admin, InstancePin(module=first, revision=self.module_revision))
        self.assertNotContains(self.page(), SHORTCUT)

        # An unpinned module of the same type shows the type revision.
        self.install(name="Bay 2", position="2")

        self.assertShortcut(self.page(), (module_newer, self.module_type))

    def test_the_review_returns_to_the_device_after_the_activation(self):
        newer = self.import_files(NEWER)
        url = self.assertShortcut(self.page(), (newer, self.device_type))
        response = self.client.get(url)

        applied = self.client.post(url, {"token": token_of(response)})

        self.assertRedirects(applied, self.device.get_absolute_url())
        self.association.refresh_from_db()
        self.assertEqual(self.association.revision, newer)
        self.assertNotContains(self.page(), SHORTCUT)

    def test_a_page_of_another_object_renders_without_plugin_content(self):
        self.import_files(NEWER)
        grant(self.user, Site, "view")

        for page in (self.association, self.site):
            with self.subTest(page=type(page).__name__):
                response = self.client.get(page.get_absolute_url())

                self.assertEqual(response.status_code, 200)
                self.assertNotContains(
                    response, "An error occurred when loading content"
                )
                self.assertNotContains(response, SHORTCUT)

    def test_the_plugin_adds_no_device_tab_for_updates(self):
        # A registered view is a class or a dotted path.
        modules = {
            v["name"]: getattr(v["view"], "__module__", v["view"])
            for v in registry["views"]["dcim"]["device"]
        }
        self.assertEqual(
            [name for name, module in modules.items() if module.startswith(PLUGIN)],
            ["deviceview"],
        )
