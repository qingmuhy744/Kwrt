"""Run the actual uhttpd list handler with pinned native libubox.

Only ubus lookups and HTTP response delivery are simulated. The original
handler must pass invalid JSON values to the string lookup boundary; the
backport must reject the whole list before any lookup or client reference.
CI provides the checksum-verified source archive after `make download`.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import prepare

OPENWRT = os.environ.get("SL3000_TEST_OPENWRT")
LIBUBOX = os.environ.get("SL3000_TEST_LIBUBOX")
CC = shutil.which("cc")

HARNESS = r"""
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <libubox/blobmsg.h>
#include <libubox/blobmsg_json.h>

struct client { struct { struct { struct blob_buf buf; } ubus; } dispatch; };
struct list_data { struct blob_buf *buf; bool verbose, add_object; };
static struct blob_buf buf;
static void *ctx;
static int lookups, refs, sends;
static void uh_ubus_list_cb(void) {}
static void uh_client_ref(struct client *cl) { (void)cl; refs++; }
static void uh_client_unref(struct client *cl) { (void)cl; }
static void uh_ubus_init_json_rpc_response(struct client *cl, struct blob_buf *b)
{ (void)cl; (void)b; }
static void uh_ubus_send_response(struct client *cl, struct blob_buf *b)
{ (void)cl; (void)b; sends++; }
static void ubus_lookup(void *c, const char *path, void (*cb)(void), void *data)
{
    /* Do not dereference path: old code may pass non-string blob payloads. */
    (void)c; (void)path; (void)cb; (void)data; lookups++;
}

/* LIST_FUNCTIONS */

int main(int argc, char **argv)
{
    struct blob_buf input = {0};
    struct client cl = {0};
    struct blob_attr *params = NULL;
    const struct blobmsg_policy policy = {"params", BLOBMSG_TYPE_UNSPEC};
    if (argc != 2) return 2;
    blob_buf_init(&input, 0);
    if (!strcmp(argv[1], "unterminated")) {
        void *array = blobmsg_open_array(&input, "params");
        blobmsg_add_string(&input, NULL, "system");
        blobmsg_close_array(&input, array);
    } else if (!blobmsg_add_json_from_string(&input, argv[1])) return 3;
    blobmsg_parse(&policy, 1, &params, blob_data(input.head), blob_len(input.head));
    if (!strcmp(argv[1], "unterminated")) {
        struct blob_attr *entry = blobmsg_data(params);
        memset(blobmsg_data(entry), 'x', blobmsg_data_len(entry));
    }
    (void)uh_ubus_send_list(&cl, params);
    printf("%d %d %d\n", lookups, refs, sends);
    blob_buf_free(&cl.dispatch.ubus.buf);
    blob_buf_free(&buf);
    blob_buf_free(&input);
    return 0;
}
"""


@unittest.skipUnless(CC and OPENWRT and LIBUBOX, "pinned source archive and native libubox required")
class UhttpdSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        root = Path(directory.name)
        tree = Path(OPENWRT)
        recipe = (tree / "package/network/services/uhttpd/Makefile").read_text()
        def value(key):
            match = re.search(rf"^{key}:=(\S+)$", recipe, re.M)
            if not match:
                raise ValueError(f"Missing {key} in uhttpd recipe")
            return match[1]
        version = value("PKG_SOURCE_DATE").replace("-", ".") + "~" + value("PKG_SOURCE_VERSION")[:8]
        archive = tree / "dl" / f"uhttpd-{version}.tar.zst"
        if hashlib.sha256(archive.read_bytes()).hexdigest() != value("PKG_MIRROR_HASH"):
            raise ValueError("uhttpd source checksum mismatch")
        subprocess.run(["tar", "-xf", str(archive), "--strip-components=1", "-C", str(root)], check=True)
        source = root / "ubus.c"
        original = source.read_text()
        lock = json.loads((ROOT / "sources.lock.json").read_text())
        for entry in lock["package_updates"]["uhttpd"]["patches"]:
            subprocess.run(["patch", "--batch", "--fuzz=0", "-p1"], cwd=root,
                           input=prepare.package_patch(entry), check=True, capture_output=True)
        cls.binaries = {}
        for label, text, start in (
            ("original", original, "static void uh_ubus_send_list("),
            ("fixed", source.read_text(), "static bool uh_ubus_valid_list_params("),
        ):
            functions = text[text.index(start):text.index("static bool parse_json_rpc(")]
            harness = root / f"{label}.c"
            harness.write_text(HARNESS.replace("/* LIST_FUNCTIONS */", functions))
            binary = root / label
            native = Path(LIBUBOX)
            subprocess.run([CC, "-std=c99", "-Wall", "-Werror", "-I" + str(native / "include"),
                            str(harness), "-L" + str(native / "lib"),
                            "-Wl,-rpath," + str(native / "lib"), "-lblobmsg_json", "-lubox",
                            "-o", str(binary)], check=True, capture_output=True)
            cls.binaries[label] = binary

    def result(self, label, params):
        argument = "unterminated" if params == "unterminated" else json.dumps(params)
        output = subprocess.check_output([str(self.binaries[label]), argument], text=True)
        return tuple(map(int, output.split()))

    def test_non_string_entries_rejected_before_any_lookup_or_reference(self):
        for value in (0, True, None, {}, [], ["nested"]):
            for values in ([value], ["system", value]):
                with self.subTest(params=values):
                    original = self.result("original", {"params": values})
                    self.assertEqual(original, (len(values), 1, 1))
                    self.assertEqual(self.result("fixed", {"params": values}), (0, 0, 0))

    def test_unterminated_string_is_rejected(self):
        self.assertEqual(self.result("original", "unterminated"), (1, 1, 1))
        self.assertEqual(self.result("fixed", "unterminated"), (0, 0, 0))

    def test_valid_lists_preserve_lookup_and_response_behavior(self):
        for values in ([], ["system"], ["system", "network.interface"], [""]):
            for label in self.binaries:
                with self.subTest(params=values, version=label):
                    self.assertEqual(self.result(label, {"params": values}), (len(values), 1, 1))

    def test_absent_and_non_array_params_still_list_all(self):
        for params in ({}, {"params": None}, {"params": 0}, {"params": True},
                       {"params": "system"}, {"params": {"object": "system"}}):
            for label in self.binaries:
                with self.subTest(params=params, version=label):
                    self.assertEqual(self.result(label, params), (1, 1, 1))


if __name__ == "__main__":
    unittest.main()
