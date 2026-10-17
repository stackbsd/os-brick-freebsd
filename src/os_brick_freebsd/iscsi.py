"""FreeBSD iSCSI initiator connector."""

import json
import os
import re
import socket
import tempfile
import time

from os_brick import exception, utils
from os_brick.initiator.connectors import base, base_iscsi
from oslo_concurrency import processutils
from oslo_log import log as logging

LOG = logging.getLogger(__name__)

OS_TYPE_FREEBSD = 'FREEBSD'
DEFAULT_IQN = 'iqn.1994-09.org.freebsd:'
DEV_DIR = '/dev'
NICKNAME = 'os-brick'

# root iscsictl parses the file, so a value failing these is refused
IQN_RE = re.compile(r'^[A-Za-z0-9.:-]+$')
PORTAL_RE = re.compile(r'^[A-Za-z0-9.\[\]:-]+$')
CHAP_RE = re.compile(r'^[A-Za-z0-9_.-]+$')


def checked(value, pattern, what):
    """Return the value if it matches the pattern."""
    if value is None or not pattern.match(value):
        raise exception.BrickException(
            message=f'refusing to render {what} {value!r} into iscsi.conf'
        )
    return value


def initiator_name():
    """Return the initiator name iscsictl uses when none is configured."""
    return DEFAULT_IQN + socket.gethostname()


def render_conf(portal, iqn, username=None, password=None):
    """Return an iscsi.conf section for one target."""
    lines = [
        f'{NICKNAME} {{',
        f'    TargetAddress = {checked(portal, PORTAL_RE, "portal")}',
        f'    TargetName = {checked(iqn, IQN_RE, "target IQN")}',
    ]
    if username is not None:
        lines += [
            '    AuthMethod = CHAP',
            f'    chapIName = {checked(username, CHAP_RE, "CHAP username")}',
            f'    chapSecret = "{checked(password, CHAP_RE, "CHAP secret")}"',
        ]
    lines.append('}')
    return '\n'.join(lines) + '\n'


def session_luns(session):
    """Return the LUN entries iscsictl lists for a session."""
    return session.get('devices', {}).get('lun', [])


