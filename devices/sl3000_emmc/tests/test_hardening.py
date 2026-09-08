import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import hardening


def acl_fixture(fixed=True):
    data = {
        "luci-mod-system-mounts": {"write": {"file": {"/bin/umount": ["exec"], "/sbin/block": ["exec"]},
                                              "uci": ["fstab"]}},
        "luci-mod-system-cron": {"write": {"file": {hardening.CRONTAB: ["write"]}}},
        "unrelated-group": {"read": {"uci": ["system"]}},
    }
    if not fixed:
        data["luci-mod-system-mounts"]["write"]["file"][hardening.CRONTAB] = ["write"]
    return data


class HardeningTests(unittest.TestCase):
    def test_only_the_extra_mount_grant_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory)
            path = tree / hardening.SOURCE_PATH
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(acl_fixture(False)))
            lock = {"hardening": [hardening.MOUNTS_ADVISORY]}
            hardening.prepare(tree, lock)
            self.assertEqual(json.loads(path.read_text()), acl_fixture())
            self.assertEqual(hardening.verify(lambda _: path.read_bytes(), lock), lock["hardening"])
            with self.assertRaisesRegex(ValueError, "source context"):
                hardening.prepare(tree, lock)

    def test_regression_and_broken_legitimate_permissions_are_rejected(self):
        for data in (acl_fixture(False), acl_fixture()):
            changed = copy.deepcopy(data)
            if changed == acl_fixture():
                changed["luci-mod-system-cron"]["write"]["file"].clear()
            with self.assertRaises(ValueError):
                hardening.validate_acl(json.dumps(changed))
        data = acl_fixture()
        data["luci-mod-system-mounts"]["write"]["file"].pop("/bin/umount")
        with self.assertRaisesRegex(ValueError, "mount operations"):
            hardening.validate_acl(json.dumps(data))
