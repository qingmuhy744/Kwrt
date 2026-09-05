import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import rf_test


def fixture():
    data = bytearray(rf_test.SIZE)
    data[:2] = b"\x81\x79"
    data[0x270] = 0x0c
    data[0x9a0] = 1
    return bytes(data)


def private_env(data=None):
    data = fixture() if data is None else data
    return {
        "SL3000_RF_TEST": "true",
        rf_test.CALIBRATION_ENV: json.dumps({
            "eeprom_base64": base64.b64encode(data).decode(),
            "sha256": hashlib.sha256(data).hexdigest(),
        }),
        rf_test.PASSPHRASE_ENV: "12" * 32,
    }


class RFInputTests(unittest.TestCase):
    def test_private_profile_requires_secrets_and_forbids_release(self):
        with patch.dict(os.environ, private_env()):
            rf_test.preflight(False)
            with self.assertRaisesRegex(ValueError, "must not be published"):
                rf_test.preflight(True)
        with patch.dict(os.environ, {"SL3000_RF_TEST": "true"}, clear=True):
            with self.assertRaises(ValueError):
                rf_test.preflight(False)

    def test_generic_profile_does_not_require_private_secrets(self):
        with patch.dict(os.environ, {}, clear=True):
            rf_test.preflight(True)
            self.assertFalse(rf_test.enabled())
        with patch.dict(os.environ, {"SL3000_RF_TEST": "yes"}):
            with self.assertRaises(ValueError):
                rf_test.enabled()

    def test_bad_calibration_is_rejected_without_echoing(self):
        for value in ("private-invalid-json", "[]", "null", "{}",
                      json.dumps({"eeprom_base64": "invalid!", "sha256": "bad"})):
            with self.subTest(value=value), patch.dict(os.environ, {rf_test.CALIBRATION_ENV: value}):
                with self.assertRaises(ValueError) as error:
                    rf_test.calibration()
                self.assertNotIn(value, str(error.exception))
        env = private_env()
        payload = json.loads(env[rf_test.CALIBRATION_ENV])
        payload["sha256"] = "0" * 64
        env[rf_test.CALIBRATION_ENV] = json.dumps(payload)
        with patch.dict(os.environ, env), self.assertRaisesRegex(ValueError, "checksum"):
            rf_test.calibration()

    def test_wrong_chip_size_precal_and_adie_are_rejected(self):
        for data in (fixture()[:-1], fixture() + b"\0", b"\0" * rf_test.SIZE):
            with self.assertRaises(ValueError):
                rf_test.validate_eeprom(data)
        for offset in (0x19a, 0x270, 0x9a0):
            data = bytearray(fixture())
            data[offset] ^= 0xff
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                rf_test.validate_eeprom(data)

    def test_key_format_and_length(self):
        for value in ("", "short", "a" * 63, "a" * 65, "Z" * 64):
            with patch.dict(os.environ, {rf_test.PASSPHRASE_ENV: value}), self.assertRaises(ValueError):
                rf_test.passphrase()

    def test_injects_exact_runtime_data_outside_cache_paths(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, private_env()):
            tree = Path(directory)
            rf_test.inject(tree)
            data = tree / "files" / rf_test.FIRMWARE_PATH
            marker = tree / "files" / rf_test.MARKER_PATH
            self.assertEqual(data.read_bytes(), fixture())
            self.assertEqual(marker.read_bytes(), rf_test.marker(fixture()))
            self.assertEqual(data.stat().st_mode & 0o777, 0o600)
            self.assertEqual({p for p in tree.rglob("*") if p.is_file()}, {data, marker})
            with self.assertRaises(FileExistsError):
                rf_test.inject(tree)


class RFCaptureTests(unittest.TestCase):
    def test_words_are_little_endian_and_must_be_complete(self):
        self.assertEqual(rf_test.parse_words("[0x0000]:7981 [0x0002]:0xABCD", [0, 2]), b"\x81\x79\xcd\xab")
        for text in ("", "[0x0000]:7981", "[0x0000]:7981 [0x0000]:7981",
                     "[0x0002]:ABCD [0x0000]:7981"):
            with self.assertRaises(ValueError):
                rf_test.parse_words(text, [0, 2])

    def test_capture_only_issues_bounded_read_commands_twice(self):
        data = fixture()
        def read(command, **kwargs):
            self.assertEqual(command[-2], "root@192.168.21.1")
            self.assertTrue(command[-1].startswith("iwpriv rax0 e2p "))
            self.assertNotIn("=", command[-1])
            addresses = [int(value, 16) for value in command[-1].split()[-1].split(",")]
            self.assertEqual(len(addresses), 32)
            self.assertTrue(all(0 <= address <= 4094 and address % 2 == 0 for address in addresses))
            return " ".join(f"[0x{address:04X}]:0x{int.from_bytes(data[address:address+2], 'little'):04X}" for address in addresses)
        with patch.object(rf_test.subprocess, "check_output", side_effect=read) as ssh:
            self.assertEqual(rf_test.capture("root@192.168.21.1", Path("/tmp/control"), "rax0"), data)
            self.assertEqual(ssh.call_count, 128)

    def test_reference_difference_must_be_calibration_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "reference.bin"
            reference.write_bytes(fixture())
            changed = bytearray(fixture())
            changed[0x24c] = 3
            output = root / "private"
            rf_test.save_capture(bytes(changed), reference, output)
            self.assertEqual((output / "runtime-eeprom.bin").read_bytes(), changed)
            for path in output.iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            changed[0x444] = 2
            with self.assertRaisesRegex(ValueError, "beyond known calibration"):
                rf_test.save_capture(bytes(changed), reference, root / "bad")
            self.assertFalse((root / "bad").exists())


@unittest.skipUnless(shutil.which("gpg"), "GnuPG is required for encrypted export tests")
class RFEncryptionTests(unittest.TestCase):
    def test_export_round_trip_uploads_only_ciphertext_and_checksum(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, private_env()):
            root = Path(directory)
            source = root / "verified"
            source.mkdir()
            secret = b"device-specific-test-calibration-never-upload-plaintext"
            (source / "sysupgrade.bin").write_bytes(secret)
            output = root / "encrypted"
            rf_test.encrypt(source, output)
            self.assertEqual({p.name for p in output.iterdir()}, {"sl3000-rf-test.tar.gpg", "sha256sums"})
            encrypted = output / "sl3000-rf-test.tar.gpg"
            self.assertNotIn(secret, encrypted.read_bytes())
            self.assertTrue((output / "sha256sums").read_text().startswith(hashlib.sha256(encrypted.read_bytes()).hexdigest()))
            gpg_home = root / "gpg"
            gpg_home.mkdir(mode=0o700)
            archive = root / "decrypted.tar"
            subprocess.run([
                "gpg", "--homedir", str(gpg_home), "--no-options", "--batch",
                "--no-symkey-cache", "--no-autostart", "--pinentry-mode", "loopback",
                "--passphrase-fd", "0", "--output", str(archive), "--decrypt", str(encrypted),
            ], input=(rf_test.passphrase() + "\n").encode(), check=True, capture_output=True)
            with tarfile.open(archive) as stream:
                self.assertEqual(stream.getnames(), ["sysupgrade.bin"])
                self.assertEqual(stream.extractfile("sysupgrade.bin").read(), secret)

    def test_encryption_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, private_env()):
            root = Path(directory)
            source = root / "verified"
            source.mkdir()
            (source / "unsafe").symlink_to("outside")
            with self.assertRaises(ValueError):
                rf_test.encrypt(source, root / "encrypted")


if __name__ == "__main__":
    unittest.main()