class FreeBSDISCSIConnector(base_iscsi.BaseISCSIConnector):
    """iSCSI connector for the FreeBSD initiator driven by iscsictl."""

    os_type = OS_TYPE_FREEBSD

    @staticmethod
    def get_connector_properties(root_helper, *args, **kwargs):
        """Return the initiator name of this host."""
        return {'initiator': initiator_name()}

    def get_search_path(self):
        """Return the directory holding da(4) device nodes."""
        return DEV_DIR

    def check_valid_device(self, path, run_as_root=True):
        """Report whether the first block of the device can be read."""
        return utils.check_valid_device(self, path)

    def _iscsictl(self, *args):
        """Run iscsictl as root and return its stdout."""
        out, _err = self._execute(
            'iscsictl',
            *args,
            run_as_root=True,
            root_helper=self._root_helper,
        )
        return out

    def _sessions(self):
        """Return the sessions iscsictl reports."""
        out = self._iscsictl('--libxo', 'json', '-L')
        return json.loads(out).get('iscsictl', {}).get('session', [])

    def _find_session(self, portal, iqn):
        """Return the session for a portal and target, or None."""
        for session in self._sessions():
            if session.get('name') == iqn and session.get('portal') == portal:
                return session
        return None

    @staticmethod
    def _device_for(session, lun):
        """Return the device path of a LUN on a session, or None."""
        for entry in session_luns(session or {}):
            if int(entry['lun']) == int(lun):
                return os.path.join(DEV_DIR, entry['device'])
        return None

    def _add_session(self, portal, iqn, connection_properties):
        """Add a session for the target from a private config file."""
        method = connection_properties.get('auth_method')
        if method is not None and method.upper() != 'CHAP':
            raise exception.BrickException(
                message=f'unsupported auth_method {method}. '
                'Export the target with CHAP or no authentication.'
            )
        username = password = None
        if method is not None:
            username = connection_properties['auth_username']
            password = connection_properties['auth_password']
        text = render_conf(portal, iqn, username, password)
        # a config file keeps the CHAP secret out of the process list
        handle, path = tempfile.mkstemp(prefix='os-brick-', suffix='.conf')
        try:
            with os.fdopen(handle, 'w') as f:
                f.write(text)
            self._iscsictl('-A', '-c', path, '-n', NICKNAME)
        finally:
            os.unlink(path)

    def _remove_session(self, portal, iqn):
        """Remove the session for a portal and target."""
        self._iscsictl('-R', '-p', portal, '-t', iqn)

    def _wait_for_device(self, portal, iqn, lun):
        """Return the LUN's device path once the session has attached it."""
        state = None
        for attempt in range(self.device_scan_attempts):
            if attempt:
                time.sleep(attempt**2)
            session = self._find_session(portal, iqn)
            path = self._device_for(session, lun)
            if path is not None:
                return path
            state = session and session.get('state')
            LOG.debug(
                'no device for %(iqn)s lun %(lun)s yet (state %(state)s)',
                {'iqn': iqn, 'lun': lun, 'state': state},
            )
        # iscsid retries a failed login forever unless the session goes
        self._remove_session(portal, iqn)
        raise exception.VolumeDeviceNotFound(
            device=f'{iqn} lun {lun} (last state {state})'
        )

    @utils.trace
    @base.synchronized('connect_volume', external=True)
    def connect_volume(self, connection_properties):
        """Log in to the target and return the device for its LUN."""
        portal, iqn, lun = self._get_all_targets(connection_properties)[0]
        if self._find_session(portal, iqn) is None:
            self._add_session(portal, iqn, connection_properties)
        return {
            'type': 'block',
            'path': self._wait_for_device(portal, iqn, lun),
        }

    @utils.trace
    @base.synchronized('connect_volume', external=True)
    def disconnect_volume(
        self,
        connection_properties,
        device_info,
        force=False,
        ignore_errors=False,
    ):
        """Log out of the target unless it still carries other LUNs."""
        portal, iqn, lun = self._get_all_targets(connection_properties)[0]
        session = self._find_session(portal, iqn)
        if session is None:
            return
        if any(int(e['lun']) != int(lun) for e in session_luns(session)):
            LOG.info('staying logged in to %s as other LUNs remain', iqn)
            return
        try:
            self._remove_session(portal, iqn)
        except processutils.ProcessExecutionError:
            if not (force and ignore_errors):
                raise
            LOG.warning('ignoring failed logout from %s', iqn)

    def get_volume_paths(self, connection_properties):
        """Return the device path of the volume if it is attached."""
        portal, iqn, lun = self._get_all_targets(connection_properties)[0]
        path = self._device_for(self._find_session(portal, iqn), lun)
        return [path] if path is not None else []

    def get_all_available_volumes(self, connection_properties=None):
        """Return the device paths of every LUN on every session."""
        return [
            os.path.join(DEV_DIR, entry['device'])
            for session in self._sessions()
            for entry in session_luns(session)
        ]

    @utils.trace
    @base.synchronized('extend_volume', external=True)
    def extend_volume(self, connection_properties):
        """Reprobe the attached device and return its size in bytes."""
        paths = self.get_volume_paths(connection_properties)
        if not paths:
            raise exception.VolumeDeviceNotFound(
                device=connection_properties.get('target_iqn')
            )
        self._execute(
            'camcontrol',
            'reprobe',
            os.path.basename(paths[0]),
            run_as_root=True,
            root_helper=self._root_helper,
        )
        out, _err = self._execute(
            'diskinfo',
            paths[0],
            run_as_root=True,
            root_helper=self._root_helper,
        )
        return int(out.split()[2])
