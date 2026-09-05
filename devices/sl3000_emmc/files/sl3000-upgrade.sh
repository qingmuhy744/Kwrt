# shellcheck shell=dash
# Only the observed SL-3000 bootstrap GPT layout is supported. No disk writes here.
sl3000_check_image() {
    local image="$1" kernel_size root_size
    if ! { [ "$(find_mmc_part kernel mmcblk0)" = /dev/mmcblk0p1 ] &&
    [ "$(find_mmc_part rootfs mmcblk0)" = /dev/mmcblk0p2 ] &&
    [ "$(find_mmc_part storage mmcblk0)" = /dev/mmcblk0p3 ] &&
    [ "$(cat /sys/class/block/mmcblk0p1/start)" = 8192 ] &&
    [ "$(cat /sys/class/block/mmcblk0p1/size)" = 65536 ] &&
    [ "$(cat /sys/class/block/mmcblk0p2/start)" = 73728 ] &&
    [ "$(cat /sys/class/block/mmcblk0p2/size)" = 4096000 ] &&
    [ "$(cat /sys/class/block/mmcblk0p3/start)" = 4169728 ]; }; then
        echo 'Unsupported SL-3000 partition layout. Do not repartition or force upgrade.'
        return 1
    fi
    tar tf "$image" >/dev/null 2>&1 || return 1
    kernel_size=$(tar -xOf "$image" sysupgrade-sl_3000-emmc/kernel 2>/dev/null | wc -c)
    root_size=$(tar -xOf "$image" sysupgrade-sl_3000-emmc/root 2>/dev/null | wc -c)
    if ! { [ "$kernel_size" -gt 0 ] && [ "$kernel_size" -le 33554432 ] &&
    [ "$root_size" -gt 0 ] && [ "$root_size" -le 2097086464 ]; }; then
        echo 'Image payload is missing or exceeds the existing system partitions.'
        return 1
    fi
    return 0
}
