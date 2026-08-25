SHELL := /bin/sh
.DEFAULT_GOAL := help

.PHONY: help prepare-ai-bundle build-iso build-iso-clean flash-usb vm vm-headless vm-gui-probe validate validate-config test-iso inspect-iso verify-reproducible clean

help:
	@echo "ARES OS build targets:"
	@echo "  make prepare-ai-bundle Download and stage the offline Ollama/Qwen bundle"
	@echo "  make build-iso       Build iso/ARES.iso in the pinned Debian container"
	@echo "  make build-iso-clean Build without the persistent live-build cache"
	@echo "  make flash-usb       Flash iso/ARES.iso to ARES_USB_DEVICE=/dev/sdX"
	@echo "  make vm              Boot iso/ARES.iso in an interactive VM"
	@echo "  make vm-headless     Boot iso/ARES.iso headlessly and retain evidence"
	@echo "  make vm-gui-probe    Boot a VNC VM, capture the graphical screen, retain evidence"
	@echo "  make validate        Run fast, network-free source validation"
	@echo "  make validate-config Validate the configuration with Debian live-build"
	@echo "  make test-iso        Run BIOS/UEFI smoke tests for iso/ARES.iso"
	@echo "  make inspect-iso     Inspect boot and checksum metadata"
	@echo "  make verify-reproducible Build twice cleanly and compare ISO bytes"
	@echo "  make clean           Remove only generated ARES OS build artifacts"

prepare-ai-bundle:
	./scripts/prepare_ai_bundle.sh

build-iso:
	./scripts/build_iso.sh

build-iso-clean:
	ARES_CLEAN_BUILD=1 ./scripts/build_iso.sh

flash-usb:
	@test -n "$(ARES_USB_DEVICE)" || { echo "Usage: make flash-usb ARES_USB_DEVICE=/dev/sdX"; exit 2; }
	./scripts/flash_usb.sh --device "$(ARES_USB_DEVICE)" --yes

vm:
	./scripts/run_vm_iso.sh --iso iso/ARES.iso --firmware uefi-secure --boot hybrid --display gtk

vm-headless:
	./scripts/vm_test_cases.sh --iso iso/ARES.iso --profile uefi-secure-hybrid

vm-gui-probe:
	./scripts/vm_gui_probe.sh --iso iso/ARES.iso --firmware uefi-secure --boot hybrid

validate:
	./scripts/validate_live.sh

validate-config:
	./scripts/build_iso.sh --config-only

test-iso:
	./scripts/smoke_test_iso.sh iso/ARES.iso

inspect-iso:
	./scripts/inspect_iso.sh iso/ARES.iso

verify-reproducible:
	./scripts/verify_reproducible.sh

clean:
	./scripts/clean_live.sh
