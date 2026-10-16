#!/bin/bash
# USB menu: offline driver replacement, log collection, and removal.
set -u
set -o pipefail
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH COPYFILE_DISABLE=1
umask 022
HERE=$(cd "$(dirname "$0")" && pwd -P) || exit 1
PB=/usr/libexec/PlistBuddy
. "$HERE/common.sh" || exit 1
[ "$(uname -s):$(uname -m):$(id -u)" = Darwin:x86_64:0 ] || stop 'Open Terminal in Intel macOS Recovery.'
[ -d /System/Installation/CDIS ] && [ ! -d /System/Volumes/Data/Users ] || stop 'This entry is for Recovery, not the running installed macOS.'
MANIFEST="$HERE/kit.plist"
PART_UUID=$(get "$MANIFEST" Target:PartitionUUID) || stop 'Kit target manifest is missing.'
SYSTEM_UUID=$(get "$MANIFEST" Target:SystemUUID)
DATA_UUID=$(get "$MANIFEST" Target:DataUUID)
ESP_UUID=$(get "$MANIFEST" Target:ESPUUID)
EXPECTED_OS=$(get "$MANIFEST" Target:OSVersion)
EXPECTED_BUILD=$(get "$MANIFEST" Target:OSBuild)
GUID=7C436110-AB2A-4BBB-A880-FE41995C9F82
KEXTS='NVRM NVAccel NVRMFB NVRMAGDC'
USB_ROOT=$(cd "$HERE/../.." && pwd -P) || stop 'Cannot locate the USB root.'
case "$USB_ROOT" in /Volumes/*) ;; *) stop 'Start the kit from the USB volume.';; esac
echo
echo 'Dell 7920 / RTX6000 - Recovery driver toolkit 1'
echo "Package: $(get "$MANIFEST" Package:Label)"
echo '1  Install / replace driver (no prior uninstall needed)'
echo '2  Collect logs only'
echo '3  Collect logs, then uninstall driver'
echo '0  Exit'
printf 'Select [0-3]: '
IFS= read -r choice || exit 0
case "$choice" in 1) ACTION=install;; 2) ACTION=collect;; 3) ACTION=remove;; 0|'') exit 0;; *) stop 'Unknown menu selection.';; esac
OUT="$USB_ROOT/NullMoth/RecoveryLogs/$(date +%Y%m%d-%H%M%S)-$ACTION-$$"
mkdir -p "$OUT" || stop 'Cannot write the USB operation log.'
exec > >(tee -a "$OUT/operation.txt") 2>&1
TX_ACTIVE=0
finish() {
    local rc=$?
    if [ "$TX_ACTIVE" = 1 ]; then
        if ! rollback_transaction; then echo "RESTORE INCOMPLETE: keep Recovery open; backup: $WORK"; rc=1; fi
    fi
    exit "$rc"
}
trap finish EXIT
trap 'stop "Interrupted; restoring the previous installation."' HUP INT TERM
sw_vers > "$OUT/recovery-os.txt"
identify_target
collect() {
    NULLMOTH_COLLECTION_OUT="$OUT/diagnostics" NULLMOTH_OC_CONFIG="$CFG" NULLMOTH_USB_ROOT="$USB_ROOT" \
        /bin/bash "$HERE/collect.sh"
}
if [ "$ACTION" = collect ]; then collect || stop 'Log collection was incomplete; partial files are retained.'; echo "DONE: $OUT"; exit 0; fi
if [ "$ACTION" = remove ]; then
    echo 'Collecting evidence BEFORE removing any driver files...'
    collect || stop 'Collection was incomplete. No driver files removed; logs retained.'
fi
resume_interrupted
CFG_SHA=$(sha "$CFG")
BAR_APPLE=$(get "$CFG" Booter:Quirks:ResizeAppleGpuBars)
BAR_UEFI=$(get "$CFG" UEFI:Quirks:ResizeGpuBars)
[ "$BAR_APPLE:$BAR_UEFI" = -1:-1 ] || stop 'Recovered configuration has unexpected BAR values.'
for k in $KEXTS; do
    safe_data_path "Library/Extensions/$k.kext"
    if [ -e "$EXT/$k.kext" ]; then
        [ "$(get "$EXT/$k.kext/Contents/Info.plist" CFBundleIdentifier)" = "com.nullmoth.$k" ] || stop "Unexpected installed $k identity."
    fi
done
for rel in Library/NullMoth/state Library/NullMoth/install.lock Library/GPUBundles/nvmtl Users/Shared/nvfw Library/KernelCollections/AuxiliaryKernelExtensions.kc; do safe_data_path "$rel"; done
free_kb=$(df -Pk "$DATA_ROOT" | awk 'END {print $4}')
case "$free_kb" in ''|*[!0-9]*) stop 'Cannot measure free space.';; esac
[ "$free_kb" -ge 4194304 ] || stop 'At least 4 GiB free is required on the installed Data volume.'
new_work
echo "Staging and rollback: $WORK"
prepare_config
if [ "$ACTION" = install ]; then . "$HERE/install.sh"; prepare_install; fi
prepare_repo
if [ "$ACTION" = install ] || [ -s "$WORK/other-ids.txt" ]; then
    build_kc "$WORK/repo" "$WORK/preflight.kc" preflight
fi
# All preflight work is complete. Save/move only the explicit owned components.
txn_begin
for k in $KEXTS; do txn_save "Library/Extensions/$k.kext"; done
for b in NVMTLDriver.bundle NVIDIAShared.bundle nvmtl nvmtl-allow.txt; do txn_save "Library/GPUBundles/$b"; done
txn_save Users/Shared/nvfw
for n in kexts driver-version os-major state update-pending install.lock nullmoth-setup.sh uninstall.sh nullmoth-recover.sh nullmoth-displaytrace.sh nullmoth-wsreset.sh offline-build.json; do txn_save "Library/NullMoth/$n"; done
for n in recover osupdate displaytrace wsreset; do txn_save "Library/LaunchDaemons/com.nullmoth.$n.plist"; done
txn_save Library/LaunchAgents/com.nullmoth.crashcheck.plist
txn_save Library/KernelCollections/AuxiliaryKernelExtensions.kc
if [ "$ACTION" = install ]; then
    while IFS= read -r rel; do txn_put "$rel" "$WORK/stage/$rel"; done < "$WORK/install-list.txt"
    # Official installer also rebuilds against the final /Library/Extensions paths.
    build_kc "$EXT" "$WORK/final.kc" installed
    txn_put Library/KernelCollections/AuxiliaryKernelExtensions.kc "$WORK/final.kc"
elif [ -s "$WORK/other-ids.txt" ]; then
    build_kc "$EXT" "$WORK/final.kc" removed
    txn_put Library/KernelCollections/AuxiliaryKernelExtensions.kc "$WORK/final.kc"
fi
txn_commit
printf 'Action=%s\nRollback=%s\nOC=%s\nAppleBAR=%s\nUEFIBAR=%s\n' "$ACTION" "$WORK" "$CFG" "$BAR_APPLE" "$BAR_UEFI" > "$OUT/SUCCESS.txt"
echo
echo "SUCCESS: $ACTION complete. BAR settings preserved: $BAR_APPLE / $BAR_UEFI."
echo 'Restart manually. Select Mac to test the driver, or Windows to review the logs.'
echo "Logs: $OUT"
echo "Previous files: $WORK/old"
# Only expendable staging is cleared; previous installation and journal are retained.
check_work_path
rm -rf "$WORK/payload" "$WORK/stage" "$WORK/repo"
sync
