#!/bin/bash
# Sourced only after the exact offline System/Data pair has been verified.
stage_copy() {
    local rel=$1 source=$2
    safe_data_path "$rel"
    mkdir -p "$(dirname "$WORK/stage/$rel")" && ditto "$source" "$WORK/stage/$rel" || stop "Cannot stage $rel."
    printf '%s\n' "$rel" >> "$WORK/install-list.txt" || stop 'Cannot record the staged payload.'
}
prepare_install() {
    local package expected actual payload k b dir a args csr config_sha
    package=$(get "$MANIFEST" Package:File)
    case "$package" in Packages/*.tar.gz) ;; *) stop 'Invalid package filename in the kit manifest.';; esac
    case "$package" in *..*) stop 'Invalid package path.';; esac
    package="$HERE/$package"
    expected=$(get "$MANIFEST" Package:SHA256)
    echo 'Checking the pinned driver archive (offline; no downloads)...'
    [ -f "$package" ] || stop 'Driver archive is missing.'
    actual=$(sha "$package") || stop 'Cannot calculate the driver archive SHA-256.'
    [ "$actual" = "$expected" ] || stop 'Driver archive SHA-256 mismatch.'
    args=$(get "$CFG_NEW" "NVRAM:Add:$GUID:boot-args")
    for a in nvfb=1 nvaccel=1; do case " $args " in *" $a "*) ;; *) stop "Current OC is missing $a; no automatic configuration guesses.";; esac; done
    # plutil returns plist <data> as Base64; compare directly without a missing decoder.
    csr=$(plutil -extract "NVRAM.Add.$GUID.csr-active-config" raw -o - "$CFG_NEW") || stop 'Cannot read the recorded SIP configuration.'
    [ "$csr" = QwoAAA== ] || stop 'The recorded SIP configuration changed. No driver files replaced.'
    mkdir "$WORK/payload" || stop 'Cannot prepare package extraction.'
    tar -xzf "$package" -C "$WORK/payload" || stop 'Cannot extract the verified driver archive.'
    payload="$WORK/payload/pkgroot"
    verify_manifest "$payload" "$payload/SHA256SUMS" > "$OUT/package-check.txt" 2>&1 || stop 'Payload manifest verification failed.'
    [ "$(cat "$payload/VERSION")" = "$(get "$MANIFEST" Package:Version)" ] || stop 'Package version mismatch.'
    # Same Mach-O minimum-OS check as the app, evaluated for the INSTALLED OS.
    chmod 755 "$HERE/Support/nullmoth-runtime-check" 2>/dev/null || true
    /bin/bash "$HERE/Support/nullmoth-runtime-check.sh" "$payload" "$OS_VERSION" > "$OUT/runtime-check.txt" 2>&1 || stop 'Runtime compatibility check failed.'
    : > "$WORK/install-list.txt"
    for k in $KEXTS; do
        dir="$payload/Library/Extensions/$k.kext"
        [ "$k" != NVAccel ] || dir="$payload/Library/NullMoth/kexts/15/NVAccel.kext"
        [ "$(get "$dir/Contents/Info.plist" CFBundleIdentifier)" = "com.nullmoth.$k" ] || stop "Incorrect payload identity: $k"
        stage_copy "Library/Extensions/$k.kext" "$dir"
        chown -R root:wheel "$WORK/stage/Library/Extensions/$k.kext" && chmod -R 755 "$WORK/stage/Library/Extensions/$k.kext" || stop 'Cannot prepare kext permissions.'
    done
    for b in NVMTLDriver.bundle NVIDIAShared.bundle nvmtl nvmtl-allow.txt; do
        stage_copy "Library/GPUBundles/$b" "$payload/Library/GPUBundles/$b"
    done
    stage_copy Users/Shared/nvfw "$payload/Users/Shared/nvfw"
    stage_copy Library/NullMoth/kexts "$payload/Library/NullMoth/kexts"
    for a in 15 26; do
        dir="$WORK/stage/Library/NullMoth/kexts/$a"
        write_kext_manifest "$dir" || stop 'Cannot create/verify the accelerator manifest.'
    done
    cp "$payload/VERSION" "$WORK/version"
    printf '15\n' > "$WORK/os-major"
    stage_copy Library/NullMoth/driver-version "$WORK/version"
    stage_copy Library/NullMoth/os-major "$WORK/os-major"
    stage_copy Library/NullMoth/offline-build.json "$payload/EXPERIMENTAL-BUILD.json"
    # Install the same logging/one-shot helpers as the paired app, including the BAR-preserving remover.
    for a in nullmoth-setup.sh uninstall.sh nullmoth-recover.sh nullmoth-displaytrace.sh nullmoth-wsreset.sh; do
        stage_copy "Library/NullMoth/$a" "$HERE/Support/$a"
    done
    for a in recover displaytrace wsreset; do
        stage_copy "Library/LaunchDaemons/com.nullmoth.$a.plist" "$HERE/Support/com.nullmoth.$a.plist"
    done
    # Current config is the restore reference; never resurrect an earlier Apple BAR=0 backup.
    config_sha=$(sha "$CFG_NEW") || stop 'Cannot hash the staged configuration.'
    {
        echo '# Recovery toolkit install record'
        shell_pairs EFI_UUID "$ESP_UUID" CONFIG_PATH "$CFG" OCREL EFI/OC \
            CONFIG_BACKUP_REL '' CONFIG_BACKUP "$WORK/config-before.plist" \
            CONFIG_SHA_AFTER "$config_sha" ADDED_ARGS '' REMOVED_ARGS '' DRIVER_BACKUP ''
    } > "$WORK/state" || stop 'Cannot write the staged driver state.'
    stage_copy Library/NullMoth/state "$WORK/state"
    chown -R root:wheel "$WORK/stage" && chmod -R go-w,a+rX "$WORK/stage" || stop 'Cannot protect the staged driver.'
    chmod 600 "$WORK/stage/Library/NullMoth/state"
    chmod 755 "$WORK/stage/Library/NullMoth/"*.sh
    chmod 644 "$WORK/stage/Library/LaunchDaemons/"*.plist "$WORK/stage/Library/GPUBundles/nvmtl-allow.txt"
    # Use the installed OS's account database; Recovery's directory service may have different numeric IDs.
    local wsuid wsgid userplist="$DATA_ROOT/private/var/db/dslocal/nodes/Default/users/_windowserver.plist"
    if [ ! -f "$userplist" ]; then
        userplist="$SYSTEM_ROOT/System/Library/DirectoryServices/DefaultLocalDB/Default/users/_windowserver.plist"
    fi
    wsuid=$(get "$userplist" uid:0) || stop 'Cannot identify the installed WindowServer account.'
    wsgid=$(get "$userplist" gid:0) || stop 'Cannot identify the installed WindowServer group.'
    case "$wsuid:$wsgid" in *[!0-9:]*|:*|*:) stop 'Invalid WindowServer account identity.';; esac
    for a in aircache spvcache; do
        dir="$WORK/stage/Library/GPUBundles/nvmtl/$a"
        mkdir -p "$dir" && chown -R "$wsuid:$wsgid" "$dir" && chmod 755 "$dir" || stop 'Cannot prepare the protected shader cache.'
    done
    # Existing system-specific NVK knobs are retained instead of silently resetting the experiment.
    local current="$DATA_ROOT/Library/GPUBundles/nvmtl/nvrm610.conf"
    if [ -f "$current" ] && [ ! -L "$current" ]; then
        sed -n '/^# --- 1401 per-system rules ---$/,/^# --- end 1401 per-system rules ---$/p' "$current" > "$WORK/knobs.txt"
        if [ -s "$WORK/knobs.txt" ]; then
            cat "$WORK/knobs.txt" >> "$WORK/stage/Library/GPUBundles/nvmtl/nvrm610.conf" || stop 'Cannot preserve per-system knobs.'
        fi
    fi
    echo 'Package staged; building a test collection before replacing existing files.'
}
