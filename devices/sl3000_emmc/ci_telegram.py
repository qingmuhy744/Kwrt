#!/usr/bin/env python3
"""Send verified firmware as size-limited Telegram documents."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import zipfile

from security_monitor import RemoteError, request_json


PART_BYTES = 45 * 1024 * 1024
IMAGE_PREFIX = "openwrt-mediatek-filogic-sl_3000-emmc"
PUBLIC_FILES = {
    "README.md", "build-info.json", "kernel.config", "openwrt.config",
    "sources.lock.json", "sha256sums", f"{IMAGE_PREFIX}.manifest",
    f"{IMAGE_PREFIX}-squashfs-sysupgrade.bin", f"{IMAGE_PREFIX}-initramfs.itb",
}
ENCRYPTED_FILES = {"sl3000-rf-test.tar.gpg", "sha256sums"}


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verified_files(source, encrypted=False):
    expected = ENCRYPTED_FILES if encrypted else PUBLIC_FILES
    files = sorted(source.iterdir())
    if source.is_symlink() or {path.name for path in files} != expected:
        raise ValueError("Unexpected delivery files; use the verified export directory")
    if any(path.is_symlink() or not path.is_file() for path in files):
        raise ValueError("Delivery files must be regular files, without symlinks")
    checksums = {}
    for line in (source / "sha256sums").read_text().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_.-]+)", line)
        if not match or match[2] not in expected - {"sha256sums"} or match[2] in checksums:
            raise ValueError("Invalid delivery checksum manifest")
        checksums[match[2]] = match[1]
    if set(checksums) != expected - {"sha256sums"}:
        raise ValueError("Incomplete delivery checksum manifest")
    for name, digest in checksums.items():
        if sha256(source / name) != digest:
            raise ValueError("Delivery checksum mismatch")
    if not encrypted:
        info = json.loads((source / "build-info.json").read_text())
        if (info.get("wifi_profile") not in ("generic-bootstrap", "public-factory-eeprom-test")
                or info.get("contains_private_calibration") is True
                or info.get("supported_devices") != ["sl,3000-emmc"]):
            raise ValueError("Private or unknown firmware must not be sent as a public archive")
    return files


def prepare(source, output, name, encrypted=False, part_bytes=PART_BYTES):
    if not re.fullmatch(r"sl3000-[A-Za-z0-9-]+", name):
        raise ValueError("Invalid delivery archive name")
    if not 0 < part_bytes <= PART_BYTES:
        raise ValueError("Invalid Telegram part size")
    files = verified_files(source, encrypted)
    if output.resolve() == source.resolve() or source.resolve() in output.resolve().parents:
        raise ValueError("Delivery output must be outside the verified export directory")
    output.mkdir(parents=True, exist_ok=False)
    archive = output / (name + ".zip")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as bundle:
        for path in files:
            bundle.write(path, path.name)
    archive_digest = sha256(archive)
    documents = []
    if archive.stat().st_size <= part_bytes:
        documents.append(archive)
    else:
        with archive.open("rb") as stream:
            for index, block in enumerate(iter(lambda: stream.read(part_bytes), b""), 1):
                part = output / f"{archive.name}.{index:03d}"
                part.write_bytes(block)
                documents.append(part)
        archive.unlink()
    checksums = output / "SHA256SUMS.txt"
    checksums.write_text("".join(f"{sha256(path)}  {path.name}\n" for path in documents))
    instructions = output / "RESTORE.txt"
    text = [
        "SL-3000 eMMC verified build artifacts", "",
        f"Archive: {archive.name}", f"Archive SHA-256: {archive_digest}",
        f"Data files: {len(documents)}", "",
        "Download every data file and SHA256SUMS.txt into the same directory.",
        "On Linux: sha256sum -c SHA256SUMS.txt",
        "On macOS: shasum -a 256 -c SHA256SUMS.txt", "",
    ]
    if len(documents) > 1:
        text += ["Reassemble in numeric order on Linux/macOS:",
                 f"cat {archive.name}.[0-9][0-9][0-9] > {archive.name}", ""]
    text += [f"Unzip {archive.name}, then verify the included sha256sums.",
             "The original firmware files are unchanged. Do not flash a ZIP or a split part."]
    if encrypted:
        text += ["The enclosed RF test archive remains encrypted; use your existing local key."]
    instructions.write_text("\n".join(text) + "\n")
    return [instructions, checksums, *documents]


def send_document(token, chat_id, path, caption):
    if path.stat().st_size > PART_BYTES:
        raise ValueError("Telegram document exceeds the configured size limit")
    quoted_path = str(path.resolve()).replace("\\", "\\\\").replace('"', '\\"')
    command = [
        "curl", "--silent", "--show-error", "--proto", "=https",
        "--connect-timeout", "20", "--max-time", "240", "--config", "-",
        "--form-string", "chat_id=" + chat_id,
        "--form-string", "caption=" + caption,
        "--form", f'document=@"{quoted_path}";type=application/octet-stream',
        "--write-out", "\n%{http_code}",
    ]
    # Keep the token out of process arguments and suppress raw HTTP/error bodies.
    config = 'url = "https://api.telegram.org/bot' + token + '/sendDocument"\n'
    for attempt in range(3):
        status, result, network_error = 0, {}, False
        delay = 2 ** (attempt + 1)
        try:
            response = subprocess.run(command, input=config, text=True, capture_output=True, timeout=260)
            network_error = response.returncode != 0
            if not network_error:
                body, code = response.stdout.rsplit("\n", 1)
                status = int(code)
                try:
                    result = json.loads(body)
                except ValueError:
                    result = {}
                if not isinstance(result, dict):
                    result = {}
        except (subprocess.TimeoutExpired, OSError):
            network_error = True
        except ValueError:
            network_error = True
        if not network_error and status == 200 and result.get("ok") is True:
            return
        code = result.get("error_code", status)
        retryable = network_error or code == 429 or (isinstance(code, int) and 500 <= code < 600)
        if code == 429:
            try:
                delay = max(delay, int(result.get("parameters", {}).get("retry_after", delay)))
            except (ValueError, TypeError, AttributeError):
                pass
        if not retryable or attempt == 2 or delay > 120:
            reason = "connection failed" if network_error else f"request failed (HTTP {status})"
            raise RemoteError("Telegram document " + reason) from None
        print(f"Telegram document retry {attempt + 2}/3 in {delay}s", flush=True)
        time.sleep(delay)
    raise RemoteError("Telegram document upload failed")


def deliver(documents, name, run_url, github_status, artifact_url, token, chat_id):
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token) or not re.fullmatch(r"-?[0-9]+", chat_id):
        raise ValueError("Telegram credentials are missing or invalid")
    url = "https://api.telegram.org/bot" + token + "/sendMessage"
    text = f"SL-3000 firmware files\n{name}\nGitHub upload: {github_status}\n{run_url}"
    if artifact_url and github_status == "success":
        text += "\nGitHub download: " + artifact_url
    text += f"\nSending {len(documents) - 2} data file(s), checksums and restore instructions."
    request_json(url, telegram=True, payload={"chat_id": chat_id, "text": text,
                                            "link_preview_options": {"is_disabled": True}})
    for index, path in enumerate(documents, 1):
        time.sleep(1)
        send_document(token, chat_id, path, f"{name}\n{path.name}\nFile {index}/{len(documents)}")
        print(f"Telegram delivered {index}/{len(documents)}: {path.name}", flush=True)
    time.sleep(1)
    request_json(url, telegram=True, payload={
        "chat_id": chat_id, "text": f"{name}\nAll files delivered. Follow RESTORE.txt and verify the checksums.",
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--encrypted", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    documents = prepare(args.source, args.output, args.name, args.encrypted)
    print(f"Prepared {len(documents) - 2} Telegram data file(s), each at most 45 MiB")
    if not args.prepare_only:
        deliver(documents, args.name, os.environ.get("BUILD_RUN_URL", ""),
                os.environ.get("FIRMWARE_UPLOAD_STATUS", "unknown"),
                os.environ.get("FIRMWARE_ARTIFACT_URL", ""),
                os.environ.get("TELEGRAM_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", ""))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, RemoteError) as error:
        print("Telegram artifact delivery failed: " + str(error), file=sys.stderr)
        sys.exit(1)
