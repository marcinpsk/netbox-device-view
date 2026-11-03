"""The css_to_yaml command stores only a layout that passes the save validation."""

from io import StringIO

from dcim.models import DeviceType, Manufacturer
from django.core.management import call_command
from django.test import TestCase

from netbox_device_view.layout.parser import parse
from netbox_device_view.models import DeviceView


def css(*areas):
    return '.deviceview.area{grid-template-areas:"' + " ".join(areas) + '";}'


class CssToYamlCommandTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="Mfr", slug="mfr")
        cls.device_type = DeviceType.objects.create(
            manufacturer=manufacturer, model="Switch", slug="switch"
        )

    def convert(self, grid_template_area):
        device_view = DeviceView.objects.create(
            device_type=self.device_type, grid_template_area=grid_template_area
        )
        stdout, stderr = StringIO(), StringIO()
        call_command("css_to_yaml", stdout=stdout, stderr=stderr)
        device_view.refresh_from_db()
        return device_view, stdout.getvalue(), stderr.getvalue()

    def test_a_valid_conversion_is_stored(self):
        device_view, stdout, stderr = self.convert(css("p1", "p2"))
        self.assertEqual(stderr, "")
        self.assertIn("Done: 1 converted", stdout)
        parse(device_view.yaml_layout, strict=True)

    def test_a_conversion_that_breaks_a_save_rule_is_not_stored(self):
        device_view, stdout, stderr = self.convert(css("a" * 65))
        self.assertEqual(device_view.yaml_layout, "")
        self.assertIn("the element key", stderr)
        self.assertIn("Done: 0 converted, 1 errors", stdout)
