/* Pinned OpenWrt f0a60eee2fe051741c643ea6118718aae1ef17fb; BSD-3-Clause. */
static uc_value_t *
uc_hostapd_iface_stop(uc_vm_t *vm, size_t nargs)
{
	struct hostapd_iface *iface = uc_fn_thisval("hostapd.iface");
	int i;

	if (!iface)
		return NULL;

	if (iface->state != HAPD_IFACE_ENABLED)
		uc_hostapd_disable_iface(iface);

	for (i = 0; i < iface->num_bss; i++) {
		struct hostapd_data *hapd = iface->bss[i];

		hostapd_drv_stop_ap(hapd);
		hapd->beacon_set_done = 0;
	}

	return NULL;
}
