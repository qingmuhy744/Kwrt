include $(TOPDIR)/rules.mk

PKG_NAME:=sl3000-default-eeprom
PKG_VERSION:=2026.09.05
PKG_RELEASE:=1
PKG_SOURCE:=mt7981_eeprom_mt7976_dbdc.bin
PKG_SOURCE_URL:=https://raw.githubusercontent.com/openwrt/mt76/39c960c3ada558b4c2e7915772483d3731573d09/firmware/
PKG_HASH:=97238e2228897a7388370604cbe7175e0bfa0cc35f562b64330b12c1aaeeb751
PKG_LICENSE:=LicenseRef-MediaTek

include $(INCLUDE_DIR)/package.mk

define Package/sl3000-default-eeprom
  SECTION:=firmware
  CATEGORY:=Firmware
  TITLE:=MT7981 default EEPROM for SL-3000 bootstrap hardware validation
  DEPENDS:=@TARGET_mediatek_filogic
endef

define Build/Prepare
endef
define Build/Configure
endef
define Build/Compile
endef

define Package/sl3000-default-eeprom/install
	$(INSTALL_DIR) $(1)/lib/firmware/mediatek
	$(INSTALL_DATA) $(DL_DIR)/$(PKG_SOURCE) $(1)/lib/firmware/mediatek/$(PKG_SOURCE)
	$(INSTALL_DIR) $(1)/usr/share/licenses/sl3000-default-eeprom
	$(INSTALL_DATA) ./LICENSE $(1)/usr/share/licenses/sl3000-default-eeprom/LICENSE
endef

$(eval $(call BuildPackage,sl3000-default-eeprom))
