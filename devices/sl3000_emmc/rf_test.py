#!/usr/bin/env python3
"""Private, device-specific EEPROM A/B test support; never writes router storage."""

import argparse
import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import tarfile
import tempfile

SIZE = 4096
FIRMWARE_PATH = "lib/firmware/mediatek/mt7981_eeprom_mt7976_dbdc.bin"
MARKER_PATH = "etc/sl3000-rf-test.json"
CALIBRATION_ENV = "SL3000_RF_TEST_CALIBRATION"
PASSPHRASE_ENV = "SL3000_RF_TEST_PASSPHRASE"
# Observed MT7981 A-die v1 byte locations, not calibration values from any device.
CAL_FREE_OFFSETS = {
    0x24c, 0x24d, 0x24e, 0x24f, 0x250, 0x251, 0x253, 0x255, 0x257, 0x259,
    0x270, 0x271, 0x990, 0x991, 0x994, 0x995, 0x9a0, 0x9a6, 0x9a8, 0x9aa,
}


def enabled():
    value = os.environ.get("SL3000_RF_TEST", "false")
    if value not in ("true", "false"):
        raise ValueError("SL3000_RF_TEST must be true or false")
    return value == "true"


def validate_eeprom(data):
    if len(data) != SIZE or data[:2] != b"\x81\x79":
        raise ValueError("RF test requires exactly 4096 bytes of MT7981 EEPROM")
    if data[0x19a] != 0:
        raise ValueError("RF test cannot supply external pre-calibration data")
    if data[0x270] != 0x0c or data[0x9a0] != 1:
        raise ValueError("RF test is restricted to the observed MT7976C / A-die v1")
    return data


def calibration():
    try:
        payload = json.loads(os.environ.get(CALIBRATION_ENV, ""))
        data = base64.b64decode(payload["eeprom_base64"], validate=True)
        expected = payload["sha256"]
    except (ValueError, KeyError, TypeError, binascii.Error):
        raise ValueError("Missing or invalid private RF calibration secret") from None
    validate_eeprom(data)
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("Private RF calibration checksum mismatch")
    return data


def passphrase():
    value = os.environ.get(PASSPHRASE_ENV, "")
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("RF artifact encryption requires a random 32-byte hex key")
    return value


def preflight(publish_release):
    import factory_test
    factory_test.preflight(publish_release)
    import nor_probe
    nor_probe.require_private()
    if enabled():
        if publish_release:
            raise ValueError("Device-specific RF test images must not be published")
        calibration()
        passphrase()


def marker(data):
    return (json.dumps({
        "profile": "private-runtime-eeprom-ab-test",
        "hardware_validated": False,
        "eeprom_sha256": hashlib.sha256(data).hexdigest(),
        "warning": "Only for the router from which the runtime EEPROM was captured",
    }, indent=2) + "\n").encode()


def write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def inject(tree):
    import factory_test
    factory_test.preflight()
    if not enabled():
        return
    data = calibration()
    for name, content in ((FIRMWARE_PATH, data), (MARKER_PATH, marker(data))):
        path = tree / "files" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        write_private(path, content)


def parse_words(output, offsets):
    words = re.findall(r"\[0x([0-9a-f]{4})\]:(?:0x)?([0-9a-f]{4})", output, re.I)
    addresses = [int(address, 16) for address, _ in words]
    if addresses != list(offsets):
        raise ValueError("Incomplete, duplicated or out-of-order EEPROM read")
    return b"".join(int(value, 16).to_bytes(2, "little") for _, value in words)


def capture(router, control, interface):
    if not re.fullmatch(r"root@[a-zA-Z0-9.-]+", router):
        raise ValueError("Unexpected router SSH target")
    if not re.fullmatch(r"[a-zA-Z0-9]+", interface):
        raise ValueError("Unexpected wireless interface")
    snapshots = []
    for _ in range(2):
        blocks = []
        for start in range(0, SIZE, 64):
            offsets = range(start, start + 64, 2)
            # Bare hexadecimal addresses only: '=' would select an EEPROM write.
            addresses = ",".join(f"{offset:04x}" for offset in offsets)
            command = shlex.join(["iwpriv", interface, "e2p", addresses])
            output = subprocess.check_output([
                "ssh", "-S", str(control), "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=5",
                router, command,
            ], text=True, timeout=15)
            blocks.append(parse_words(output, offsets))
        snapshots.append(validate_eeprom(b"".join(blocks)))
    if snapshots[0] != snapshots[1]:
        raise ValueError("Runtime EEPROM changed between reads; no test input saved")
    return snapshots[0]


