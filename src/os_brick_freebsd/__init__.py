"""os-brick connectors for FreeBSD."""

# connector class path for each protocol as the os-brick factory names it
CONNECTORS = {
    'ISCSI': 'os_brick_freebsd.iscsi.FreeBSDISCSIConnector',
    'ZFS_LOCAL': 'os_brick_freebsd.local.FreeBSDLocalConnector',
}
