"""Concurrent apply tests: each thread has its own PostgreSQL connection and transaction."""

import shutil
import tempfile
import threading
import time
from pathlib import Path

from core.models import DataFile, DataSource, ObjectChange, ObjectType
from dcim.models import DeviceType, Manufacturer
from django.db import connection, connections, transaction
from django.db.models.signals import post_save
from django.test import Client, TransactionTestCase
from django.urls import reverse
from users.models import ObjectPermission, User

from netbox_device_view import datasource_import
from netbox_device_view.layout import parse_yaml, render_css
from netbox_device_view.models import DeviceView, RenderMode

from .test_datasource_import import APPLY_URL, EXAMPLE_PATH, EXAMPLE_YAML, REVIEW_URL

WAIT_SECONDS = 20
OTHER_YAML = EXAMPLE_YAML.read_text().replace("blank: 14", "blank: 13")


class ConcurrentApplyTests(TransactionTestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo)
        self.write_file(EXAMPLE_PATH, EXAMPLE_YAML.read_bytes())
        self.write_file("examples/yaml/Cisco/Other.yaml", OTHER_YAML.encode())
        self.source = DataSource.objects.create(
            name="Layouts", type="local", source_url=f"file://{self.repo}"
        )
        self.source.sync()
        manufacturer = Manufacturer.objects.create(name="Cisco", slug="cisco")
        self.device_type = DeviceType.objects.create(
            manufacturer=manufacturer, model="C9300-24T", slug="c9300-24t"
        )
        user = User.objects.create_user(username="importer")
        for model in (DataSource, DataFile, Manufacturer, DeviceType, DeviceView):
            permission = ObjectPermission.objects.create(
                name=f"{model._meta.model_name}-all",
                actions=["view", "add", "change"],
            )
            permission.users.add(user)
            permission.object_types.add(ObjectType.objects.get_for_model(model))
        self.clients = [Client(), Client()]
        for client in self.clients:
            client.force_login(user)
        self.paused = threading.Event()
        self.release = threading.Event()
        self.addCleanup(self.release.set)

    def write_file(self, path, content):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    def token(self, path, replace=False):
        datafile = self.source.datafiles.get(path=path)
        data = {
            "source": self.source.pk,
            "prefix": "examples/",
            "step": "review",
            "file": [datafile.pk],
            f"device_type_{datafile.pk}": self.device_type.pk,
        }
        if replace:
            data[f"replace_{datafile.pk}"] = "on"
        token = self.clients[0].post(reverse(REVIEW_URL), data).context["review"].token
        self.assertTrue(token)
        return token

    def pause_saves_in(self, thread_name):
        """Hold the named thread's transaction open after its device view write."""

        def pause(sender, **kwargs):
            if threading.current_thread().name == thread_name:
                self.paused.set()
                self.release.wait(WAIT_SECONDS)

        post_save.connect(pause, sender=DeviceView, weak=False)
        self.addCleanup(post_save.disconnect, pause, sender=DeviceView)

    def start(self, name, target):
        results = {}

        def run():
            try:
                results["value"] = target()
            finally:
                connections.close_all()

        thread = threading.Thread(target=run, name=name)
        thread.start()
        self.addCleanup(thread.join, WAIT_SECONDS)
        return thread, results

    def apply_in_thread(self, name, client, token):
        return self.start(
            name, lambda: client.post(reverse(APPLY_URL), {"token": token})
        )

    def wait_for_lock_wait(self, statement):
        """Wait until a backend running a statement LIKE the pattern waits for a lock."""
        deadline = time.monotonic() + WAIT_SECONDS
        while time.monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                    "AND query LIKE %s",
                    [statement],
                )
                if cursor.fetchone()[0]:
                    return
            time.sleep(0.05)
        self.fail("no transaction waited for a lock")

    def finish(self, thread, results):
        thread.join(WAIT_SECONDS)
        self.assertFalse(thread.is_alive())
        return results["value"]

    def test_two_concurrent_creates_for_one_target(self):
        first_token = self.token(EXAMPLE_PATH)
        second_token = self.token("examples/yaml/Cisco/Other.yaml")
        self.pause_saves_in("first")
        first = self.apply_in_thread("first", self.clients[0], first_token)
        self.assertTrue(self.paused.wait(WAIT_SECONDS))
        second = self.apply_in_thread("second", self.clients[1], second_token)
        self.wait_for_lock_wait('%FROM "dcim_devicetype"%FOR NO KEY UPDATE%')
        self.release.set()

        self.assertContains(self.finish(*first), "Created: 1")
        response = self.finish(*second)
        self.assertContains(response, datasource_import.STALE_TARGET, status_code=400)
        self.assertEqual(DeviceView.objects.get().yaml_layout, EXAMPLE_YAML.read_text())
        self.assertEqual(
            list(
                ObjectChange.objects.filter(
                    changed_object_type=ObjectType.objects.get_for_model(DeviceView)
                ).values_list("action", flat=True)
            ),
            ["create"],
        )

    def test_edit_during_apply_is_not_overwritten(self):
        device_view = DeviceView.objects.create(
            device_type=self.device_type,
            yaml_layout=OTHER_YAML,
            grid_template_area=render_css(parse_yaml(OTHER_YAML)),
            render_mode=RenderMode.SVG,
        )
        token = self.token(EXAMPLE_PATH, replace=True)
        edited = OTHER_YAML.replace("blank: 13", "blank: 12")

        def edit():
            with transaction.atomic():
                record = DeviceView.objects.get(pk=device_view.pk)
                record.yaml_layout = edited
                record.save()

        self.pause_saves_in("edit")
        editor = self.start("edit", edit)
        self.assertTrue(self.paused.wait(WAIT_SECONDS))
        applier = self.apply_in_thread("apply", self.clients[0], token)
        self.wait_for_lock_wait('%FROM "netbox_device_view_deviceview"%FOR UPDATE%')
        self.release.set()

        self.finish(*editor)
        response = self.finish(*applier)
        self.assertContains(response, datasource_import.STALE_TARGET, status_code=400)
        device_view.refresh_from_db()
        self.assertEqual(device_view.yaml_layout, edited)

    def test_create_from_another_code_path_during_apply(self):
        token = self.token(EXAMPLE_PATH)

        def create():
            with transaction.atomic():
                DeviceView.objects.create(
                    device_type=self.device_type, grid_template_area="x"
                )

        self.pause_saves_in("ui")
        creator = self.start("ui", create)
        self.assertTrue(self.paused.wait(WAIT_SECONDS))
        applier = self.apply_in_thread("apply", self.clients[0], token)
        self.wait_for_lock_wait('INSERT INTO "netbox_device_view_deviceview"%')
        self.release.set()

        self.finish(*creator)
        response = self.finish(*applier)
        self.assertContains(
            response, datasource_import.CONCURRENT_CREATE, status_code=400
        )
        self.assertEqual(DeviceView.objects.get().grid_template_area, "x")
