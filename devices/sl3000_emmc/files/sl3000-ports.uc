// One-time migration of the exact 02_network + 03_sl3000-network append bug.
import { readfile, open, mkdir, chmod, rename, access } from 'fs';
import { cursor } from 'uci';

// A filesystem prefix allows integration tests to use disposable configurations.
let root = ARGV[0] ?? '';
if (trim(readfile(root + '/tmp/sysinfo/board_name') ?? '') != 'sl,3000-emmc')
    exit(0);

let old_ports = ['lan1', 'lan2', 'lan3', 'lan4', 'lan1', 'lan2', 'lan3'];
let ports = ['lan1', 'lan2', 'lan3'];
function same(a, b) { return sprintf('%J', a) == sprintf('%J', b); }
function require_ok(ok, message) { if (!ok) die(message + '\n'); }

let board_path = root + '/etc/board.json';
let network_path = root + '/etc/config/network';
let board_text = readfile(board_path);
let board = json(board_text);
let fix_board = same(board.network?.lan?.ports, old_ports);
let network_text = readfile(network_path);
require_ok(network_text != null, 'Cannot read network configuration');
// Do not commit unrelated changes staged by another boot hook or a user.
require_ok(!length(readfile(root + '/tmp/.uci/network') ?? ''), 'Pending UCI network changes');
let ctx = cursor(root + '/etc/config', root + '/tmp/.uci', '');
require_ok(ctx.load('network'), 'Cannot parse network configuration');
let sections = ctx.get_all('network');
let bridges = [], devices = [];
for (let id, section in sections) {
    if (section['.type'] != 'device')
        continue;
    if (section.name == 'br-lan')
        push(bridges, section);
    if (index(old_ports, section.name) >= 0)
        push(devices, section);
}

let bridge = bridges[0];
let fix_network = length(bridges) == 1 && bridge.type == 'bridge' && same(bridge.ports, old_ports);
let mac = board.network?.lan?.macaddr;
fix_network = fix_network && type(mac) == 'string' && length(mac) == 17;
fix_network = fix_network && same(map(devices, s => s.name), old_ports);
for (let section in devices) {
    // Never remove named sections or sections containing customized options/MACs.
    if (!section['.anonymous'] || section.macaddr != mac)
        fix_network = false;
    for (let key, value in section)
        if (substr(key, 0, 1) != '.' && key != 'name' && key != 'macaddr')
            fix_network = false;
}

function refers_to_lan4(value) {
    if (type(value) == 'array')
        return length(filter(value, refers_to_lan4)) > 0;
    return type(value) == 'string' && match(value, /(^|\s)lan4([.:\s]|$)/) != null;
}
for (let id, section in sections) {
    for (let key, value in section) {
        if (substr(key, 0, 1) == '.' || (id == bridge?.['.name'] && key == 'ports') ||
            (section['.type'] == 'device' && key == 'name'))
            continue;
        if (refers_to_lan4(value))
            fix_network = false;
    }
}
if (access(root + '/sys/class/net/lan4'))
    fix_network = false;

if (!fix_board && !fix_network)
    exit(0);

let backup = root + '/etc/sl3000-port-backup-v1';
require_ok(mkdir(backup, 0o700) || access(backup), 'Cannot create port migration backup');
require_ok(chmod(backup, 0o700), 'Cannot protect port migration backup');
function write_new(path, content) {
    let file = open(path, 'wx', 0o600);
    require_ok(file, 'Cannot create migration file');
    let count = file.write(content);
    let closed = file.close();
    require_ok(count == length(content) && closed, 'Cannot finish migration file');
}
for (let name, text in { 'board.json': board_text, 'network': network_text })
    if (!access(backup + '/' + name))
        write_new(backup + '/' + name, text);

if (fix_network) {
    require_ok(ctx.set('network', bridge['.name'], 'ports', ports), 'Cannot update bridge ports');
    // Preserve the first three device sections, including their original MACs.
    for (let i = 3; i < length(devices); i++)
        require_ok(ctx.delete('network', devices[i]['.name']), 'Cannot remove generated duplicate');
    require_ok(ctx.commit('network'), 'Cannot commit port migration');
    print('Removed generated duplicate LAN entries and nonexistent lan4.\n');
}
else {
    print('Custom network configuration retained; only board port metadata corrected.\n');
}
if (fix_board) {
    board.network.lan.ports = ports;
    let path = backup + '/board.json.new';
    write_new(path, sprintf('%.J\n', board));
    require_ok(chmod(path, 0o644) && rename(path, board_path), 'Cannot replace board port metadata');
}
