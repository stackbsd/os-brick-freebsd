"""Tests for the FreeBSD iSCSI connector."""

import json
import unittest
from unittest import mock

from os_brick import exception
from oslo_concurrency import processutils

from os_brick_freebsd import iscsi

PORTAL = '192.0.2.10:3260'
IQN = 'iqn.2010-10.org.openstack:volume-1'
PROPS = {
    'target_discovered': False,
    'target_portal': PORTAL,
    'target_iqn': IQN,
    'target_lun': 0,
    'volume_id': '1',
}
CHAP_PROPS = dict(
    PROPS,
    auth_method='CHAP',
    auth_username='chapuser',
    auth_password='chapsecret123',
)


def listing(*sessions):
    """Return iscsictl -L json output for the given sessions."""
    body = {'session': list(sessions)} if sessions else {}
    return json.dumps({'__version': '1', 'iscsictl': body})


def session(luns=((0, 'da3'),), state='Connected', iqn=IQN):
    """Return one session entry as iscsictl lists it."""
    entry = {'name': iqn, 'portal': PORTAL, 'state': state}
    if luns:
        entry['devices'] = {
            'lun': [{'lun': lun, 'device': dev} for lun, dev in luns],
            'scbus': 3,
            'bus': 0,
            'target': 0,
        }
    return entry


class FakeExecute:
    """Record commands and answer iscsictl -L from a queue of listings."""

    def __init__(self, *listings, fail=None):
        """Store the listings to return and an optional failing verb."""
        self.listings = list(listings)
        self.fail = fail
        self.calls = []
        self.conf = None

    def __call__(self, *cmd, **kwargs):
        """Run one fake command."""
        self.calls.append(cmd)
        if self.fail is not None and self.fail in cmd:
            raise processutils.ProcessExecutionError(cmd=' '.join(cmd))
        if cmd[:4] == ('iscsictl', '--libxo', 'json', '-L'):
            if len(self.listings) > 1:
                return self.listings.pop(0), ''
            return self.listings[0], ''
        if cmd[:2] == ('iscsictl', '-A'):
            with open(cmd[cmd.index('-c') + 1]) as f:
                self.conf = f.read()
        if cmd[0] == 'diskinfo':
            return f'{cmd[1]}\t512\t21474836480\t41943040\t0\t0\n', ''
        return '', ''

    def verbs(self):
        """Return the commands that were not session listings."""
        return [c for c in self.calls if '-L' not in c]


class ConnectorTestCase(unittest.TestCase):
    """Shared fixture building a connector around a fake executor."""

    def setUp(self):
        """Patch out sleeps and the external lock."""
        sleep = mock.patch.object(iscsi.time, 'sleep')
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)
        lock = mock.patch('oslo_concurrency.lockutils.lock')
        lock.start()
        self.addCleanup(lock.stop)

    def connector(self, execute):
        """Return a connector using the fake executor."""
        return iscsi.FreeBSDISCSIConnector(
            None, execute=execute, device_scan_attempts=3
        )


class RenderTest(unittest.TestCase):
    """Tests for the iscsi.conf section."""

    def test_without_chap(self):
        """A target without CHAP has no auth lines."""
        text = iscsi.render_conf(PORTAL, IQN)
        self.assertIn(f'TargetAddress = {PORTAL}', text)
        self.assertIn(f'TargetName = {IQN}', text)
        self.assertNotIn('AuthMethod', text)

    def test_with_chap(self):
        """CHAP credentials are written into the section."""
        text = iscsi.render_conf(PORTAL, IQN, 'chapuser', 'chapsecret123')
        self.assertIn('AuthMethod = CHAP', text)
        self.assertIn('chapIName = chapuser', text)
        self.assertIn('chapSecret = "chapsecret123"', text)

    def test_refuses_unsafe_values(self):
        """Values that could break out of the section are refused."""
        for args in [
            (PORTAL, 'iqn.x }\nevil {'),
            ('192.0.2.10:3260;', IQN),
            (PORTAL, IQN, 'user', 'se"cret'),
        ]:
            with self.assertRaises(exception.BrickException):
                iscsi.render_conf(*args)


class PropertiesTest(unittest.TestCase):
    """Tests for the connector properties."""

    def test_initiator_name_matches_iscsictl_default(self):
        """The initiator is the FreeBSD prefix and the hostname."""
        with mock.patch.object(
            iscsi.socket, 'gethostname', return_value='compute1'
        ):
            props = iscsi.FreeBSDISCSIConnector.get_connector_properties(None)
        self.assertEqual(
            {'initiator': 'iqn.1994-09.org.freebsd:compute1'}, props
        )

    def test_os_type_matches_freebsd_platform(self):
        """The connector matches the sys.platform of FreeBSD hosts."""
        from os_brick import utils

        self.assertTrue(
            utils.os_matches('freebsd15', iscsi.FreeBSDISCSIConnector.os_type)
        )
        self.assertFalse(
            utils.os_matches('linux', iscsi.FreeBSDISCSIConnector.os_type)
        )


