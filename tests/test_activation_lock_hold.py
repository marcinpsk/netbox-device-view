"""The activation composes its review before it takes any lock, and its lock waits have a bound."""

import time
from contextlib import contextmanager
from unittest import mock

from dcim.models import ModuleBay, RearPort
from django.db import connection, transaction

from netbox_device_view.artwork import overrides, pins, review
from netbox_device_view.artwork.type_lock import lock_bulk_writes
from netbox_device_view.models import (
    BindingOverride,
    DefinitionRevision,
    InstancePin,
    RevisionFile,
)

from .test_activation_review_concurrency import (
    WAIT_SECONDS,
    ConcurrentActivationTestCase,
    lock_table,
)
from .test_artwork_activation import REVIEW_AGAIN

# A short lock timeout keeps the tests of the timeout fast.
SHORT_LOCK_TIMEOUT_MS = 200


def blocked_backends():
    """Return the process IDs of the backends whose lock waits this connection blocks."""
    with connection.cursor() as cursor:
        # In a transaction PostgreSQL keeps its first pg_stat_activity snapshot, without later backends.
        cursor.execute("SELECT pg_stat_clear_snapshot()")
        # pg_stat_activity shows the whole server, so only the waits that this backend causes count.
        cursor.execute(
            "SELECT pid FROM pg_stat_activity"
            " WHERE pg_backend_pid() = ANY(pg_blocking_pids(pid))"
        )
        return [pid for (pid,) in cursor.fetchall()]


def held_locks(pid):
    """Return the advisory locks and the transaction ID locks that the backend ``pid`` holds."""
    with connection.cursor() as cursor:
        # A row lock gives the transaction an ID, so a backend without one holds no row lock.
        cursor.execute(
            "SELECT locktype FROM pg_locks WHERE pid = %s AND granted"
            " AND locktype IN ('advisory', 'transactionid')",
            [pid],
        )
        return [locktype for (locktype,) in cursor.fetchall()]


class LockHoldTestCase(ConcurrentActivationTestCase):
    def wait_until_blocked(self, thread):
        """Return the process ID of the backend that waits for a lock of this connection."""
        deadline = time.monotonic() + WAIT_SECONDS
        while not (pids := blocked_backends()):
            self.assertTrue(thread.is_alive(), "the thread did not wait")
            self.assertLess(time.monotonic(), deadline, "no transaction waited")
            time.sleep(0.05)
        [pid] = pids
        return pid

    @contextmanager
    def composing(self):
        """Start an activation and hold it in its composition; yield the thread, its results and its backend."""
        token = self.token()
        with transaction.atomic():
            # The composition reads the artwork files; nothing else of the activation reads them.
            lock_table(RevisionFile)
            thread, results = self.activate_in_thread(token)
            yield thread, results, self.wait_until_blocked(thread)

    @contextmanager
    def composed(self):
        """Start an activation and hold it at its first lock, after its composition; yield the thread and its results."""
        token = self.token()
        with transaction.atomic():
            lock_bulk_writes("default")
            thread, results = self.activate_in_thread(token)
            self.wait_until_blocked(thread)
            yield thread, results

    def assertRejected(self, activation, message):
        response = self.finish(*activation)
        self.assertContains(response, message, status_code=409)
        self.assertContains(response, "No association changed.", status_code=409)
        self.assertUnchanged()


class CompositionTests(LockHoldTestCase):
    def test_the_activation_holds_no_lock_while_it_composes(self):
        with self.composing() as (thread, results, pid):
            self.assertEqual(held_locks(pid), [])

        self.assertEqual(self.finish(thread, results).status_code, 302)
        self.assertActivated()

    def test_a_component_write_does_not_wait_while_the_activation_composes(self):
        def write():
            with transaction.atomic():
                self.eth1.description = "uplink"
                self.eth1.save()

        with self.composing() as activation:
            # The writer ends while the activation composes the device of the component.
            self.finish(*self.start("writer", write))

        # A description is not in the fingerprint, so the activation applies.
        self.assertEqual(self.finish(*activation[:2]).status_code, 302)
        self.assertActivated()
        self.eth1.refresh_from_db()
        self.assertEqual(self.eth1.description, "uplink")

    def test_a_delete_of_the_candidate_while_the_activation_composes_rejects_it(self):
        candidate = self.newer.pk
        with self.composing() as activation:
            # The composition then reads a candidate without its files.
            self.newer.delete()

        self.assertRejected(activation[:2], review.CHANGED)
        self.assertFalse(DefinitionRevision.objects.filter(pk=candidate).exists())


class ChangeAfterTheCompositionTests(LockHoldTestCase):
    """A change that commits after the composition and before the locks rejects the activation."""

    def activation_after_its_composition(self, change):
        with self.composed() as activation:
            change()

        self.assertRejected(activation, REVIEW_AGAIN)

    def test_a_rename_into_a_binding_lookup(self):
        self.activation_after_its_composition(
            lambda: RearPort.objects.filter(pk=self.spare.pk).update(name="R1")
        )

    def test_a_module_bay_position_change(self):
        self.activation_after_its_composition(
            lambda: ModuleBay.objects.filter(pk=self.bay.pk).update(position="2")
        )

    def test_a_new_device_of_the_type(self):
        self.activation_after_its_composition(lambda: self.make_device("sw9"))

    def test_a_pin(self):
        self.activation_after_its_composition(
            lambda: pins.save(
                self.admin, InstancePin(device=self.device, revision=self.revision)
            )
        )

        self.assertTrue(InstancePin.objects.filter(device=self.device).exists())

    def test_an_override(self):
        self.activation_after_its_composition(
            lambda: overrides.save(
                self.admin,
                BindingOverride(
                    device_type=self.device_type, connector_key="con-psu", bindings=[]
                ),
            )
        )


@mock.patch.object(review, "LOCK_TIMEOUT_MS", SHORT_LOCK_TIMEOUT_MS)
class LockTimeoutTests(LockHoldTestCase):
    def test_an_activation_that_waits_too_long_for_the_bulk_lock_writes_nothing(self):
        token = self.token()
        with transaction.atomic():
            lock_bulk_writes("default")
            activation = self.activate_in_thread(token)
            self.assertRejected(activation, review.LOCKED)

    def test_an_activation_that_waits_too_long_for_a_component_writes_nothing(self):
        token = self.token()
        with transaction.atomic():
            # The update holds the row until the commit; the activation locks it FOR SHARE.
            self.eth1.description = "uplink"
            self.eth1.save()
            activation = self.activate_in_thread(token)
            self.assertRejected(activation, review.LOCKED)
