"""Request-level tests for the plugin's helper views."""

from django.test import TestCase
from django.urls import reverse
from users.models import User

INTERFACES_URL = "plugins:netbox_device_view:devicetype_interfaces"


class DeviceTypeInterfacesViewTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_user(username="viewer"))

    def test_non_ascii_or_oversized_device_type_id_returns_empty_list(self):
        for value in ("²", "١", "9" * 5000):
            with self.subTest(value=value[:8]):
                response = self.client.get(
                    reverse(INTERFACES_URL), {"device_type_id": value}
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), [])