def save_capture(data, reference, destination):
    base = reference.read_bytes()
    if len(base) not in (SIZE, 2 * 1024**2):
        raise ValueError("Unexpected reference EEPROM file size")
    validate_eeprom(base[:SIZE])
    differences = {offset for offset in range(SIZE) if data[offset] != base[offset]}
    if not differences <= CAL_FREE_OFFSETS:
        raise ValueError("Runtime differs from the ROM beyond known calibration bytes")
    destination.mkdir(mode=0o700)
    payload = {
        "sha256": hashlib.sha256(data).hexdigest(),
        "eeprom_base64": base64.b64encode(data).decode(),
    }
    write_private(destination / "runtime-eeprom.bin", data)
    write_private(destination / "calibration-secret.json", (json.dumps(payload) + "\n").encode())
    write_private(destination / "artifact-passphrase.txt", (secrets.token_hex(32) + "\n").encode())
    print(f"Captured two identical 4096-byte reads; {len(differences)} calibration bytes differ from ROM.")
    print("Private calibration and artifact decryption key saved locally; no router writes.")


def encrypt(source, destination):
    if not enabled():
        raise ValueError("Encrypted export is only for private RF test builds")
    key = passphrase()
    files = sorted(source.iterdir())
    if not files or any(not path.is_file() or path.is_symlink() for path in files):
        raise ValueError("RF export must contain only verified regular files")
    if destination.exists():
        raise ValueError("Encrypted output directory must not already exist")
    destination.mkdir(parents=True)
    encrypted = destination / "sl3000-rf-test.tar.gpg"
    # Keep plaintext staging outside upload/cache paths; isolate GnuPG from user config.
    with tempfile.TemporaryDirectory(prefix="sl3000-rf-encrypt-") as directory:
        scratch = Path(directory)
        archive = scratch / "firmware.tar"
        with tarfile.open(archive, "w") as stream:
            for path in files:
                stream.add(path, arcname=path.name, recursive=False)
        subprocess.run([
            "gpg", "--homedir", str(scratch), "--no-options", "--batch",
            "--no-symkey-cache", "--no-autostart",
            "--pinentry-mode", "loopback", "--passphrase-fd", "0",
            "--symmetric", "--cipher-algo", "AES256", "--compress-algo", "none",
            "--s2k-mode", "3", "--s2k-digest-algo", "SHA256", "--s2k-count", "65011712",
            "--output", str(encrypted), str(archive),
        ], input=(key + "\n").encode(), check=True)
    digest = hashlib.sha256()
    with encrypted.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    (destination / "sha256sums").write_text(f"{digest.hexdigest()}  {encrypted.name}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("preflight")
    check.add_argument("--publish-release", choices=("true", "false"), default="false")
    install = commands.add_parser("inject")
    install.add_argument("openwrt", type=Path)
    export = commands.add_parser("encrypt")
    export.add_argument("source", type=Path)
    export.add_argument("destination", type=Path)
    snapshot = commands.add_parser("capture")
    snapshot.add_argument("--router", default="root@192.168.21.1")
    snapshot.add_argument("--control", required=True, type=Path)
    snapshot.add_argument("--interface", default="rax0")
    snapshot.add_argument("--reference", required=True, type=Path)
    snapshot.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "preflight":
            preflight(args.publish_release == "true")
        elif args.command == "inject":
            inject(args.openwrt)
        elif args.command == "encrypt":
            encrypt(args.source, args.destination)
        else:
            data = capture(args.router, args.control, args.interface)
            save_capture(data, args.reference, args.output)
    except (ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, str(error) + "\n")
