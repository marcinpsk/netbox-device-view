"""A delete of a binding override reads the override again after the type lock and the scope lock."""

import io
import shutil
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager

import django_rq
from core.choices import JobStatusChoices
from core.models import Job, ObjectChange, ObjectType
from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.conf import settings
from django.contrib.messages import get_messages
from django.db import connection, transaction
from django.test import Client, RequestFactory, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from netbox.context import current_request, events_queue, query_cache
from netbox.jobs import AsyncViewJob
from rest_framework.test import APIClient
from rq import SimpleWorker
from users.constants import TOKEN_PREFIX
from users.models import Token, User
from utilities.request import copy_safe_request

from netbox_device_view.artwork.importing import import_bundle
from netbox_device_view.artwork.type_lock import lock_bulk_writes, lock_types
from netbox_device_view.models import (
    SCOPE_BUSY,
    SCOPE_TYPE_CHANGED,
    Association,
    BindingOverride,
)
from netbox_device_view.views import BindingOverrideBulkDeleteView

from .artwork_samples import bundle, definition
from .permissions import grant
from .test_artwork_device_view import FILES
from .test_artwork_import_concurrency import backend_pid, blocking_pids
from .test_binding_override_api import DETAIL as API_DETAIL
from .test_binding_override_api import LIST as API_LIST
from .test_binding_override_pages import BULK_DELETE_URL, DELETE_URL

REPLACEMENT = [{"component": "interface", "name": "eth2"}]


class OverrideDeleteTestCase(TransactionTestCase):
    """The delete constraint of the user is on the device status."""

    def setUp(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media)
        media_override = override_settings(MEDIA_ROOT=media)
        media_override.enable()
        self.addCleanup(media_override.disable)
        admin = User.objects.create_user(username="admin", is_superuser=True)
        self.revision = revision = import_bundle(
            admin, "sw24.zip", io.BytesIO(bundle(definition("ACME/SW-24", FILES)))
        )[0].revision
        manufacturer = Manufacturer.objects.create(name="ACME", slug="acme")
        self.device_type = DeviceType.objects.create(
            manufacturer=manufacturer, model="SW-24", slug="sw-24"
        )
        self.other_type = DeviceType.objects.create(
            manufacturer=manufacturer, model="SW-48", slug="sw-48"
        )
        self.device = Device.objects.create(
            name="sw1",
            device_type=self.device_type,
            site=Site.objects.create(name="Site", slug="site"),
            role=DeviceRole.objects.create(name="Role", slug="role"),
            status="active",
        )
        self.site, self.role = self.device.site, self.device.role
        Association.objects.create(device_type=self.device_type, revision=revision)
        self.override = BindingOverride.objects.create(
            device=self.device,
            definition=revision.definition,
            connector_key="con-eth1",
            bindings=REPLACEMENT,
        )
        self.user = User.objects.create_user(username="operator")
        grant(
            self.user,
            BindingOverride,
            "view",
            "delete",
            constraints={"device__status": "active"},
        )
        self.pids = {}

    def browser(self):
        client = Client()
        client.force_login(self.user)
        return client

    def api(self):
        token = Token.objects.create(user=self.user)
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {TOKEN_PREFIX}{token.key}.{token.token}"
        )
        return client

    def wait_until_blocked(self, waiter, holder):
        """Wait until the waiter's backend waits for a lock of the holder's backend."""
        deadline = time.monotonic() + 10
        while waiter not in self.pids or self.pids[holder] not in blocking_pids(
            self.pids[waiter]
        ):
            self.assertLess(
                time.monotonic(),
                deadline,
                f"the {waiter} did not wait for the {holder}",
            )
            time.sleep(0.05)


