SHELL := /bin/sh
.DEFAULT_GOAL := help

.PHONY: help build-iso build-iso-clean validate validate-config test-iso inspect-iso verify-reproducible clean

help:
	@echo "ARES OS build targets:"
	@echo "  make build-iso       Build iso/ARES.iso in the pinned Debian container"
	@echo "  make build-iso-clean Build without the persistent live-build cache"
	@echo "  make validate        Run fast, network-free source validation"
	@echo "  make validate-config Validate the configuration with Debian live-build"
	@echo "  make test-iso        Run BIOS/UEFI smoke tests for iso/ARES.iso"
	@echo "  make inspect-iso     Inspect boot and checksum metadata"
	@echo "  make verify-reproducible Build twice cleanly and compare ISO bytes"
	@echo "  make clean           Remove only generated ARES OS build artifacts"

build-iso:
	./scripts/build_iso.sh

build-iso-clean:
	ARES_CLEAN_BUILD=1 ./scripts/build_iso.sh

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
