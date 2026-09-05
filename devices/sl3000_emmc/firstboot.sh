#!/bin/sh
# shellcheck shell=dash
# OpenWrt supplies these libraries; callbacks are invoked by config_foreach.
# shellcheck disable=SC1091,SC2317

[ "$(cat /tmp/sysinfo/board_name)" = "sl,3000-emmc" ] || exit 1
. /lib/functions.sh
. /lib/functions/system.sh

# WIFI_PASSWORD_INJECTED_HERE
: "${wifi_password:?Missing setup Wi-Fi password}"

uci set network.lan.ipaddr='192.168.21.1'
uci set network.lan.netmask='255.255.255.0'
uci set dhcp.lan.ignore='0'
uci set dhcp.lan.start='100'
uci set dhcp.lan.limit='150'
uci set dhcp.lan.leasetime='12h'
uci set system.@system[0].hostname='SL3000'
uci set system.@system[0].zonename='Asia/Shanghai'
uci set system.@system[0].timezone='CST-8'
uci set luci.main.lang='zh_cn'

# These packages stay installed but must not capture DNS or open ports at boot.
uci -q set passwall.@global[0].enabled='0'
uci -q set passwall.@global[0].acl_enable='0'
uci -q set openclash.config.enable='0'
uci -q set upnpd.config.enabled='0'
uci -q set upnpd.config.secure_mode='1'
for service in passwall passwall_server openclash tailscale miniupnpd; do
    [ ! -x "/etc/init.d/$service" ] || "/etc/init.d/$service" disable
done
for config in network dhcp system luci passwall openclash upnpd; do
    uci -q commit "$config"
done

# Mainline mt76 generates wireless sections after its drivers load.
/sbin/wifi config
radio_count=0
iface_count=0
base_mac=$(uci -q get network.lan.macaddr)
setup_radio() {
    local section="$1"
    radio_count=$((radio_count + 1))
    uci set "wireless.$section.disabled=0"
    uci set "wireless.$section.country=CN"
}
setup_iface() {
    local section="$1"
    iface_count=$((iface_count + 1))
    uci set "wireless.$section.mode=ap"
    uci set "wireless.$section.network=lan"
    uci set "wireless.$section.ssid=SL3000-Setup"
    uci set "wireless.$section.encryption=sae-mixed"
    uci set "wireless.$section.key=$wifi_password"
    uci set "wireless.$section.disabled=0"
    # Default EEPROM MACs are not unique. Derive per-device AP addresses.
    [ -z "$base_mac" ] || uci set "wireless.$section.macaddr=$(macaddr_add "$base_mac" "$((iface_count + 1))")"
}
config_load wireless
config_foreach setup_radio wifi-device
config_foreach setup_iface wifi-iface
if [ "$radio_count" -eq 0 ] || [ "$iface_count" -eq 0 ]; then
    logger -t sl3000-setup 'Wi-Fi not detected; setup will retry next boot. Use wired LAN.'
    exit 1
fi
uci commit wireless
unset wifi_password
logger -t sl3000-setup 'Public setup Wi-Fi enabled. Replace its password and set a root password.'
exit 0
