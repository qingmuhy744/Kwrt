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
from prepare import prepare_network
from verify import validate_network_files

PORTS = ['lan1', 'lan2', 'lan3']
OLD_PORTS = ['lan1', 'lan2', 'lan3', 'lan4', 'lan1', 'lan2', 'lan3']
MAC = '02:11:22:33:44:55'
ANCHOR = 'mediatek_setup_interfaces()\n{\n\tlocal board="$1"\n\n\tcase $board in\n'
UPSTREAM = ANCHOR + '\t*) ucidef_set_interfaces_lan_wan "lan1 lan2 lan3 lan4" wan;;\n\tesac\n}\n'
UCODE = os.environ.get('SL3000_TEST_UCODE') or shutil.which('ucode')
OPENWRT = os.environ.get('SL3000_TEST_OPENWRT')


def network_files():
    return {
        'etc/board.d/02_network': (ANCHOR + '\tsl,3000-emmc)\n'
            '\t\tucidef_set_interfaces_lan_wan "lan1 lan2 lan3" wan\n\t\t;;\n').encode(),
        **{destination: (ROOT / 'files' / source).read_bytes() for source, destination in (
            ('03_sl3000-network', 'etc/board.d/03_sl3000-network'),
            ('98-sl3000-ports', 'etc/uci-defaults/98-sl3000-ports'),
            ('sl3000-ports.uc', 'usr/libexec/sl3000-ports.uc'),
        )},
    }


