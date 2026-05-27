VERSION := 0.2.0
IMAGE_NAME := huginn-vm-endpoint-$(VERSION)
OUTPUT_DIR := output
RAW := $(OUTPUT_DIR)/vm-endpoint.raw
ARTIFACT := $(OUTPUT_DIR)/$(IMAGE_NAME).raw.xz

.PHONY: image clean publish staging

# Build the raw image. The agent source is dropped into the rootfs via
# mkosi.extra/usr/local/lib/huginn-vm-endpoint — mkosi copies it verbatim into
# the image during build, no postinst gymnastics needed.
image: $(ARTIFACT)

$(RAW): mkosi.conf mkosi.postinst $(shell find agent mkosi.extra -type f)
	@command -v mkosi >/dev/null || { echo "mkosi not installed (apt install mkosi)"; exit 1; }
	@mkdir -p $(OUTPUT_DIR)
	# Copy the agent into the rootfs overlay so mkosi includes it.
	rm -rf mkosi.extra/usr/local/lib/huginn-vm-endpoint
	mkdir -p mkosi.extra/usr/local/lib/huginn-vm-endpoint
	cp -a agent/. mkosi.extra/usr/local/lib/huginn-vm-endpoint/
	find mkosi.extra/usr/local/lib/huginn-vm-endpoint -name __pycache__ -prune -exec rm -rf {} +
	mkosi --force build

$(ARTIFACT): $(RAW)
	xz -T0 -9 -k -f $(RAW)
	mv $(RAW).xz $(ARTIFACT)
	cd $(OUTPUT_DIR) && sha256sum $(notdir $(ARTIFACT)) > $(notdir $(ARTIFACT)).sha256
	@echo
	@echo "Built: $(ARTIFACT)"
	@du -h $(ARTIFACT)

clean:
	rm -rf $(OUTPUT_DIR) .build-staging

# Sync the built artifact + checksum + installer to the Hetzner downloads
# dir served by Traefik at https://monitoring.danmagi.io/downloads/vm-image/.
# The installer is versionless (always served as install-vm-image.sh) so that
# bookmarks and copy-pasted instructions don't break across releases.
publish: $(ARTIFACT)
	rsync -av \
		$(OUTPUT_DIR)/$(IMAGE_NAME).raw.xz \
		$(OUTPUT_DIR)/$(IMAGE_NAME).raw.xz.sha256 \
		install.sh \
		huginn-prod:/home/docker/huginn-downloads/vm-image/
	ssh huginn-prod 'cd /home/docker/huginn-downloads/vm-image && \
		ln -sf $(IMAGE_NAME).raw.xz huginn-vm-endpoint-latest.raw.xz && \
		ln -sf $(IMAGE_NAME).raw.xz.sha256 huginn-vm-endpoint-latest.raw.xz.sha256 && \
		mv install.sh install-vm-image.sh'

# Local sanity check: spin the freshly built image up under qemu with two
# virtio NICs (no networking; smoke test only).
staging: $(RAW)
	qemu-system-x86_64 -m 512 -smp 2 -nographic \
		-drive file=$(RAW),format=raw,if=virtio \
		-netdev user,id=n0 -device virtio-net,netdev=n0,mac=52:54:00:aa:bb:c0 \
		-netdev user,id=n1 -device virtio-net,netdev=n1,mac=52:54:00:aa:bb:c1 \
		-append "huginn.host_mac=52:54:00:aa:bb:c0 huginn.api_url=https://sensor.optaro.io"
