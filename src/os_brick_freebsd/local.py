"""FreeBSD local zvol connector."""

from os_brick import exception, utils
from os_brick.initiator import initiator_connector

from os_brick_freebsd.iscsi import OS_TYPE_FREEBSD


class FreeBSDLocalConnector(initiator_connector.InitiatorConnector):
    """Connector for a zvol on the host that serves it."""

    os_type = OS_TYPE_FREEBSD

    @staticmethod
    def get_connector_properties(root_helper, *args, **kwargs):
        """Return no connector properties."""
        return {}

    def get_search_path(self):
        """Return no search path."""
        return None

    def check_valid_device(self, path, run_as_root=True):
        """Report whether the first block of the device can be read."""
        return utils.check_valid_device(self, path)

    def get_volume_paths(self, connection_properties):
        """Return the device path the connection names."""
        path = connection_properties.get('device_path')
        if not path:
            raise exception.BrickException(
                message='refusing a local connection with no device_path. '
                'Name the device node in the connection.'
            )
        return [path]

    def get_all_available_volumes(self, connection_properties=None):
        """Return no volumes."""
        return []

    @utils.trace
    def connect_volume(self, connection_properties):
        """Return the device the connection names."""
        (path,) = self.get_volume_paths(connection_properties)
        return {'type': 'block', 'path': path}

    @utils.trace
    def disconnect_volume(
        self,
        connection_properties,
        device_info,
        force=False,
        ignore_errors=False,
    ):
        """Leave the device in place."""

    @utils.trace
    def extend_volume(self, connection_properties):
        """Return the size of the device in bytes."""
        (path,) = self.get_volume_paths(connection_properties)
        out, _err = self._execute(
            'diskinfo',
            path,
            run_as_root=True,
            root_helper=self._root_helper,
        )
        return int(out.split()[2])
