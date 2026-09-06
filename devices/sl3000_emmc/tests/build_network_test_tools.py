#!/usr/bin/env python3
"""Build native UCI/ucode from already downloaded, pinned OpenWrt sources."""

import argparse
import hashlib
import os
from pathlib import Path
import re
import subprocess


def build(tree, output):
    output.mkdir(parents=True, exist_ok=False)
    install = output / 'install'
    for name, recipe, options in (
        ('libubox', 'package/libs/libubox/Makefile', ['-DBUILD_LUA=OFF', '-DBUILD_EXAMPLES=OFF']),
        ('uci', 'package/system/uci/Makefile', ['-DBUILD_LUA=OFF']),
        ('ucode', 'package/utils/ucode/Makefile', [
            '-DFS_SUPPORT=ON', '-DUCI_SUPPORT=ON',
            *[f'-D{feature}_SUPPORT=OFF' for feature in (
                'DEBUG', 'IO', 'MATH', 'UBUS', 'RTNL', 'NL80211', 'RESOLV',
                'STRUCT', 'ULOOP', 'LOG', 'SOCKET', 'ZLIB', 'DIGEST')],
        ]),
    ):
        text = (tree / recipe).read_text()
        def value(key):
            match = re.search(rf'^{key}:=(\S+)$', text, re.M)
            if not match:
                raise ValueError(f'Missing pinned {key} in {recipe}')
            return match[1]
        version = value('PKG_SOURCE_DATE').replace('-', '.') + '~' + value('PKG_SOURCE_VERSION')[:8]
        archive = tree / 'dl' / f'{name}-{version}.tar.zst'
        if hashlib.sha256(archive.read_bytes()).hexdigest() != value('PKG_MIRROR_HASH'):
            raise ValueError(f'Native test source checksum mismatch: {name}')
        source = output / name
        source.mkdir()
        subprocess.run(['tar', '-xf', str(archive), '--strip-components=1', '-C', str(source)], check=True)
        build_dir = output / f'build-{name}'
        subprocess.run(['cmake', '-S', str(source), '-B', str(build_dir),
            f'-DCMAKE_INSTALL_PREFIX={install}', '-DCMAKE_INSTALL_LIBDIR=lib',
            f'-DCMAKE_PREFIX_PATH={install};/opt/homebrew',
            f'-DCMAKE_INSTALL_RPATH={install}/lib', *options], check=True)
        subprocess.run(['cmake', '--build', str(build_dir), '-j', str(min(os.cpu_count() or 1, 4))], check=True)
        subprocess.run(['cmake', '--install', str(build_dir)], check=True)
    print(f'Native migration test interpreter: {install}/bin/ucode')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('openwrt', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    build(args.openwrt.resolve(), args.output.resolve())
