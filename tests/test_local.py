"""Tests for the FreeBSD local zvol connector."""

import unittest

from os_brick import exception
from os_brick.initiator import initiator_connector
from oslo_utils import importutils

import os_brick_freebsd
from os_brick_freebsd import local

PATH = '/dev/zvol/zroot/cinder/volume-1'
PROPS = {'device_path': PATH, 'volume_id': '1'}


class FakeExecute:
    """Record commands and answer diskinfo."""

    def __init__(self):
        """Start with no recorded commands."""
        self.calls = []

    def __call__(self, *cmd, **kwargs):
        """Run one fake command."""
        self.calls.append(cmd)
        if cmd[0] == 'diskinfo':
            return f'{cmd[1]}\t512\t2147483648\t4194304\t0\t0\n', ''
        return '', ''


class MappingTest(unittest.TestCase):
    """Protocol mapping read by the os-brick connector factory."""

    def test_zfs_local_maps_to_the_connector(self):
        """Resolve the entry for the cinder-zfs-driver volume type."""
        path = os_brick_freebsd.CONNECTORS['ZFS_LOCAL']
        self.assertIs(
            importutils.import_class(path), local.FreeBSDLocalConnector
        )

    def test_the_protocol_is_the_volume_type_in_upper_case(self):
        """The factory looks a volume type up in upper case."""
        self.assertIn('zfs_local'.upper(), os_brick_freebsd.CONNECTORS)


class LocalConnectorTest(unittest.TestCase):
    """Tests for attaching a zvol on its own host."""

    def setUp(self):
        """Build a connector around a fake executor."""
        self.execute = FakeExecute()
        self.connector = local.FreeBSDLocalConnector(
            None, execute=self.execute
        )

    def test_is_an_os_brick_connector(self):
        """The class satisfies the os-brick connector interface."""
        self.assertIsInstance(
            self.connector, initiator_connector.InitiatorConnector
        )

    def test_properties_are_empty(self):
        """No initiator is reported for a local device."""
        props = local.FreeBSDLocalConnector.get_connector_properties(None)
        self.assertEqual({}, props)

    def test_connect_returns_the_named_device(self):
        """Connecting runs nothing and returns the device path."""
        device = self.connector.connect_volume(PROPS)
        self.assertEqual({'type': 'block', 'path': PATH}, device)
        self.assertEqual([], self.execute.calls)

    def test_connect_refuses_a_connection_without_a_device(self):
        """A connection that names no device is an error."""
        with self.assertRaises(exception.BrickException):
            self.connector.connect_volume({'volume_id': '1'})

    def test_disconnect_runs_nothing(self):
        """Disconnecting leaves the device alone."""
        self.connector.disconnect_volume(PROPS, {'path': PATH})
        self.assertEqual([], self.execute.calls)

    def test_volume_paths_is_the_named_device(self):
        """The volume path is the one the connection names."""
        self.assertEqual([PATH], self.connector.get_volume_paths(PROPS))

    def test_extend_returns_the_device_size(self):
        """Extending reads the new size from diskinfo."""
        self.assertEqual(2147483648, self.connector.extend_volume(PROPS))
        self.assertEqual([('diskinfo', PATH)], self.execute.calls)
