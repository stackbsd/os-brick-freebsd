# os-brick-freebsd

An out-of-tree os-brick iSCSI connector for the FreeBSD initiator.

Designed to be used with [cinder-zfs-driver](https://github.com/stackbsd/cinder-zfs-driver) and [nova-bhyve](https://github.com/stackbsd/nova-bhyve).

## Status

### Features

- Attach and detach iSCSI volumes with `iscsictl(8)` and return the `da(4)` device
- CHAP authentication with the secret passed in a private config file
- Initiator name reported to Cinder that matches what `iscsictl` logs in with
- Online size refresh with `camcontrol reprobe`

### Roadmap

- Lookup through the os-brick connector factory on FreeBSD
- Mutual CHAP
- Multipath portals
- NVMe over Fabrics connector

### Limitations

- Only the first portal of a multipath connection is used
- Encrypted volumes are not handled
- Because os-brick's connector factory doesn't look up this driver yet, other tools like cinder-backup or glance's cinder store can't use it 

## Host requirements

Make sure the `iscsid` service is enable and running.

```sh
sysrc iscsid_enable=YES
service iscsid start
```

This package needs to be installed alongside os-brick, so make sure whatever Python environment its installed in nova will be able to load it.