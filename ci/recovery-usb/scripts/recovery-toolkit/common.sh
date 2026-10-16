#!/bin/bash
# Dell 7920 only. Sourced by the USB Recovery entry point; Bash 3.2 compatible.
get() { "$PB" -c "Print :$2" "$1" 2>/dev/null; }
stop() { echo "STOP: $*" >&2; echo "Operation log: ${OUT:-not created}" >&2; exit 1; }
sha() { shasum -a 256 "$1" | awk '{print $1}'; }
same_uuid() { [ "$(printf '%s' "$1" | tr A-Z a-z)" = "$(printf '%s' "$2" | tr A-Z a-z)" ]; }
shell_pairs() {
    local value
    while [ "$#" -ge 2 ]; do
        value=$2
        value=${value//\'/\'\\\'\'}
        printf "%s='%s'\n" "$1" "$value"
        shift 2
    done
}

mount_uuid() {
    local uuid=$1 info=$2
    diskutil info -plist "$uuid" > "$info" || stop "Cannot find the recorded volume $uuid."
    same_uuid "$(get "$info" DiskUUID)" "$uuid" || stop 'Volume identity mismatch.'
    diskutil mount "$uuid" || stop 'Cannot mount the recorded volume. Unlock it in Disk Utility if encrypted.'
    diskutil info -plist "$uuid" > "$info" || stop 'Cannot inspect the mounted volume.'
    same_uuid "$(get "$info" DiskUUID)" "$uuid" || stop 'Mounted volume identity changed.'
}
physical_mount() {
    local mp
    mp=$(get "$1" MountPoint) || return 1
    case "$mp" in /Volumes/*) ;; *) return 1;; esac
    (cd "$mp" && pwd -P)
}
identify_target() {
    diskutil list -plist > "$OUT/disks.plist" || stop 'Cannot enumerate disks.'
    diskutil apfs list -plist > "$OUT/apfs.plist" || stop 'Cannot enumerate APFS.'
    diskutil list > "$OUT/disks.txt"
    diskutil info -plist "$PART_UUID" > "$OUT/physical.plist" || stop 'Recorded macOS partition is missing.'
    same_uuid "$(get "$OUT/physical.plist" DiskUUID)" "$PART_UUID" || stop 'macOS partition identity mismatch.'
    local physical container='' i=0 j dev role found_system=0 found_data=0
    physical=$(get "$OUT/physical.plist" DeviceIdentifier) || stop 'Missing physical device identity.'
    while get "$OUT/apfs.plist" "Containers:$i:ContainerReference" >/dev/null; do
        j=0
        while dev=$(get "$OUT/apfs.plist" "Containers:$i:PhysicalStores:$j:DeviceIdentifier"); do
            if [ "$dev" = "$physical" ]; then [ -z "$container" ] || stop 'Ambiguous physical store.'; container=$i; fi
            j=$((j+1))
        done
        i=$((i+1))
    done
    [ -n "$container" ] || stop 'Recorded partition has no APFS container.'
    mount_uuid "$SYSTEM_UUID" "$OUT/system.plist"
    mount_uuid "$DATA_UUID" "$OUT/data.plist"
    SYSTEM_DEV=$(get "$OUT/system.plist" DeviceIdentifier)
    DATA_DEV=$(get "$OUT/data.plist" DeviceIdentifier)
    j=0
    while dev=$(get "$OUT/apfs.plist" "Containers:$container:Volumes:$j:DeviceIdentifier"); do
        role=$(get "$OUT/apfs.plist" "Containers:$container:Volumes:$j:Roles:0" || true)
        [ "$dev:$role" != "$SYSTEM_DEV:System" ] || found_system=1
        [ "$dev:$role" != "$DATA_DEV:Data" ] || found_data=1
        j=$((j+1))
    done
    [ "$found_system:$found_data" = 1:1 ] || stop 'System/Data roles or physical store do not match.'
    SYSTEM_ROOT=$(physical_mount "$OUT/system.plist") || stop 'Unexpected System mount.'
    DATA_ROOT=$(physical_mount "$OUT/data.plist") || stop 'Unexpected Data mount.'
    [ "$SYSTEM_ROOT" != "$DATA_ROOT" ] && [ -d "$DATA_ROOT/Users" ] || stop 'Invalid System/Data pair.'
    local version="$SYSTEM_ROOT/System/Library/CoreServices/SystemVersion.plist"
    OS_VERSION=$(get "$version" ProductVersion) || stop 'Cannot identify the installed OS.'
    OS_BUILD=$(get "$version" ProductBuildVersion) || stop 'Cannot identify the installed build.'
    [ "$OS_VERSION:$OS_BUILD" = "$EXPECTED_OS:$EXPECTED_BUILD" ] || stop "Target OS changed ($OS_VERSION $OS_BUILD); this kit is for $EXPECTED_OS $EXPECTED_BUILD."
    [ "$(sw_vers -productVersion | cut -d. -f1)" = 15 ] || stop 'Use the Sequoia Recovery entry, not a different major OS.'
    cp "$version" "$OUT/installed-SystemVersion.plist" || stop 'Cannot record the installed build.'
    mount_uuid "$ESP_UUID" "$OUT/esp.plist"
    [ "$(get "$OUT/esp.plist" ParentWholeDisk)" = "$(get "$OUT/physical.plist" ParentWholeDisk)" ] || stop 'EFI and macOS partition are on different physical disks.'
    ESP_ROOT=$(physical_mount "$OUT/esp.plist") || stop 'Unexpected internal EFI mount.'
    CFG="$ESP_ROOT/EFI/OC/config.plist"
    [ -f "$CFG" ] && [ ! -L "$CFG" ] && plutil -lint "$CFG" >/dev/null || stop 'Internal EFI/OC/config.plist is missing or invalid.'
    BAR_APPLE=$(get "$CFG" Booter:Quirks:ResizeAppleGpuBars) || stop 'Missing Apple BAR setting.'
    BAR_UEFI=$(get "$CFG" UEFI:Quirks:ResizeGpuBars) || stop 'Missing UEFI BAR setting.'
    [ "$BAR_APPLE:$BAR_UEFI" = -1:-1 ] || stop "BAR settings changed ($BAR_APPLE / $BAR_UEFI). Refusing to guess or shrink BAR1."
    cp "$CFG" "$OUT/config-before.plist" || stop 'Cannot save the live internal OC config.'
    CFG_SHA=$(sha "$CFG")
    printf 'System=%s (%s)\nData=%s (%s)\nOC=%s\nAppleBAR=%s\nUEFIBAR=%s\n' "$SYSTEM_ROOT" "$SYSTEM_DEV" "$DATA_ROOT" "$DATA_DEV" "$CFG" "$BAR_APPLE" "$BAR_UEFI" | tee "$OUT/target.txt"
    EXT="$DATA_ROOT/Library/Extensions"
    ST="$DATA_ROOT/Library/NullMoth"
    KC="$DATA_ROOT/Library/KernelCollections/AuxiliaryKernelExtensions.kc"
    BOOTKC="$SYSTEM_ROOT/System/Library/KernelCollections/BootKernelExtensions.kc"
    SYSTEMKC="$SYSTEM_ROOT/System/Library/KernelCollections/SystemKernelExtensions.kc"
    KERNEL="$SYSTEM_ROOT/System/Library/Kernels/kernel"
}

# Only these named components may be replaced or restored by a transaction.
allowed_rel() {
    case "$1" in
        Library/Extensions/NVRM.kext|Library/Extensions/NVAccel.kext|Library/Extensions/NVRMFB.kext|Library/Extensions/NVRMAGDC.kext|\
        Library/GPUBundles/NVMTLDriver.bundle|Library/GPUBundles/NVIDIAShared.bundle|Library/GPUBundles/nvmtl|Library/GPUBundles/nvmtl-allow.txt|\
        Users/Shared/nvfw|Library/KernelCollections/AuxiliaryKernelExtensions.kc|\
        Library/NullMoth/kexts|Library/NullMoth/driver-version|Library/NullMoth/os-major|Library/NullMoth/state|Library/NullMoth/update-pending|Library/NullMoth/install.lock|\
        Library/NullMoth/nullmoth-setup.sh|Library/NullMoth/uninstall.sh|Library/NullMoth/nullmoth-recover.sh|Library/NullMoth/nullmoth-displaytrace.sh|Library/NullMoth/nullmoth-wsreset.sh|Library/NullMoth/offline-build.json|\
        Library/LaunchDaemons/com.nullmoth.recover.plist|Library/LaunchDaemons/com.nullmoth.osupdate.plist|Library/LaunchDaemons/com.nullmoth.displaytrace.plist|Library/LaunchDaemons/com.nullmoth.wsreset.plist|Library/LaunchAgents/com.nullmoth.crashcheck.plist) return 0;;
        *) return 1;;
    esac
}
safe_data_path() {
    local rel=$1 d="$DATA_ROOT" part rest
    allowed_rel "$rel" || stop "Unapproved target path: $rel"
    rest=$rel
    while [ -n "$rest" ]; do
        part=${rest%%/*}; d="$d/$part"
        [ ! -L "$d" ] || stop "Refusing a symlink in the target path: $d"
        [ "$rest" != "$part" ] || break
        rest=${rest#*/}
    done
}
check_work_path() {
    case "$WORK" in "$DATA_ROOT"/Users/Shared/NullMoth-Offline.*) ;; *) stop 'Invalid transaction directory.';; esac
    [ -d "$WORK" ] && [ ! -L "$WORK" ] && [ "$(stat -f %u "$WORK")" = 0 ] || stop 'Untrusted transaction directory.'
    [ "$(cd "$WORK" && pwd -P)" = "$WORK" ] || stop 'Transaction directory traverses a symlink.'
}
new_work() {
    # All staging/backups live on the same APFS Data volume, not the small Recovery RAM disk or FAT32.
    [ ! -L "$DATA_ROOT/Users" ] && [ ! -L "$DATA_ROOT/Users/Shared" ] || stop 'Unexpected Users/Shared symlink.'
    WORK=$(mktemp -d "$DATA_ROOT/Users/Shared/NullMoth-Offline.XXXXXX") || stop 'Cannot create a private staging directory.'
    chmod 700 "$WORK" || stop 'Cannot protect the transaction directory.'
    check_work_path
    mkdir "$WORK/old" "$WORK/stage" "$WORK/repo" || stop 'Cannot prepare the transaction.'
}
prepare_config() {
    CFG_NEW="$WORK/config-next.plist"
    cp "$CFG" "$WORK/config-before.plist" && cp "$CFG" "$CFG_NEW" || stop 'Cannot stage the OC config.'
    local args new='' a i=0 ident found=0 value
    args=$(get "$CFG_NEW" "NVRAM:Add:$GUID:boot-args") || stop 'Cannot read boot arguments.'
    for a in $args; do [ "$a" = -nvoff ] || new="$new $a"; done
    plutil -replace "NVRAM.Add.$GUID.boot-args" -string "${new# }" "$CFG_NEW" || stop 'Cannot clear the temporary driver-off flag.'
    # OC clears stale remove requests and rewrites boot-args at the next start; no firmware writes here.
    if get "$CFG_NEW" "NVRAM:Add:$GUID:nullmoth-remove" >/dev/null; then
        "$PB" -c "Delete :NVRAM:Add:$GUID:nullmoth-remove" "$CFG_NEW" || stop 'Cannot remove a persistent removal request.'
    fi
    get "$CFG_NEW" "NVRAM:Delete:$GUID" >/dev/null || "$PB" -c "Add :NVRAM:Delete:$GUID array" "$CFG_NEW" || stop 'Missing NVRAM Delete dictionary.'
    for a in boot-args nullmoth-remove; do
        i=0; found=0
        while value=$(get "$CFG_NEW" "NVRAM:Delete:$GUID:$i"); do [ "$value" != "$a" ] || found=1; i=$((i+1)); done
        [ "$found" = 1 ] || "$PB" -c "Add :NVRAM:Delete:$GUID: string $a" "$CFG_NEW" || stop 'Cannot stage NVRAM cleanup.'
    done
    i=0; found=0
    while ident=$(get "$CFG_NEW" "Kernel:Block:$i:Identifier"); do
        if [ "$ident" = com.apple.iokit.IONDRVSupport ]; then
            found=$((found+1))
            value=false; [ "$ACTION" != install ] || value=true
            plutil -replace "Kernel.Block.$i.Enabled" -bool "$value" "$CFG_NEW" || stop 'Cannot stage firmware-framebuffer setting.'
        fi
        i=$((i+1))
    done
    [ "$found" = 1 ] || stop 'Expected exactly one existing IONDRVSupport block entry.'
    [ "$(get "$CFG_NEW" Booter:Quirks:ResizeAppleGpuBars)" = "$BAR_APPLE" ] &&
    [ "$(get "$CFG_NEW" UEFI:Quirks:ResizeGpuBars)" = "$BAR_UEFI" ] || stop 'BAR preservation check failed.'
    plutil -lint "$CFG_NEW" >/dev/null || stop 'Staged OC configuration is invalid.'
}