class ConnectTest(ConnectorTestCase):
    """Tests for connect_volume."""

    def test_adds_session_and_returns_device(self):
        """A new target is logged in and its LUN device returned."""
        execute = FakeExecute(listing(), listing(session()))
        info = self.connector(execute).connect_volume(PROPS)
        self.assertEqual({'type': 'block', 'path': '/dev/da3'}, info)
        self.assertEqual(
            [('iscsictl', '-A', '-n', 'os-brick')],
            [(c[0], c[1], c[4], c[5]) for c in execute.verbs()],
        )
        self.assertIn(f'TargetName = {IQN}', execute.conf)

    def test_chap_secret_stays_off_the_command_line(self):
        """CHAP credentials go to the config file only."""
        execute = FakeExecute(listing(), listing(session()))
        self.connector(execute).connect_volume(CHAP_PROPS)
        self.assertIn('chapSecret = "chapsecret123"', execute.conf)
        for cmd in execute.calls:
            self.assertNotIn('chapsecret123', ' '.join(cmd))

    def test_config_file_is_removed(self):
        """The temporary config file is gone after the login."""
        execute = FakeExecute(listing(), listing(session()))
        self.connector(execute).connect_volume(PROPS)
        add = execute.verbs()[0]
        path = add[add.index('-c') + 1]
        with self.assertRaises(FileNotFoundError):
            open(path)

    def test_reuses_existing_session(self):
        """An existing session is not added again."""
        execute = FakeExecute(listing(session()))
        info = self.connector(execute).connect_volume(PROPS)
        self.assertEqual('/dev/da3', info['path'])
        self.assertEqual([], execute.verbs())

    def test_waits_for_device(self):
        """The connector polls until the LUN device appears."""
        execute = FakeExecute(
            listing(),
            listing(session(luns=(), state='Connected')),
            listing(session()),
        )
        info = self.connector(execute).connect_volume(PROPS)
        self.assertEqual('/dev/da3', info['path'])
        self.sleep.assert_called_once_with(1)

    def test_gives_up_and_removes_session(self):
        """A target that never attaches is logged out and reported."""
        execute = FakeExecute(
            listing(), listing(session(luns=(), state='Login timed out'))
        )
        with self.assertRaises(exception.VolumeDeviceNotFound) as ctx:
            self.connector(execute).connect_volume(PROPS)
        self.assertIn('Login timed out', str(ctx.exception))
        self.assertEqual(
            ('iscsictl', '-R', '-p', PORTAL, '-t', IQN), execute.verbs()[-1]
        )

    def test_refuses_unknown_auth_method(self):
        """An auth method other than CHAP is refused."""
        execute = FakeExecute(listing())
        props = dict(PROPS, auth_method='RADIUS')
        with self.assertRaises(exception.BrickException):
            self.connector(execute).connect_volume(props)
        self.assertEqual([], execute.verbs())


class DisconnectTest(ConnectorTestCase):
    """Tests for disconnect_volume."""

    def test_removes_session(self):
        """The session carrying only this LUN is logged out."""
        execute = FakeExecute(listing(session()))
        self.connector(execute).disconnect_volume(PROPS, None)
        self.assertEqual(
            [('iscsictl', '-R', '-p', PORTAL, '-t', IQN)], execute.verbs()
        )

    def test_missing_session_is_noop(self):
        """Disconnecting a target with no session does nothing."""
        execute = FakeExecute(listing())
        self.connector(execute).disconnect_volume(PROPS, None)
        self.assertEqual([], execute.verbs())

    def test_keeps_session_with_other_luns(self):
        """A session still carrying other LUNs stays logged in."""
        execute = FakeExecute(listing(session(luns=((0, 'da3'), (1, 'da4')))))
        self.connector(execute).disconnect_volume(PROPS, None)
        self.assertEqual([], execute.verbs())

    def test_logout_failure_raises(self):
        """A failed logout raises without force."""
        execute = FakeExecute(listing(session()), fail='-R')
        with self.assertRaises(processutils.ProcessExecutionError):
            self.connector(execute).disconnect_volume(PROPS, None)

    def test_logout_failure_ignored_with_force(self):
        """A failed logout is ignored with force and ignore_errors."""
        execute = FakeExecute(listing(session()), fail='-R')
        with self.assertLogs(iscsi.LOG.logger, 'WARNING'):
            self.connector(execute).disconnect_volume(
                PROPS, None, force=True, ignore_errors=True
            )


class PathsTest(ConnectorTestCase):
    """Tests for path lookups and extend."""

    def test_volume_paths(self):
        """The attached LUN's device is reported."""
        execute = FakeExecute(listing(session()))
        self.assertEqual(
            ['/dev/da3'], self.connector(execute).get_volume_paths(PROPS)
        )

    def test_volume_paths_when_detached(self):
        """A detached volume has no paths."""
        execute = FakeExecute(listing())
        self.assertEqual([], self.connector(execute).get_volume_paths(PROPS))

    def test_all_available_volumes(self):
        """Every LUN of every session is listed."""
        execute = FakeExecute(
            listing(
                session(luns=((0, 'da3'), (1, 'da4'))),
                session(luns=((0, 'da7'),), iqn=IQN + '-b'),
            )
        )
        self.assertEqual(
            ['/dev/da3', '/dev/da4', '/dev/da7'],
            self.connector(execute).get_all_available_volumes(),
        )

    def test_extend_reprobes_and_returns_size(self):
        """Extend reprobes the device and reads its new size."""
        execute = FakeExecute(listing(session()))
        size = self.connector(execute).extend_volume(PROPS)
        self.assertEqual(21474836480, size)
        self.assertEqual(
            [('camcontrol', 'reprobe', 'da3'), ('diskinfo', '/dev/da3')],
            execute.verbs(),
        )

    def test_extend_detached_volume_raises(self):
        """Extending a volume that is not attached is refused."""
        execute = FakeExecute(listing())
        with self.assertRaises(exception.VolumeDeviceNotFound):
            self.connector(execute).extend_volume(PROPS)


if __name__ == '__main__':
    unittest.main()
