"""Run the actual uhttpd /ubus list handlers with pinned native libubox.

Only ubus lookups and HTTP response delivery are simulated. The pinned handler
must reject non-string, unterminated or over-limit path lists before any lookup
or client reference, deduplicate repeated object paths, and abort responses
that exceed UH_UBUS_MAX_LIST_SIZE. CI provides the checksum-verified source
archive after `make download`.
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

ERROR_PARAMS = 3
ERROR_INTERNAL = 4
NO_ERROR = 9

HARNESS = r"""
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <libubox/blobmsg.h>
#include <libubox/blobmsg_json.h>

struct ubus_context;
struct ubus_object_data {
    const char *path;
    struct blob_attr *signature;
};
typedef void (*ubus_lookup_handler_t)(struct ubus_context *ctx, struct ubus_object_data *obj, void *priv);

struct client { struct { struct { struct blob_buf buf; } ubus; } dispatch; };
static struct blob_buf buf;
static struct ubus_context *ctx;
static int lookups, refs, sends, flood_objects;

static void uh_client_ref(struct client *cl) { (void)cl; refs++; }
static void uh_client_unref(struct client *cl) { (void)cl; }
static void uh_ubus_init_json_rpc_response(struct client *cl, struct blob_buf *b)
{ (void)cl; (void)b; }
static void uh_ubus_send_response(struct client *cl, struct blob_buf *b)
{ (void)cl; (void)b; sends++; }

/* LIST_DEFINITIONS */

static int ubus_lookup(struct ubus_context *c, const char *path, ubus_lookup_handler_t cb, void *priv)
{
    /* Do not dereference path: unvalidated code could pass non-string blob payloads. */
    (void)path;
    lookups++;
    if (flood_objects) {
        char big[4096];
        struct ubus_object_data obj = { .path = big, .signature = NULL };
        int i;
        memset(big, 'a', sizeof(big) - 1);
        big[sizeof(big) - 1] = '\0';
        for (i = 0; i < flood_objects; i++)
            cb(c, &obj, priv);
    }
    return 0;
}

/* LIST_FUNCTIONS */

int main(int argc, char **argv)
{
    struct blob_buf input = {0};
    struct client cl = {0};
    struct blob_attr *params = NULL;
    enum rpc_error err = __ERROR_MAX;
    bool ok;
    const struct blobmsg_policy policy = {"params", BLOBMSG_TYPE_UNSPEC};
    if (argc != 2) return 2;
    blob_buf_init(&input, 0);
    if (!strcmp(argv[1], "unterminated")) {
        void *array = blobmsg_open_array(&input, "params");
        blobmsg_add_string(&input, NULL, "system");
        blobmsg_close_array(&input, array);
    } else if (!strcmp(argv[1], "oversized")) {
        flood_objects = 600;
        if (!blobmsg_add_json_from_string(&input, "{}")) return 3;
    } else if (!blobmsg_add_json_from_string(&input, argv[1])) return 3;
    blobmsg_parse(&policy, 1, &params, blob_data(input.head), blob_len(input.head));
    if (!strcmp(argv[1], "unterminated")) {
        struct blob_attr *entry = blobmsg_data(params);
        memset(blobmsg_data(entry), 'x', blobmsg_data_len(entry));
    }
    ok = uh_ubus_send_list(&cl, params, &err);
    printf("%d %d %d %d %d\n", ok ? 1 : 0, (int)err, lookups, refs, sends);
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
        lock = json.loads((ROOT / "sources.lock.json").read_text())
        for entry in lock.get("package_updates", {}).get("uhttpd", {}).get("patches", []):
            subprocess.run(["patch", "--batch", "--fuzz=0", "-p1"], cwd=root,
                           input=prepare.package_patch(entry), check=True, capture_output=True)
        text = (root / "ubus.c").read_text()
        definitions = (
            text[text.index("#define UH_UBUS_MAX_LIST_SIZE"):text.index("enum {")]
            + "\n"
            + text[text.index("struct list_data {"):text.index("static const struct {")]
        )
        functions = text[text.rindex("static void uh_ubus_list_cb("):text.index("static bool parse_json_rpc(")]
        harness = root / "uhttpd_list.c"
        harness.write_text(
            HARNESS.replace("/* LIST_DEFINITIONS */", definitions).replace("/* LIST_FUNCTIONS */", functions)
        )
        cls.binary = root / "uhttpd_list"
        native = Path(LIBUBOX)
        subprocess.run([CC, "-std=c99", "-Wall", "-Werror", "-I" + str(native / "include"),
                        str(harness), "-L" + str(native / "lib"),
                        "-Wl,-rpath," + str(native / "lib"), "-lblobmsg_json", "-lubox",
                        "-o", str(cls.binary)], check=True, capture_output=True)

    def result(self, params):
        argument = params if params in ("unterminated", "oversized") else json.dumps(params)
        output = subprocess.check_output([str(self.binary), argument], text=True)
        return tuple(map(int, output.split()))

    def test_non_string_entries_rejected_before_any_lookup_or_reference(self):
        for value in (0, True, None, {}, [], ["nested"]):
            for values in ([value], ["system", value]):
                with self.subTest(params=values):
                    self.assertEqual(self.result({"params": values}), (0, ERROR_PARAMS, 0, 0, 0))

    def test_unterminated_string_is_rejected(self):
        self.assertEqual(self.result("unterminated"), (0, ERROR_PARAMS, 0, 0, 0))

    def test_excessive_path_count_is_rejected_and_duplicates_are_deduplicated(self):
        at_limit = [f"obj.{index}" for index in range(512)]
        self.assertEqual(self.result({"params": at_limit}), (1, NO_ERROR, 512, 1, 1))
        self.assertEqual(self.result({"params": [*at_limit, "obj.512"]}), (0, ERROR_PARAMS, 0, 0, 0))
        self.assertEqual(
            self.result({"params": ["system", "system", "network.interface", "system"]}),
            (1, NO_ERROR, 2, 1, 1),
        )

    def test_oversized_list_response_aborts_before_delivery(self):
        self.assertEqual(self.result("oversized"), (0, ERROR_INTERNAL, 1, 1, 0))

    def test_valid_lists_preserve_lookup_and_response_behavior(self):
        for values in ([], ["system"], ["system", "network.interface"], [""]):
            with self.subTest(params=values):
                self.assertEqual(self.result({"params": values}), (1, NO_ERROR, len(values), 1, 1))

    def test_absent_and_non_array_params_still_list_all(self):
        for params in ({}, {"params": None}, {"params": 0}, {"params": True},
                       {"params": "system"}, {"params": {"object": "system"}}):
            with self.subTest(params=params):
                self.assertEqual(self.result(params), (1, NO_ERROR, 1, 1, 1))


if __name__ == "__main__":
    unittest.main()
