"""Apply and verify narrowly scoped firmware permission fixes."""

import json


MOUNTS_ADVISORY = "GHSA-v5f9-62c7-cw29"
FIXES = (MOUNTS_ADVISORY,)
ACL_PATH = "usr/share/rpcd/acl.d/luci-mod-system.json"
SOURCE_PATH = "feeds/luci/modules/luci-mod-system/root/" + ACL_PATH
CRONTAB = "/etc/crontabs/root"


def validate_acl(data):
    acl = json.loads(data)
    mounts = acl["luci-mod-system-mounts"]["write"]["file"]
    cron = acl["luci-mod-system-cron"]["write"]["file"]
    if CRONTAB in mounts or cron.get(CRONTAB) != ["write"]:
        raise ValueError("LuCI mount/cron permission separation is missing")
    if mounts.get("/bin/umount") != ["exec"] or mounts.get("/sbin/block") != ["exec"]:
        raise ValueError("LuCI mount operations were changed")


def prepare(tree, lock):
    if MOUNTS_ADVISORY not in lock.get("hardening", []):
        return
    path = tree / SOURCE_PATH
    acl = json.loads(path.read_text())
    grants = acl["luci-mod-system-mounts"]["write"]["file"]
    if grants.pop(CRONTAB, None) != ["write"]:
        raise ValueError("LuCI mount ACL source context changed")
    data = json.dumps(acl, ensure_ascii=False, indent="\t") + "\n"
    validate_acl(data)
    path.write_text(data)


def verify(read_file, lock):
    if MOUNTS_ADVISORY in lock.get("hardening", []):
        validate_acl(read_file(ACL_PATH))
    return list(lock.get("hardening", []))
