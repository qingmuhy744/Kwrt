"""Exercise the actual patched stop function with a simulated mac80211 backend.

The unmodified function must reproduce the stale userspace station table.
CI repeats the check against the pinned source tree, in addition to the small
upstream fixture. This is a state transition test, not a radio/hwsim test.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import prepare

SOURCE = "package/network/services/hostapd/src/src/ap/ucode.c"
CC = shutil.which("cc")
OPENWRT = os.environ.get("SL3000_TEST_OPENWRT")

HARNESS = r"""
#include <stddef.h>
#include <string.h>

typedef int uc_vm_t;
typedef int uc_value_t;
#define HAPD_IFACE_ENABLED 1
#define HAPD_IFACE_DISABLED 0
#define MSG_INFO 3
#define WLAN_REASON_DEAUTH_LEAVING 3
struct sta_info { struct sta_info *next; unsigned char addr[6]; };
struct hostapd_data {
    struct sta_info *sta_list;
    int kernel_clients, beacon_set_done, deauths, stops;
};
struct hostapd_iface {
    int state, num_bss;
    struct hostapd_data **bss;
};
static struct hostapd_iface *active;
static int errors, fail_deauth, logged;
#define uc_fn_thisval(type) active
static void check(int condition) { if (!condition) errors++; }
static int clients(struct hostapd_data *hapd) {
    int count = 0;
    for (struct sta_info *sta = hapd->sta_list; sta; sta = sta->next) count++;
    return count;
}
static void wpa_printf(int level, const char *message) {
    check(level == MSG_INFO && strstr(message, "AP+STA") != NULL);
    logged++;
}
static int hostapd_drv_sta_deauth(struct hostapd_data *hapd,
                                const unsigned char *addr, int reason) {
    /* Per-client notification must precede removal of the driver's keys. */
    check(hapd->kernel_clients == clients(hapd) && hapd->kernel_clients > 0);
    check(hapd->beacon_set_done && reason == WLAN_REASON_DEAUTH_LEAVING);
    int found = 0;
    for (struct sta_info *sta = hapd->sta_list; sta; sta = sta->next)
        if (sta->addr == addr) found++;
    check(found == 1);
    hapd->deauths++;
    return fail_deauth ? -1 : 0;
}
static void hostapd_free_stas(struct hostapd_data *hapd) {
    if (hapd->sta_list) check(hapd->deauths == clients(hapd));
    hapd->sta_list = NULL;
    hapd->kernel_clients = 0;
}
static void uc_hostapd_disable_iface(struct hostapd_iface *iface) {
    iface->state = HAPD_IFACE_DISABLED;
}
static int hostapd_drv_stop_ap(struct hostapd_data *hapd) {
    /* mac80211 stop_ap drops stations regardless of hostapd's view. */
    hapd->kernel_clients = 0;
    hapd->stops++;
    return 0;
}

/* STOP_FUNCTION */

int main(int argc, char **argv) {
    (void)argv;
    fail_deauth = argc > 1;
    struct sta_info a = {0}, b = {0}, c = {0}, other = {0};
    a.next = &b;
    struct hostapd_data first = {&a, 2, 1, 0, 0};
    struct hostapd_data second = {&c, 1, 1, 0, 0};
    struct hostapd_data unrelated = {&other, 1, 1, 0, 0};
    struct hostapd_data *bss[] = {&first, &second};
    struct hostapd_iface iface = {HAPD_IFACE_ENABLED, 2, bss};
    active = &iface;
    check(uc_hostapd_iface_stop(NULL, 0) == NULL);
    check(first.sta_list == NULL && second.sta_list == NULL);
    check(first.kernel_clients == 0 && second.kernel_clients == 0);
    check(first.deauths == 2 && second.deauths == 1);
    check(first.stops == 1 && second.stops == 1 && logged == 2);
    check(!first.beacon_set_done && !second.beacon_set_done);
    check(iface.state == HAPD_IFACE_ENABLED);
    check(unrelated.sta_list == &other && unrelated.kernel_clients == 1);

    /* Repeated DISCONNECTED/SCANNING notifications have no clients to evict. */
    uc_hostapd_iface_stop(NULL, 0);
    check(first.deauths == 2 && second.deauths == 1 && logged == 2);
    check(first.stops == 2 && second.stops == 2);

    iface.state = HAPD_IFACE_DISABLED;
    uc_hostapd_iface_stop(NULL, 0);
    check(iface.state == HAPD_IFACE_DISABLED && !first.sta_list);
    active = NULL;
    check(uc_hostapd_iface_stop(NULL, 0) == NULL);
    return errors ? 1 : 0;
}
"""


def stop_function(text):
    start = text.index("static uc_value_t *\nuc_hostapd_iface_stop(")
    return text[start:text.index("\n}\n", start) + 3]


@unittest.skipUnless(CC, "native C compiler required")
class ApstaRecoveryTests(unittest.TestCase):
    def check_source(self, text):
        lock = json.loads((ROOT / "sources.lock.json").read_text())
        update = lock["package_updates"]["hostapd"]
        patch = prepare.package_patch(update["patches"][0])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "src/ap/ucode.c"
            source.parent.mkdir(parents=True)
            source.write_text(text)
            result = subprocess.run(["patch", "--batch", "--fuzz=0", "-p1"],
                                    input=patch, cwd=root, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            changed = source.read_text()
            for label, code, expected in (("original", text, 1), ("fixed", changed, 0)):
                harness = root / (label + ".c")
                harness.write_text(HARNESS.replace("/* STOP_FUNCTION */", stop_function(code)))
                binary = root / label
                subprocess.run([CC, "-std=c99", "-Wall", "-Werror", "-Wno-unused-function",
                                str(harness), "-o", str(binary)], check=True, capture_output=True)
                for args in ([], ["deauth-send-failed"]):
                    with self.subTest(version=label, args=args):
                        run = subprocess.run([str(binary), *args], capture_output=True)
                        self.assertEqual(run.returncode, expected, run.stderr)

    def test_stale_clients_reproduced_and_cleared_before_kernel_stop(self):
        self.check_source((ROOT / "tests/fixtures/hostapd-apsta-stop.c").read_text())

    @unittest.skipUnless(OPENWRT, "CI provides the actual pinned source tree")
    def test_patch_against_actual_pinned_hostapd_source(self):
        self.check_source((Path(OPENWRT) / SOURCE).read_text())


if __name__ == "__main__":
    unittest.main()