class DeleteAfterLockTests(OverrideDeleteTestCase):
    """A writer changes the device or the override while the delete waits for the type lock."""

    def delete_while_locked(self, request, change):
        """Send ``request`` while a writer holds the type lock; ``change`` commits with that writer."""
        result = {}

        def run():
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '20s'")
                self.pids["delete"] = backend_pid()
                result["response"] = request()
            except Exception as exc:  # noqa: BLE001  (the test checks the outcome)
                result["error"] = exc
            finally:
                connection.close()

        thread = threading.Thread(target=run)
        self.addCleanup(thread.join, 30)
        with transaction.atomic():
            lock_types([self.device_type], "default")
            self.pids["writer"] = backend_pid()
            thread.start()
            self.wait_until_blocked("delete", "writer")
            change()
        thread.join(30)
        self.assertFalse(thread.is_alive(), "the delete did not end")
        self.assertNotIn("error", result)
        return result["response"]

    def stop_the_device(self):
        Device.objects.filter(pk=self.device.pk).update(status="offline")

    def change_the_bindings(self):
        BindingOverride.objects.filter(pk=self.override.pk).update(bindings=[])

    def change_the_device_type(self):
        Device.objects.filter(pk=self.device.pk).update(device_type=self.other_type)

    def recorded_bindings(self):
        change = ObjectChange.objects.get(
            changed_object_type=ObjectType.objects.get_for_model(BindingOverride),
            action="delete",
        )
        return change.prechange_data["bindings"]

    def page_delete(self):
        client = self.browser()
        return lambda: client.post(
            reverse(DELETE_URL, args=[self.override.pk]), {"confirm": True}
        )

    def page_bulk_delete(self):
        client = self.browser()
        return lambda: client.post(
            reverse(BULK_DELETE_URL),
            {"pk": [self.override.pk], "_confirm": True, "confirm": True},
        )

    def api_delete(self):
        client = self.api()
        return lambda: client.delete(reverse(API_DETAIL, args=[self.override.pk]))

    def api_bulk_delete(self):
        client = self.api()
        return lambda: client.delete(
            reverse(API_LIST), [{"id": self.override.pk}], format="json"
        )

    def test_a_page_delete_checks_the_constraints_on_the_current_device(self):
        response = self.delete_while_locked(self.page_delete(), self.stop_the_device)

        self.assertEqual(response.status_code, 404)
        self.assertTrue(BindingOverride.objects.filter(pk=self.override.pk).exists())

    def test_a_page_bulk_delete_checks_the_constraints_on_the_current_device(self):
        response = self.delete_while_locked(
            self.page_bulk_delete(), self.stop_the_device
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(BindingOverride.objects.filter(pk=self.override.pk).exists())

    def test_an_api_delete_checks_the_constraints_on_the_current_device(self):
        response = self.delete_while_locked(self.api_delete(), self.stop_the_device)

        self.assertEqual(response.status_code, 404)
        self.assertTrue(BindingOverride.objects.filter(pk=self.override.pk).exists())

    def test_an_api_bulk_delete_checks_the_constraints_on_the_current_device(self):
        response = self.delete_while_locked(
            self.api_bulk_delete(), self.stop_the_device
        )

        # NetBox 4.7 reports an id that the constraints exclude; earlier versions skip it.
        self.assertIn(response.status_code, (204, 400))
        self.assertTrue(BindingOverride.objects.filter(pk=self.override.pk).exists())

    def test_a_page_delete_records_the_current_bindings(self):
        response = self.delete_while_locked(
            self.page_delete(), self.change_the_bindings
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.recorded_bindings(), [])

    def test_a_page_bulk_delete_records_the_current_bindings(self):
        response = self.delete_while_locked(
            self.page_bulk_delete(), self.change_the_bindings
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.recorded_bindings(), [])

    def test_an_api_delete_records_the_current_bindings(self):
        response = self.delete_while_locked(self.api_delete(), self.change_the_bindings)

        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.recorded_bindings(), [])

    def test_an_api_bulk_delete_records_the_current_bindings(self):
        response = self.delete_while_locked(
            self.api_bulk_delete(), self.change_the_bindings
        )

        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.recorded_bindings(), [])

    def test_a_page_delete_stops_when_the_device_type_changes(self):
        response = self.delete_while_locked(
            self.page_delete(), self.change_the_device_type
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            [str(m) for m in get_messages(response.wsgi_request)],
            [str(SCOPE_TYPE_CHANGED)],
        )
        self.assertTrue(BindingOverride.objects.filter(pk=self.override.pk).exists())

    def test_an_api_delete_stops_when_the_device_type_changes(self):
        response = self.delete_while_locked(
            self.api_delete(), self.change_the_device_type
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": str(SCOPE_TYPE_CHANGED)})
        self.assertTrue(BindingOverride.objects.filter(pk=self.override.pk).exists())

    def etag(self, client):
        etag = client.get(reverse(API_DETAIL, args=[self.override.pk])).get("ETag")
        if etag is None:
            self.skipTest("This NetBox version has no ETag.")
        return etag

    def test_an_api_delete_with_the_current_etag_removes_the_override(self):
        client = self.api()
        etag = self.etag(client)

        response = client.delete(
            reverse(API_DETAIL, args=[self.override.pk]), HTTP_IF_MATCH=etag
        )

        self.assertEqual(response.status_code, 204)
        self.assertFalse(BindingOverride.objects.exists())

    def test_an_api_delete_checks_the_etag_on_the_current_override(self):
        client = self.api()
        etag = self.etag(client)

        def change():
            BindingOverride.objects.filter(pk=self.override.pk).update(
                bindings=[], last_updated=timezone.now()
            )

        response = self.delete_while_locked(
            lambda: client.delete(
                reverse(API_DETAIL, args=[self.override.pk]), HTTP_IF_MATCH=etag
            ),
            change,
        )

        self.assertEqual(response.status_code, 412)
        self.assertTrue(BindingOverride.objects.filter(pk=self.override.pk).exists())


class BusyScopeTests(OverrideDeleteTestCase):
    """Another connection holds the row of a device, as a NetBox edit of the device does."""

    def setUp(self):
        super().setUp()
        self.hidden_device = Device.objects.create(
            name="sw2",
            device_type=self.device_type,
            site=self.site,
            role=self.role,
            status="offline",
        )
        # The constraints of the user exclude this override.
        self.hidden = BindingOverride.objects.create(
            device=self.hidden_device,
            definition=self.revision.definition,
            connector_key="con-eth1",
            bindings=REPLACEMENT,
        )
        # A private RQ queue, so the burst worker runs only the jobs of this test.
        queue = f"netbox_device_view-test-{uuid.uuid4().hex}"
        # AsyncViewJob has no object, so NetBox maps the model None to its queue.
        self.enterContext(
            override_settings(
                RQ_QUEUES={**settings.RQ_QUEUES, queue: settings.RQ_QUEUES["default"]},
                QUEUE_MAPPINGS={None: queue},
            )
        )
        self.queue = django_rq.get_queue(queue)
        self.addCleanup(self.queue.delete, delete_jobs=True)

    @contextmanager
    def held(self, lock):
        """Run ``lock`` in a transaction of another connection, and hold that transaction."""
        holding, release = threading.Event(), threading.Event()

        def run():
            try:
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute("SET LOCAL lock_timeout = '20s'")
                    self.pids["holder"] = backend_pid()
                    lock()
                    holding.set()
                    release.wait(30)
            finally:
                connection.close()

        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(holding.wait(10), "the other connection took no lock")
            yield
        finally:
            release.set()
            thread.join(30)

    @staticmethod
    def set_lock_timeout(value):
        with connection.cursor() as cursor:
            cursor.execute(f"SET lock_timeout = {value}")

    def busy(self, device):
        """Hold the row lock of ``device`` in another connection."""
        return self.held(
            lambda: list(
                Device.objects.select_for_update()
                .filter(pk=device.pk)
                .values_list("pk")
            )
        )

    def work(self):
        # NetBox 4.5 leaves the request context of a failed view job in this thread.
        for var, value in (
            (current_request, None),
            (events_queue, {}),
            (query_cache, None),
        ):
            self.addCleanup(var.set, value)
        SimpleWorker([self.queue], connection=self.queue.connection).work(burst=True)
        return Job.objects.get()

    def bulk_delete_data(self, **extra):
        return {"pk": [self.override.pk], "_confirm": True, "confirm": True, **extra}

    def test_a_page_bulk_delete_of_all_skips_the_busy_scope_of_an_excluded_override(
        self,
    ):
        with self.busy(self.hidden_device):
            response = self.browser().post(
                reverse(BULK_DELETE_URL), self.bulk_delete_data(_all=True)
            )

        self.assertEqual(response.status_code, 302)
        self.assertNotIn(
            str(SCOPE_BUSY), [str(m) for m in get_messages(response.wsgi_request)]
        )
        self.assertEqual(list(BindingOverride.objects.all()), [self.hidden])

    def test_an_api_bulk_delete_of_an_excluded_override_with_a_busy_scope_is_not_found(
        self,
    ):
        client = self.api()
        unknown = BindingOverride.objects.order_by("-pk").first().pk + 1000

        with self.busy(self.hidden_device):
            excluded = client.delete(
                reverse(API_LIST), [{"id": self.hidden.pk}], format="json"
            )
        missing = client.delete(reverse(API_LIST), [{"id": unknown}], format="json")

        self.assertNotIn(str(SCOPE_BUSY), excluded.content.decode())
        self.assertEqual(excluded.status_code, missing.status_code)
        self.assertTrue(BindingOverride.objects.filter(pk=self.hidden.pk).exists())

    def test_a_background_bulk_delete_is_queued_while_a_scope_is_busy(self):
        with self.busy(self.device):
            response = self.browser().post(
                reverse(BULK_DELETE_URL), self.bulk_delete_data(background_job=True)
            )

        self.assertEqual(response.status_code, 302)
        self.assertNotIn(
            str(SCOPE_BUSY), [str(m) for m in get_messages(response.wsgi_request)]
        )
        job = self.work()
        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED, job.error)
        self.assertEqual(list(BindingOverride.objects.all()), [self.hidden])

    def job_request(self):
        """Return the request copy that BulkDeleteView.post() queues for a background job."""
        request = RequestFactory().post(
            reverse(BULK_DELETE_URL), self.bulk_delete_data(background_job=True)
        )
        request.user = self.user
        request.id = uuid.uuid4()
        return copy_safe_request(request)

    def test_a_background_bulk_delete_logs_a_busy_scope_in_its_job(self):
        AsyncViewJob.enqueue(
            name="Bulk delete",
            user=self.user,
            view_cls=BindingOverrideBulkDeleteView,
            request=self.job_request(),
        )

        with self.busy(self.device):
            job = self.work()

        self.assertEqual(job.status, JobStatusChoices.STATUS_FAILED, job.error)
        self.assertIn(str(SCOPE_BUSY), [entry["message"] for entry in job.log_entries])
        self.assertTrue(BindingOverride.objects.filter(pk=self.override.pk).exists())

    def test_a_background_bulk_delete_is_queued_while_another_bulk_write_runs(self):
        # A wait for a lock fails the request, so the test does not hang.
        self.set_lock_timeout("'5s'")
        self.addCleanup(self.set_lock_timeout, "DEFAULT")

        def hold():
            lock_bulk_writes("default")
            lock_types([self.device_type], "default")

        with self.held(hold):
            response = self.browser().post(
                reverse(BULK_DELETE_URL), self.bulk_delete_data(background_job=True)
            )

        self.assertEqual(response.status_code, 302)
        job = self.work()
        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED, job.error)
        self.assertEqual(list(BindingOverride.objects.all()), [self.hidden])

    def test_a_confirmed_bulk_delete_with_an_invalid_field_takes_the_scope_locks(self):
        client = self.browser()
        result = {}

        def run():
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '20s'")
                self.pids["delete"] = backend_pid()
                result["response"] = client.post(
                    reverse(BULK_DELETE_URL),
                    self.bulk_delete_data(changelog_message="x" * 201),
                )
            except Exception as exc:  # noqa: BLE001  (the test checks the outcome)
                result["error"] = exc
            finally:
                connection.close()

        thread = threading.Thread(target=run)
        self.addCleanup(thread.join, 30)
        with self.held(lambda: lock_types([self.device_type], "default")):
            thread.start()
            self.wait_until_blocked("delete", "holder")
        thread.join(30)

        self.assertNotIn("error", result)
        # NetBox shows the form again for the invalid field.
        self.assertEqual(result["response"].status_code, 200)
        self.assertTrue(BindingOverride.objects.filter(pk=self.override.pk).exists())

    def test_a_background_bulk_delete_reads_the_overrides_again_after_its_locks(self):
        request = self.job_request()
        job = Job.objects.create(
            name="Bulk delete", user=self.user, job_id=uuid.uuid4()
        )
        result = {}

        def run():
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '20s'")
                self.pids["job"] = backend_pid()
                # The RQ worker runs the job with this call.
                AsyncViewJob.handle(
                    job, view_cls=BindingOverrideBulkDeleteView, request=request
                )
            except Exception as exc:  # noqa: BLE001  (the test checks the outcome)
                result["error"] = exc
            finally:
                connection.close()

        thread = threading.Thread(target=run)
        self.addCleanup(thread.join, 30)
        with transaction.atomic():
            lock_types([self.device_type], "default")
            self.pids["writer"] = backend_pid()
            thread.start()
            self.wait_until_blocked("job", "writer")
            Device.objects.filter(pk=self.device.pk).update(status="offline")
        thread.join(30)

        self.assertNotIn("error", result)
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED, job.error)
        self.assertTrue(BindingOverride.objects.filter(pk=self.override.pk).exists())

    def test_a_filtered_bulk_delete_of_all_skips_the_busy_scope_outside_the_filter(
        self,
    ):
        other_device = Device.objects.create(
            name="sw3",
            device_type=self.device_type,
            site=self.site,
            role=self.role,
            status="active",
        )
        # The user can delete this override, but the filter does not select it.
        outside = BindingOverride.objects.create(
            device=other_device,
            definition=self.revision.definition,
            connector_key="con-eth1",
            bindings=REPLACEMENT,
        )
        query = urlencode({"device_id": self.device.pk})

        with self.busy(other_device):
            response = self.browser().post(
                f"{reverse(BULK_DELETE_URL)}?{query}",
                self.bulk_delete_data(_all=True),
            )

        self.assertEqual(response.status_code, 302)
        self.assertNotIn(
            str(SCOPE_BUSY), [str(m) for m in get_messages(response.wsgi_request)]
        )
        self.assertEqual(list(BindingOverride.objects.all()), [self.hidden, outside])