class NetworkRecipeTests(unittest.TestCase):
    def test_board_case_defines_ports_once_and_keeps_other_boards(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            path = base / 'etc/board.d/02_network'
            path.parent.mkdir(parents=True)
            path.write_text(UPSTREAM)
            prepare_network(base)
            validate_network_files(lambda name: (base / name).read_bytes())
            for board, expected in [('sl,3000-emmc', 'lan1 lan2 lan3|wan'),
                                    ('other,board', 'lan1 lan2 lan3 lan4|wan')]:
                script = path.read_text() + '\nucidef_set_interfaces_lan_wan() { printf "%s|%s\\n" "$1" "$2"; }\n'
                result = subprocess.run(['sh', '-c', script + '\nmediatek_setup_interfaces "$1"', 'test', board],
                                        text=True, capture_output=True, check=True)
                self.assertEqual(result.stdout.splitlines(), [expected])
            self.assertNotIn('ucidef_set_interfaces_lan_wan', (base / 'etc/board.d/03_sl3000-network').read_text())
            for name in ('etc/board.d/03_sl3000-network', 'etc/uci-defaults/98-sl3000-ports'):
                self.assertEqual((base / name).stat().st_mode & 0o777, 0o755)
                subprocess.run(['sh', '-n', str(base / name)], check=True)
            with self.assertRaisesRegex(ValueError, 'Source context changed'):
                prepare_network(base)

    def test_missing_or_changed_fix_is_rejected_in_image(self):
        files = network_files()
        validate_network_files(files.__getitem__)
        for name in files:
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_network_files({**files, name: b'old script'}.__getitem__)


@unittest.skipUnless(UCODE and OPENWRT, 'pinned source tree and native jshn required')
class NetworkBoardIntegrationTests(unittest.TestCase):
    def test_real_ucidef_helpers_reproduce_old_bug_and_generate_unique_fixed_ports(self):
        tree = Path(OPENWRT)
        install = Path(UCODE).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            path = base / 'etc/board.d/02_network'
            path.parent.mkdir(parents=True)
            path.write_bytes(subprocess.check_output(['git', 'show',
                'HEAD:target/linux/mediatek/filogic/base-files/etc/board.d/02_network'], cwd=tree))
            prepare_network(base)
            helpers = (tree / 'package/base-files/files/lib/functions/uci-defaults.sh').read_text()
            helpers = '\n'.join(line for line in helpers.splitlines() if not line.startswith('. /'))
            upstream = path.read_text()
            interfaces = upstream[upstream.index('mediatek_setup_interfaces()'):upstream.index('mediatek_setup_macs()')]
            cid = base / 'cid'
            cid.write_text('synthetic-emmc-cid-for-test\n')
            mac_script = (base / 'etc/board.d/03_sl3000-network').read_text()
            mac_script = '\n'.join(line for line in mac_script.splitlines() if not line.startswith('. /'))
            mac_script = mac_script.replace('/sys/block/mmcblk0/device/cid', str(cid))
            harness = '. "$1/share/libubox/jshn.sh"\n' + helpers + '\n' + interfaces + '''
board_name() { echo sl,3000-emmc; }
board_config_update() { :; }
board_config_flush() { json_dump; }
macaddr_add() { echo 02:11:22:33:44:56; }
json_init
'''
            environment = {**os.environ, 'PATH': str(install / 'bin') + os.pathsep + os.environ['PATH']}
            for script, expected in (
                ("ucidef_set_interfaces_lan_wan 'lan1 lan2 lan3 lan4' wan\n"
                 "ucidef_set_interfaces_lan_wan 'lan1 lan2 lan3' wan\njson_dump\n", OLD_PORTS),
                ('mediatek_setup_interfaces sl,3000-emmc\n' + mac_script, PORTS),
            ):
                result = subprocess.run(['bash', '-c', harness + script, 'test', str(install)],
                                        env=environment, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                board = json.loads(result.stdout)
                self.assertEqual(board['network']['lan']['ports'], expected)
                self.assertEqual(board['network']['wan']['device'], 'wan')
                if expected == PORTS:
                    self.assertRegex(board['network']['lan']['macaddr'], r'^02(:[0-9a-f]{2}){5}$')


@unittest.skipUnless(UCODE, 'native ucode with fs/uci modules required; CI runs these after download')
class NetworkMigrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for name in ('etc/config', 'tmp/sysinfo', 'tmp/.uci', 'sys/class/net'):
            (self.root / name).mkdir(parents=True)
        (self.root / 'tmp/sysinfo/board_name').write_text('sl,3000-emmc\n')
        self.board_path = self.root / 'etc/board.json'
        self.network_path = self.root / 'etc/config/network'
        self.backup = self.root / 'etc/sl3000-port-backup-v1'
        self.board = {'model': {'id': 'sl,3000-emmc'}, 'network': {
            'lan': {'ports': OLD_PORTS, 'macaddr': MAC, 'protocol': 'static'},
            'wan': {'device': 'wan', 'macaddr': '02:11:22:33:44:56'},
        }}
        self.board_path.write_text(json.dumps(self.board))
        bridge = "config device\n option name 'br-lan'\n option type 'bridge'\n option igmp_snooping '1'\n"
        bridge += ''.join(f" list ports '{port}'\n" for port in OLD_PORTS)
        devices = ''.join(f"config device\n option name '{port}'\n option macaddr '{MAC}'\n" for port in OLD_PORTS)
        self.network_path.write_text(bridge + devices + """
config device
 option name 'wan'
 option macaddr '02:11:22:33:44:56'
config interface 'lan'
 option device 'br-lan'
 option proto 'static'
 option ipaddr '192.168.21.1'
config interface 'wwan'
 option proto 'dhcp'
config bridge-vlan
 option device 'br-lan'
 option vlan '10'
 list ports 'lan1:t'
""")
        self.untouched = {}
        for name in ('wireless', 'passwall', 'openclash', 'dhcp', 'firewall'):
            path = self.root / 'etc/config' / name
            path.write_bytes(b'# opaque private configuration\n')
            self.untouched[path] = path.read_bytes()

    def migrate(self, success=True):
        result = subprocess.run([UCODE, str(ROOT / 'files/sl3000-ports.uc'), str(self.root)],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        for path, data in self.untouched.items():
            self.assertEqual(path.read_bytes(), data)
        return result

    def read_network(self):
        result = subprocess.run([UCODE, '-e', "import {cursor} from 'uci'; "
            "let c = cursor(ARGV[0] + '/etc/config', ARGV[0] + '/tmp/.uci', ''); "
            "print(sprintf('%J', c.get_all('network')));", str(self.root)],
            text=True, capture_output=True, check=True)
        return list(json.loads(result.stdout).values())

    def test_legacy_migration_preserves_settings_backs_up_and_is_idempotent(self):
        before = self.read_network()
        original = {path.name: path.read_bytes() for path in (self.board_path, self.network_path)}
        self.migrate()
        expected = {**self.board, 'network': {**self.board['network'],
            'lan': {**self.board['network']['lan'], 'ports': PORTS}}}
        self.assertEqual(json.loads(self.board_path.read_text()), expected)
        after = self.read_network()
        bridge = next(s for s in after if s.get('name') == 'br-lan')
        self.assertEqual(bridge['ports'], PORTS)
        self.assertEqual(bridge['igmp_snooping'], '1')
        devices = [s for s in after if s['.type'] == 'device' and s.get('name') != 'br-lan']
        self.assertEqual([s['name'] for s in devices], PORTS + ['wan'])
        self.assertEqual([s['macaddr'] for s in devices[:3]], [MAC] * 3)
        without_metadata = lambda s: {k: v for k, v in s.items() if not k.startswith('.')}
        self.assertEqual([without_metadata(s) for s in before if s['.type'] != 'device'],
                         [without_metadata(s) for s in after if s['.type'] != 'device'])
        self.assertEqual(self.backup.stat().st_mode & 0o777, 0o700)
        for name, content in original.items():
            self.assertEqual((self.backup / name).read_bytes(), content)
            self.assertEqual((self.backup / name).stat().st_mode & 0o777, 0o600)
        fixed = [p.read_bytes() for p in (self.board_path, self.network_path)]
        self.assertEqual(self.migrate().stdout, '')
        self.assertEqual([p.read_bytes() for p in (self.board_path, self.network_path)], fixed)

    def test_correct_new_board_metadata_still_migrates_preserved_old_network(self):
        self.board['network']['lan']['ports'] = PORTS
        self.board_path.write_text(json.dumps(self.board))
        original = self.board_path.read_bytes()
        self.migrate()
        self.assertEqual(self.board_path.read_bytes(), original)
        self.assertNotIn('lan4', self.network_path.read_text())

    def test_custom_port_list_is_not_rewritten(self):
        self.network_path.write_text(self.network_path.read_text().replace(" list ports 'lan4'\n", ''))
        self.assert_custom_network_retained()

    def assert_custom_network_retained(self):
        original = self.network_path.read_bytes()
        self.migrate()
        self.assertEqual(self.network_path.read_bytes(), original)
        self.assertEqual(json.loads(self.board_path.read_text())['network']['lan']['ports'], PORTS)

    def test_custom_mac_options_named_sections_and_lan4_references_are_retained(self):
        original = self.network_path.read_text()
        cases = [
            original.replace(f" option macaddr '{MAC}'", " option macaddr '02:ab:cd:ef:12:34'", 1),
            original.replace(" option name 'lan1'", " option name 'lan1'\n option mtu '1400'", 1),
            original.replace("config device\n option name 'lan1'", "config device 'custom_lan1'\n option name 'lan1'", 1),
            original + "config interface 'custom'\n option device 'lan4.10'\n",
            original + "config bridge-vlan\n option device 'br-lan'\n list ports 'lan4:t'\n",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.board_path.write_text(json.dumps(self.board))
                self.network_path.write_text(text)
                self.assert_custom_network_retained()

    def test_real_lan4_is_not_removed(self):
        (self.root / 'sys/class/net/lan4').mkdir()
        self.assert_custom_network_retained()

    def test_other_board_and_already_fixed_install_are_noops(self):
        (self.root / 'tmp/sysinfo/board_name').write_text('other,board\n')
        original = [p.read_bytes() for p in (self.board_path, self.network_path)]
        self.migrate()
        self.assertEqual([p.read_bytes() for p in (self.board_path, self.network_path)], original)
        self.assertFalse(self.backup.exists())
        (self.root / 'tmp/sysinfo/board_name').write_text('sl,3000-emmc\n')
        self.migrate()
        saved = [p.read_bytes() for p in self.backup.iterdir()]
        self.migrate()
        self.assertEqual([p.read_bytes() for p in self.backup.iterdir()], saved)

    def test_pending_network_changes_are_deferred_without_writes(self):
        pending = self.root / 'tmp/.uci/network'
        pending.write_text("network.lan.ipaddr='192.168.9.1'\n")
        original = [p.read_bytes() for p in (self.board_path, self.network_path, pending)]
        result = self.migrate(success=False)
        self.assertIn('Pending UCI network changes', result.stderr)
        self.assertEqual([p.read_bytes() for p in (self.board_path, self.network_path, pending)], original)
        self.assertFalse(self.backup.exists())

    def test_invalid_json_or_uci_prevents_any_rewrite(self):
        for path, text in ((self.board_path, '{invalid'), (self.network_path, "config 'unterminated")):
            original = path.read_bytes()
            path.write_text(text)
            before = [p.read_bytes() for p in (self.board_path, self.network_path)]
            self.migrate(success=False)
            self.assertEqual([p.read_bytes() for p in (self.board_path, self.network_path)], before)
            self.assertFalse(self.backup.exists())
            path.write_bytes(original)


if __name__ == '__main__':
    unittest.main()