build_kc() {
    local repo=$1 candidate=$2 label=$3
    [ -s "$BOOTKC" ] && [ -s "$SYSTEMKC" ] || stop 'The installed base kernel collections are missing.'
    local args=(create -n aux --volume-root / -a x86_64 -V release --allow-missing-kdk)
    [ ! -f "$KERNEL" ] || args+=(--kernel "$KERNEL")
    args+=(-B "$BOOTKC" -S "$SYSTEMKC" --repository "$repo" -A "$candidate" -z)
    # Bash 3.2's %q can emit malformed UTF-8 for a Chinese mount name. The
    # actual command uses the untouched array; diagnostics print each argument.
    printf 'kmutil'; printf ' <%s>' "${args[@]}"; printf '\n'
    kmutil "${args[@]}" > "$OUT/kmutil-$label.txt" 2>&1 || { tail -n 30 "$OUT/kmutil-$label.txt"; stop 'Kernel collection build failed; previous installation will be retained/restored.'; }
    [ -s "$candidate" ] || stop 'kmutil produced no collection.'
    kmutil inspect -a x86_64 -A "$candidate" > "$OUT/inspect-$label.txt" 2>&1 || stop 'Cannot inspect the candidate collection.'
    verify_kc "$OUT/inspect-$label.txt"
}
verify_kc() {
    local file=$1 k ident
    for k in $KEXTS; do
        if [ "$ACTION" = install ]; then
            grep -oE 'com\.nullmoth\.[A-Za-z0-9]+' "$file" | grep -Fxq "com.nullmoth.$k" || stop "Collection is missing com.nullmoth.$k."
        else
            if grep -oE 'com\.nullmoth\.[A-Za-z0-9]+' "$file" | grep -Fxq "com.nullmoth.$k"; then stop "Removed driver still in collection: $k"; fi
        fi
    done
    while IFS= read -r ident; do
        [ -z "$ident" ] || grep -Fq "$ident" "$file" || stop "Collection lost an unrelated extension: $ident"
    done < "$WORK/other-ids.txt"
}
prepare_repo() {
    local x n ident k
    : > "$WORK/other-ids.txt"
    for x in "$EXT"/*.kext; do
        [ -d "$x" ] || continue
        n=$(basename "$x" .kext)
        case " $KEXTS " in *" $n "*) continue;; esac
        case "$n" in NMXGPU|NMXAccel|NMXNPU) stop 'This NVIDIA-only kit does not update an enabled AMD/Intel/NPU driver.';; esac
        [ ! -L "$x" ] || stop 'Unexpected third-party kext symlink.'
        ditto "$x" "$WORK/repo/$n.kext" || stop "Cannot preserve $n."
        if [ -d "$x/Contents/MacOS" ]; then
            ident=$(get "$x/Contents/Info.plist" CFBundleIdentifier) || stop "Cannot identify $n."
            [ -n "$ident" ] || stop 'Empty extension identifier.'
            printf '%s\n' "$ident" >> "$WORK/other-ids.txt"
        fi
    done
    if [ "$ACTION" = install ]; then
        for k in $KEXTS; do ditto "$WORK/stage/Library/Extensions/$k.kext" "$WORK/repo/$k.kext" || stop "Cannot stage $k."; done
    fi
    chown -R root:wheel "$WORK/repo" && chmod -R go-w "$WORK/repo" || stop 'Cannot set repository permissions.'
}

# Persistent journal allows the same entry point to roll back after a power loss.
txn_begin() {
    mkdir -p "$ST" || stop 'Cannot create the offline transaction record.'
    [ ! -L "$ST/offline-operation" ] || stop 'Unexpected transaction record symlink.'
    : > "$WORK/journal"
    printf '%s\n' "$WORK" > "$ST/offline-operation" || stop 'Cannot record the transaction.'
    TX_ACTIVE=1
}
txn_save() {
    local rel=$1 had=0
    safe_data_path "$rel"
    mkdir -p "$(dirname "$WORK/old/$rel")" || stop 'Cannot prepare the backup destination.'
    [ ! -e "$DATA_ROOT/$rel" ] || had=1
    printf '%s|%s\n' "$had" "$rel" >> "$WORK/journal" || stop 'Cannot record backup intent.'
    # Journal is durable before the atomic rename. Rollback also handles failure before that rename.
    sync
    if [ "$had" = 1 ]; then mv "$DATA_ROOT/$rel" "$WORK/old/$rel" || stop "Cannot preserve $rel."; fi
}
txn_put() {
    local rel=$1 source=$2
    safe_data_path "$rel"
    [ ! -e "$DATA_ROOT/$rel" ] || stop "Destination was recreated during the offline operation: $rel"
    mkdir -p "$(dirname "$DATA_ROOT/$rel")" && ditto "$source" "$DATA_ROOT/$rel" || stop "Cannot install $rel."
}
rollback_transaction() {
    local had rel failed=0
    check_work_path
    echo "Restoring previous files from $WORK/old"
    while IFS='|' read -r had rel; do
        case "$had" in 0|1) ;; *) echo 'Invalid rollback journal'; return 1;; esac
        safe_data_path "$rel"
        if [ "$had" = 1 ] && [ -e "$WORK/old/$rel" ]; then
            rm -rf "$DATA_ROOT/$rel" && mkdir -p "$(dirname "$DATA_ROOT/$rel")" && mv "$WORK/old/$rel" "$DATA_ROOT/$rel" || failed=1
        elif [ "$had" = 0 ]; then rm -rf "$DATA_ROOT/$rel" || failed=1
        elif [ ! -e "$DATA_ROOT/$rel" ]; then echo "Missing original and backup: $rel"; failed=1
        fi
    done < "$WORK/journal"
    if [ -f "$WORK/config-publish-intent" ]; then
        cp "$WORK/config-before.plist" "$CFG" || failed=1
        [ -L "$CFG.offline-new" ] || rm -f "$CFG.offline-new" || failed=1
    fi
    [ ! -f "$WORK/config-before.plist" ] || [ "$(sha "$CFG")" = "$(sha "$WORK/config-before.plist")" ] || failed=1
    if [ "$failed" = 0 ]; then
        rm -f "$ST/offline-operation" || return 1
        : > "$WORK/ROLLED_BACK"
        TX_ACTIVE=0; sync
        echo 'Previous driver and OC configuration restored.'
    fi
    return "$failed"
}
resume_interrupted() {
    [ ! -e "$ST/offline-operation" ] || {
        [ -f "$ST/offline-operation" ] && [ ! -L "$ST/offline-operation" ] || stop 'Invalid interrupted-operation record.'
        WORK=$(cat "$ST/offline-operation")
        check_work_path
        [ -f "$WORK/journal" ] || stop 'Interrupted transaction has no journal.'
        if [ -f "$WORK/COMMITTED" ]; then rm -f "$ST/offline-operation" || stop 'Cannot finish the committed record.'
        else rollback_transaction || stop 'Interrupted operation could not be restored; backup retained.'; fi
    }
}
txn_commit() {
    [ "$(sha "$CFG")" = "$CFG_SHA" ] || stop 'OC config changed during this operation.'
    : > "$WORK/config-publish-intent"; sync
    local temporary="$CFG.offline-new"
    [ ! -e "$temporary" ] || stop 'A previous EFI publication is incomplete.'
    cp "$CFG_NEW" "$temporary" && cmp -s "$CFG_NEW" "$temporary" && mv "$temporary" "$CFG" || stop 'Cannot publish the staged OC config.'
    [ "$(sha "$CFG")" = "$(sha "$CFG_NEW")" ] || stop 'Published OC config does not match.'
    cp "$CFG" "$OUT/config-after.plist" || stop 'Cannot save the published config.'
    sync
    : > "$WORK/COMMITTED" || stop 'Cannot record the committed transaction.'
    sync
    TX_ACTIVE=0
    rm -f "$ST/offline-operation" || echo 'NOTE: committed transaction marker will be cleared next time.'
}
