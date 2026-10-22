# NVIDIA 615.78.08 patches for the RTX6000 BAR preflight build

The framebuffer and accelerator allocation interface changes must be built and installed together. Use the NVRMFB framebuffer source and the matching NVRM headers from this revision.

All patches apply to NVIDIA's `open-gpu-kernel-modules` tag `615.78.08` (commit `4bfe30bb78e7364959e59c16cfefd88e8c14670e`). The modeset patches are applied with `git apply --directory=src/nvidia-modeset` from the vendor tree root, as the workflow does.

`615-display-completion.patch` implements the mapped flip notifier query and resets the selected slot for each committed surface. NVIDIA modeset treats the BEGUN notifier state as the latched flip; FINISHED is also accepted. Invalid or unavailable notifier state keeps the transaction pending. 615.78.08 declares the `getFlipPendingStatus` slot in `nvkms-kapi.h` but ships no implementation, so the patch adds `GetFlipPendingStatus` (using the vendor's own `nvKmsParseNotifier`, `NVKMS_KAPI_NOTIFIER_INDEX` and `NVKMS_NOTIFIER_STATUS_*`) and wires it into the function table.

`610-setmode-reason.patch` applies after it, unchanged from 610.57.04 (the context it anchors is identical in 615). A refused mode set logged only "commit rejected" in release builds (the reason lines are debug-only), so a WindowServer that could not set its first mode stopped with no cause on record. The patch logs one warning with the set-mode status, the display status, and each head's status.

`615-viewport-scaling.patch` applies to `kapi/src/nvkms-kapi.c` (from PR #273 by Leaf-lsgtky, the finding and implementation by lsCoding666 in #221). Primary-layer scaling (`sizeIn != sizeOut`) is not offered on GPUs that scale at the head, so NVKMS refused every scaled desktop and HiDPI mode. The primary layer stays 1:1 and the desktop size becomes the head's viewport input, so the head's scaler fits it to the display's timings.

`610-backlight-register-log.patch` applies to `src/nvkms-rm.c`, unchanged from 610.57.04. A laptop panel driven by the GPU has no brightness control when NVKMS registers no backlight, and both inputs to that decision (the internal display mask and the brightness query) are answered by the GPU firmware with nothing on record. The patch logs the internal display status and masks, each display's brightness query status and level, and the case where none answered. No behaviour change.

`ci/patches/610-macos-version-headers.patch` (kept under its historical name) applies to the 615.78.04/615.78.08 `src/common/inc/nvVer.h` and `nvUnixVersion.h`: it lets the Darwin build read the version guards that NVIDIA reserves for the unix platforms.

With the original NVIDIA 615.78.08 Darwin modeset archive and compile commands already built, run:

```
python3 kexts/NVRM/build-completion-archive.py --vendor /build/vendor/src/nvidia-modeset --output /build/paired-completion
```

Link NVRM against the resulting `libnvkms.a`, not the original archive. The script preserves the original archive and replaces only the completion object and release build metadata. Build NVAccel with the matching allocation cookie interface, once for macOS 15 and once for macOS 26.

## Retired in the 615 rebaseline

`610-fsp-timer-double-destroy.patch` was dropped: 615.78.08 already destroys the FSP poll event once (`kfspCleanupBootState` clears `pKernelFsp->pPollEvent` after `tmrEventDestroy`) and `tmrEventDestroyOSTimer_OSTimer` now guards on `pTmrEvent->super.pOSTmrCBdata != NULL`, which is what the 610 patch worked around (the second destroy of the same event faulted in `nv_destroy_nano_timer`). The old `610-display-completion.patch` was retired with it; its 615 adaptation is `615-display-completion.patch` above.
