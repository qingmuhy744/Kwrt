import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ci_telegram as delivery


class DeliveryFilesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "verified"
        self.source.mkdir()
        for name in delivery.PUBLIC_FILES - {"sha256sums"}:
            (self.source / name).write_bytes((name + "\n").encode() * 10)
        self.provenance = {
            "wifi_profile": "public-factory-eeprom-test",
            "supported_devices": ["sl,3000-emmc"], "contains_private_calibration": False,
        }
        self.update_provenance()

    def checksums(self):
        paths = sorted(path for path in self.source.iterdir() if path.name != "sha256sums")
        (self.source / "sha256sums").write_text("".join(
            f"{delivery.sha256(path)}  {path.name}\n" for path in paths))

    def update_provenance(self):
        (self.source / "build-info.json").write_text(json.dumps(self.provenance))
        self.checksums()

    def test_split_archive_restores_every_original_byte(self):
        documents = delivery.prepare(self.source, self.root / "parts", "sl3000-test-1", part_bytes=2000)
        parts = documents[2:]
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(path.stat().st_size <= 2000 for path in parts))
        self.assertFalse((self.root / "parts/sl3000-test-1.zip").exists())
        data = b"".join(path.read_bytes() for path in parts)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertEqual(set(archive.namelist()), delivery.PUBLIC_FILES)
            for name in archive.namelist():
                self.assertEqual(archive.read(name), (self.source / name).read_bytes())
        self.assertIn(hashlib.sha256(data).hexdigest(), documents[0].read_text())
        self.assertIn("cat sl3000-test-1.zip.[0-9][0-9][0-9]", documents[0].read_text())
        self.assertEqual(documents[1].read_text(), "".join(
            f"{delivery.sha256(path)}  {path.name}\n" for path in parts))

    def test_small_archive_is_sent_whole(self):
        documents = delivery.prepare(self.source, self.root / "small", "sl3000-small-1")
        self.assertEqual(len(documents), 3)
        self.assertEqual(documents[-1].suffix, ".zip")
        self.assertNotIn("Reassemble", documents[0].read_text())

    def test_corruption_is_rejected_before_preparing_any_files(self):
        (self.source / f"{delivery.IMAGE_PREFIX}-squashfs-sysupgrade.bin").write_bytes(b"corrupted")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            delivery.prepare(self.source, self.root / "bad", "sl3000-corrupt-1")
        self.assertFalse((self.root / "bad").exists())

    def test_unapproved_files_are_rejected_even_with_a_checksum(self):
        (self.source / "private-calibration.json").write_text("private data")
        self.checksums()
        with self.assertRaisesRegex(ValueError, "Unexpected delivery files"):
            delivery.verified_files(self.source)

    def test_symlinks_are_rejected(self):
        original = self.source / "README.md"
        target = self.root / "outside"
        original.rename(target)
        original.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            delivery.verified_files(self.source)

    def test_incomplete_duplicate_and_traversing_manifests_are_rejected(self):
        checksum_file = self.source / "sha256sums"
        original = checksum_file.read_text()
        for text in ("", original + original, original.replace("  README.md", "  ../README.md")):
            with self.subTest(text=text[:20]):
                checksum_file.write_text(text)
                with self.assertRaises(ValueError):
                    delivery.verified_files(self.source)

    def test_private_plaintext_is_rejected_even_with_valid_checksums(self):
        for field, value in (("wifi_profile", "private-runtime-eeprom-ab-test"),
                             ("contains_private_calibration", True),
                             ("supported_devices", ["other-device"])):
            original = self.provenance[field]
            with self.subTest(field=field):
                self.provenance[field] = value
                self.update_provenance()
                with self.assertRaisesRegex(ValueError, "Private or unknown"):
                    delivery.verified_files(self.source)
            self.provenance[field] = original

    def test_encrypted_mode_only_accepts_the_encrypted_export(self):
        with self.assertRaises(ValueError):
            delivery.verified_files(self.source, encrypted=True)
        for path in self.source.iterdir():
            path.unlink()
        (self.source / "sl3000-rf-test.tar.gpg").write_bytes(b"opaque encrypted archive")
        self.checksums()
        documents = delivery.prepare(self.source, self.root / "encrypted", "sl3000-rf-test-1", encrypted=True)
        with zipfile.ZipFile(documents[-1]) as archive:
            self.assertEqual(set(archive.namelist()), delivery.ENCRYPTED_FILES)
        self.assertIn("remains encrypted", documents[0].read_text())

    def test_archive_name_and_output_cannot_escape_or_modify_the_export(self):
        for name in ("../escape", "sl3000-`command`", "sl3000-test\n"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                delivery.prepare(self.source, self.root / "output", name)
        with self.assertRaisesRegex(ValueError, "outside"):
            delivery.prepare(self.source, self.source / "parts", "sl3000-test")


class TelegramTransportTests(unittest.TestCase):
    TOKEN = "12345:test-placeholder"
    CHAT = "12345"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "firmware.zip.001"
        self.path.write_bytes(b"test document")

    @staticmethod
    def response(status=200, payload=None, returncode=0):
        if payload is None:
            payload = {"ok": True}
        return subprocess.CompletedProcess([], returncode, json.dumps(payload) + f"\n{status}", "")

    @patch.object(delivery.time, "sleep")
    @patch.object(delivery.subprocess, "run")
    def test_connection_error_is_retried_and_credentials_stay_out_of_arguments(self, run, sleep):
        run.side_effect = [self.response(returncode=28), self.response()]
        delivery.send_document(self.TOKEN, self.CHAT, self.path, "test caption")
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(2)
        args, kwargs = run.call_args
        self.assertNotIn(self.TOKEN, " ".join(args[0]))
        self.assertIn(self.TOKEN, kwargs["input"])
        self.assertTrue(kwargs["capture_output"])
        self.assertIn("--form-string", args[0])

    @patch.object(delivery.time, "sleep")
    @patch.object(delivery.subprocess, "run")
    def test_rate_limit_honors_retry_after(self, run, sleep):
        run.side_effect = [self.response(429, {"ok": False, "parameters": {"retry_after": 17}}),
                           self.response()]
        delivery.send_document(self.TOKEN, self.CHAT, self.path, "test")
        sleep.assert_called_once_with(17)

    @patch.object(delivery.time, "sleep")
    @patch.object(delivery.subprocess, "run")
    def test_server_error_stops_after_three_attempts(self, run, sleep):
        run.return_value = self.response(503, {"ok": False})
        with self.assertRaisesRegex(delivery.RemoteError, "HTTP 503"):
            delivery.send_document(self.TOKEN, self.CHAT, self.path, "test")
        self.assertEqual(run.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])

    @patch.object(delivery.time, "sleep")
    @patch.object(delivery.subprocess, "run")
    def test_permanent_rejection_does_not_retry_or_expose_response(self, run, sleep):
        run.return_value = self.response(403, {"ok": False, "description": self.TOKEN})
        with self.assertRaises(delivery.RemoteError) as caught:
            delivery.send_document(self.TOKEN, self.CHAT, self.path, "test")
        self.assertNotIn(self.TOKEN, str(caught.exception))
        run.assert_called_once()
        sleep.assert_not_called()

    @patch.object(delivery.time, "sleep")
    @patch.object(delivery.subprocess, "run")
    def test_http_success_requires_telegram_acknowledgement(self, run, sleep):
        for payload in ({"ok": False}, {}, []):
            with self.subTest(payload=payload):
                run.return_value = self.response(200, payload)
                with self.assertRaises(delivery.RemoteError):
                    delivery.send_document(self.TOKEN, self.CHAT, self.path, "test")

    @patch.object(delivery.subprocess, "run")
    def test_oversized_document_is_rejected_without_a_request(self, run):
        with self.path.open("wb") as stream:
            stream.truncate(delivery.PART_BYTES + 1)
        with self.assertRaisesRegex(ValueError, "size limit"):
            delivery.send_document(self.TOKEN, self.CHAT, self.path, "test")
        run.assert_not_called()

    @patch.object(delivery.time, "sleep")
    @patch.object(delivery, "send_document")
    @patch.object(delivery, "request_json")
    def test_github_failure_does_not_prevent_telegram_delivery(self, message, document, sleep):
        delivery.deliver([self.path, self.path, self.path], "sl3000-test-1", "https://github.com/run",
                         "failure", "", self.TOKEN, self.CHAT)
        self.assertEqual(document.call_count, 3)
        self.assertIn("GitHub upload: failure", message.call_args_list[0].kwargs["payload"]["text"])
        self.assertIn("All files delivered", message.call_args_list[-1].kwargs["payload"]["text"])

    @patch.object(delivery.time, "sleep")
    @patch.object(delivery, "send_document", side_effect=delivery.RemoteError("connection failed"))
    @patch.object(delivery, "request_json")
    def test_partial_delivery_is_not_acknowledged_as_complete(self, message, document, sleep):
        with self.assertRaises(delivery.RemoteError):
            delivery.deliver([self.path, self.path, self.path], "sl3000-test-1", "https://github.com/run",
                             "success", "", self.TOKEN, self.CHAT)
        message.assert_called_once()
        document.assert_called_once()


if __name__ == "__main__":
    unittest.main()
