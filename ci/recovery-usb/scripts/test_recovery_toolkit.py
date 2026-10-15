"""Exercise actual shell transaction/KC functions; native plist tests run on macOS."""
import json
import hashlib
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import tempfile
import tarfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / 'scripts/recovery-toolkit/common.sh'
BASH = r'C:\Program Files\Git\bin\bash.exe' if os.name == 'nt' else '/bin/bash'


def posix(p):
    s = str(p).replace('\\', '/')
    return '/' + s[0].lower() + s[2:] if len(s) > 2 and s[1] == ':' else s


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory(prefix='recovery-kit-')
        self.root = Path(self.t.name).resolve()
        self.data = self.root / 'Mac - 数据'
        self.work = self.data / 'Users/Shared/NullMoth-Offline.test'
        (self.work / 'old').mkdir(parents=True)
        self.st = self.data / 'Library/NullMoth'
        self.st.mkdir(parents=True)
        self.cfg = self.root / 'internal EFI/EFI/OC/config.plist'
        self.cfg.parent.mkdir(parents=True)
        self.cfg.write_text('old config')
        self.out = self.root / 'logs'
        self.out.mkdir()
        self.prefix = f'''
set -u
set -o pipefail
. {shlex.quote(posix(LIB))}
DATA_ROOT={shlex.quote(posix(self.data))}
WORK={shlex.quote(posix(self.work))}
ST={shlex.quote(posix(self.st))}
CFG={shlex.quote(posix(self.cfg))}
OUT={shlex.quote(posix(self.out))}
TX_ACTIVE=0
sync() {{ :; }}
shasum() {{ echo 'shasum is absent in Recovery' >&2; return 127; }}
cmp() {{ echo 'cmp is absent in Recovery' >&2; return 127; }}
base64() {{ echo 'base64 is absent in Recovery' >&2; return 127; }}
export -f shasum cmp base64
'''
        if os.name == 'nt':
            # MSYS stat format differs; permissions/real PlistBuddy are checked by the macOS job.
            self.prefix += 'stat() { echo 0; }; ditto() { cp -Rp "$1" "$2"; };\n'
            self.prefix += 'SHA_TOOL=test_sha256; test_sha256() { local value; value=$(sha256sum "$2") || return 1; printf "%s\\n" "${value%% *}"; };\n'

    def tearDown(self):
        self.t.cleanup()

    def run_shell(self, script, rc=0):
        r = subprocess.run([BASH, '-s'], input=self.prefix + script, text=True, encoding='utf-8', capture_output=True)
        self.assertEqual(r.returncode, rc, r.stdout + r.stderr)
        return r

    def original(self):
        p = self.data / 'Library/Extensions/NVRM.kext'
        p.mkdir(parents=True)
        (p / 'driver').write_text('old binary')
        return p

    def test_refuses_traversal_before_move(self):
        self.run_shell('txn_begin; txn_save "Library/../Windows"', 1)

    def test_refuses_unrelated_component(self):
        self.run_shell('txn_begin; txn_save "Library/Extensions/HighPoint.kext"', 1)

    def test_state_paths_round_trip_unicode_and_apostrophe(self):
        value = "/Volumes/Mac - 数据/Owner's backup/line\\path"
        self.run_shell(f'''shell_pairs TEST_VALUE {shlex.quote(value)} > "$WORK/state"
. "$WORK/state"
printf '%s' "$TEST_VALUE" > "$OUT/state-value.txt"
''')
        self.assertEqual((self.out / 'state-value.txt').read_text(encoding='utf-8'), value)

    def test_native_hash_without_desktop_utilities(self):
        digest = hashlib.sha256(b'old config').hexdigest()
        r = self.run_shell('sha "$CFG"')
        self.assertEqual(r.stdout.strip(), digest)

    def test_hash_fails_for_missing_file(self):
        self.run_shell('sha "$OUT/does-not-exist"', 1)

    def test_empty_hash_output_is_not_success(self):
        self.run_shell('broken_hash() { return 0; }; SHA_TOOL=broken_hash; sha "$CFG"', 1)

    def test_nonzero_hash_exit_is_not_hidden_by_valid_output(self):
        self.run_shell('broken_hash() { printf "%064d\\n" 0; return 7; }; SHA_TOOL=broken_hash; sha "$CFG"', 1)

    def test_two_failed_hashes_cannot_compare_equal(self):
        self.run_shell('broken_hash() { return 1; }; SHA_TOOL=broken_hash; files_equal "$CFG" "$CFG"', 1)

    def test_payload_manifest_handles_spaces_without_shasum(self):
        p = self.work / 'payload'; p.mkdir()
        (p / 'file with spaces').write_bytes(b'driver')
        (p / 'SHA256SUMS').write_text(hashlib.sha256(b'driver').hexdigest() + '  file with spaces\n', newline='\n')
        self.run_shell('verify_manifest "$WORK/payload" "$WORK/payload/SHA256SUMS"')
        (p / 'file with spaces').write_bytes(b'broken')
        self.run_shell('verify_manifest "$WORK/payload" "$WORK/payload/SHA256SUMS"', 1)

    def test_manifest_rejects_parent_path_and_empty_manifest(self):
        manifest = self.work / 'SHA256SUMS'
        manifest.write_text(hashlib.sha256(b'old config').hexdigest() + '  ../config.plist\n', newline='\n')
        self.run_shell('verify_manifest "$WORK" "$WORK/SHA256SUMS"', 1)
        manifest.write_bytes(b'')
        self.run_shell('verify_manifest "$WORK" "$WORK/SHA256SUMS"', 1)

    def test_accelerator_manifest_can_be_generated_and_verified(self):
        k = self.work / 'NVAccel.kext/Contents/MacOS'
        k.mkdir(parents=True)
        (k / 'NVAccel').write_bytes(b'accelerator')
        self.run_shell('write_kext_manifest "$WORK"')
        self.assertEqual((self.work / 'SHA256SUMS').read_text().strip(), hashlib.sha256(b'accelerator').hexdigest() + '  NVAccel.kext/Contents/MacOS/NVAccel')

    def test_recovery_image_contains_all_declared_commands(self):
        import re
        fixture = json.loads((ROOT / 'fixtures/recovery-runtime-15.7.8.json').read_text())
        commands = re.search(r"RECOVERY_COMMANDS='([^']+)'", LIB.read_text()).group(1).split()
        for name in commands + ['PlistBuddy', 'sha256']:
            self.assertIn(name, fixture['available_commands'], name)
        self.assertEqual(fixture['volume_uuid'], 'f5c4e732-3432-4813-a990-1ecafc98c6f1')
        for name in ['shasum', 'cmp', 'base64']:
            self.assertIn(name, fixture['missing_commands'])

    @unittest.skipIf(os.name == 'nt', 'Apple native command preflight')
    def test_native_runtime_preflight_with_recovery_missing_tools(self):
        self.run_shell('PB=/usr/libexec/PlistBuddy; runtime_preflight')
        self.assertFalse((self.out / '.sha256-probe').exists())

    def test_preflight_missing_dependency_stops_before_target_mutation(self):
        self.run_shell('RECOVERY_COMMANDS=tool_that_is_missing; runtime_preflight; echo changed > "$CFG"', 1)
        self.assertEqual(self.cfg.read_text(), 'old config')

    def test_commit_publishes_and_verifies_without_cmp(self):
        (self.work / 'next.plist').write_text('new config')
        self.run_shell('CFG_SHA=$(sha "$CFG") || exit 1; CFG_NEW="$WORK/next.plist"; txn_begin; txn_commit')
        self.assertEqual(self.cfg.read_text(), 'new config')
        self.assertTrue((self.work / 'COMMITTED').is_file())
        self.assertFalse((self.st / 'offline-operation').exists())

    def test_rollback_hash_failure_keeps_recovery_marker(self):
        shutil.copyfile(self.cfg, self.work / 'config-before.plist')
        r = self.run_shell('txn_begin; broken_hash() { return 1; }; SHA_TOOL=broken_hash; rollback_transaction', 1)
        self.assertTrue((self.st / 'offline-operation').exists())
        self.assertFalse((self.work / 'ROLLED_BACK').exists())
        self.assertNotIn('Previous driver and OC configuration restored.', r.stdout)

    def test_running_data_mount_is_not_an_offline_target(self):
        r = self.run_shell('get() { printf /System/Volumes/Data; }; physical_mount unused', 1)
        self.assertEqual(r.stdout, '')

    def test_running_root_mount_is_not_an_offline_target(self):
        r = self.run_shell('get() { printf /; }; physical_mount unused', 1)
        self.assertEqual(r.stdout, '')

    def test_mount_resolves_uuid_but_mounts_bsd_device(self):
        self.run_shell('''
get() { case "$2" in DiskUUID) echo AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE;; DeviceIdentifier) echo disk5s4;; MountPoint) [ ! -f "$OUT/mounted" ] || echo '/Volumes/Mac';; esac; }
diskutil() {
    if [ "$1" = info ]; then echo plist; return 0; fi
    [ "$1:$2" = mount:disk5s4 ] || return 77
    echo "$2" > "$OUT/mount-device.txt"; touch "$OUT/mounted"
}
mount_uuid aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee "$OUT/info.plist"
''')
        self.assertEqual((self.out / 'mount-device.txt').read_text().strip(), 'disk5s4')

    def test_already_mounted_volume_needs_no_mount_command(self):
        self.run_shell('''
get() { case "$2" in DiskUUID) echo AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE;; DeviceIdentifier) echo disk5s4;; MountPoint) echo '/Volumes/Mac';; esac; }
diskutil() { [ "$1" = info ] || return 77; echo plist; }
mount_uuid aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee "$OUT/info.plist"
''')

    def test_mount_rechecks_uuid_after_device_mount(self):
        self.run_shell('''
get() { case "$2" in DiskUUID) if [ -f "$OUT/mounted" ]; then echo WRONG; else echo AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE; fi;; DeviceIdentifier) echo disk5s4;; MountPoint) [ ! -f "$OUT/mounted" ] || echo '/Volumes/Mac';; esac; }
diskutil() { if [ "$1" = info ]; then echo plist; else touch "$OUT/mounted"; fi; }
mount_uuid aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee "$OUT/info.plist"
''', 1)

    @unittest.skipIf(os.name == 'nt', 'Apple native plist parser regression')
    def test_native_actual_quiet_config_and_repaired_config(self):
        fixtures = ROOT / 'fixtures'
        results = {}
        for name in ['oc-before-quiet.plist', 'oc-dotnet-quiet.plist', 'oc-repaired-quiet.plist']:
            p = fixtures / name
            self.assertTrue(p.is_file(), name)
            proc = subprocess.run(['/usr/bin/plutil', '-lint', str(p)], capture_output=True, text=True)
            results[name] = {'exit_code': proc.returncode, 'output': proc.stdout + proc.stderr}
        print('APPLE_PLUTIL_REGRESSION', json.dumps(results, ensure_ascii=False))
        self.assertEqual(results['oc-before-quiet.plist']['exit_code'], 0)
        self.assertNotEqual(results['oc-dotnet-quiet.plist']['exit_code'], 0)
        self.assertEqual(results['oc-repaired-quiet.plist']['exit_code'], 0)

    def test_recovery_entry_does_not_guess_from_cdis_directory(self):
        entry = LIB.with_name('recovery.sh').read_text(encoding='utf-8')
        self.assertNotIn('[ -d /System/Installation/CDIS ]', entry)
        self.assertNotIn('[ ! -d /System/Volumes/Data/Users ]', entry)

    def test_failed_copy_restores_old_kext(self):
        p = self.original()
        self.run_shell('''
txn_begin
txn_save Library/Extensions/NVRM.kext
ditto() { mkdir -p "$2"; echo partial > "$2/partial"; return 1; }
trap 'rollback_transaction' EXIT
txn_put Library/Extensions/NVRM.kext "$WORK/nonexistent-source"
''', 1)
        self.assertEqual((p / 'driver').read_text(), 'old binary')
        self.assertFalse((p / 'partial').exists())
        self.assertFalse((self.st / 'offline-operation').exists())

    def test_rollback_removes_new_component_that_had_no_original(self):
        self.run_shell('''
txn_begin
txn_save Library/NullMoth/driver-version
echo new > "$ST/driver-version"
rollback_transaction
''')
        self.assertFalse((self.st / 'driver-version').exists())

    def test_journal_before_rename_is_safe(self):
        p = self.original()
        (self.work / 'journal').write_text('1|Library/Extensions/NVRM.kext\n', newline='\n')
        self.run_shell('rollback_transaction')
        self.assertEqual((p / 'driver').read_text(), 'old binary')

    def test_restart_recovers_interrupted_swap(self):
        p = self.original()
        self.run_shell('txn_begin; txn_save Library/Extensions/NVRM.kext; mkdir -p "$DATA_ROOT/Library/Extensions/NVRM.kext"; echo new > "$DATA_ROOT/Library/Extensions/NVRM.kext/driver"')
        self.run_shell('resume_interrupted')
        self.assertEqual((p / 'driver').read_text(), 'old binary')

    def test_config_publication_failure_restores_both(self):
        p = self.original()
        shutil.copyfile(self.cfg, self.work / 'config-before.plist')
        self.run_shell('''
txn_begin; txn_save Library/Extensions/NVRM.kext
mkdir -p "$DATA_ROOT/Library/Extensions/NVRM.kext"; echo new > "$DATA_ROOT/Library/Extensions/NVRM.kext/driver"
touch "$WORK/config-publish-intent"; echo changed > "$CFG"; echo partial > "$CFG.offline-new"
rollback_transaction
''')
        self.assertEqual(self.cfg.read_text(), 'old config')
        self.assertEqual((p / 'driver').read_text(), 'old binary')
        self.assertFalse(Path(str(self.cfg) + '.offline-new').exists())

    def test_committed_marker_does_not_roll_back_new_install(self):
        self.run_shell('txn_begin; touch "$WORK/COMMITTED"; resume_interrupted')
        self.assertFalse((self.st / 'offline-operation').exists())
        self.assertFalse((self.work / 'ROLLED_BACK').exists())

    def test_failed_restore_keeps_marker_for_next_recovery(self):
        (self.work / 'journal').write_text('1|Library/Extensions/NVRM.kext\n', newline='\n')
        (self.st / 'offline-operation').write_text(posix(self.work))
        self.run_shell('resume_interrupted', 1)
        self.assertTrue((self.st / 'offline-operation').exists())

    def test_kc_absolute_paths_are_not_prefixed_twice(self):
        base = self.root / 'Mac System/System/Library/KernelCollections'
        base.mkdir(parents=True)
        b = base / 'BootKernelExtensions.kc'; b.write_text('base')
        s = base / 'SystemKernelExtensions.kc'; s.write_text('system')
        self.run_shell(f'''
BOOTKC={shlex.quote(posix(b))}; SYSTEMKC={shlex.quote(posix(s))}; KERNEL=/absent/kernel
kmutil() {{ if [ "$1" = create ]; then printf '%s\n' "$@" > "$OUT/actual-args.txt"; while [ "$1" != -A ]; do shift; done; echo candidate > "$2"; else echo com.nullmoth.NVRM; fi; }}
verify_kc() {{ :; }}
build_kc "$WORK/repo" "$WORK/candidate.kc" test
''')
        args = (self.out / 'actual-args.txt').read_text(encoding='utf-8').splitlines()
        self.assertEqual(args[args.index('--volume-root')+1], '/')
        self.assertEqual(args[args.index('-B')+1], posix(b))
        self.assertEqual(args[args.index('--repository')+1], posix(self.work / 'repo'))

    def test_kc_missing_driver_is_rejected(self):
        (self.work / 'other-ids.txt').write_text('')
        (self.out / 'inspect.txt').write_text('com.nullmoth.NVRM\n')
        self.run_shell('ACTION=install; KEXTS="NVRM NVAccel NVRMFB NVRMAGDC"; verify_kc "$OUT/inspect.txt"', 1)

    def test_kc_preserves_unrelated_driver_requirement(self):
        (self.work / 'other-ids.txt').write_text('com.highpoint.driver\n')
        (self.out / 'inspect.txt').write_text('com.nullmoth.NVRM\ncom.nullmoth.NVAccel\ncom.nullmoth.NVRMFB\ncom.nullmoth.NVRMAGDC\n')
        self.run_shell('ACTION=install; KEXTS="NVRM NVAccel NVRMFB NVRMAGDC"; verify_kc "$OUT/inspect.txt"', 1)

    def test_kc_remove_does_not_allow_stale_nullmoth(self):
        (self.work / 'other-ids.txt').write_text('')
        (self.out / 'inspect.txt').write_text('com.nullmoth.NVRMFB\n')
        self.run_shell('ACTION=remove; KEXTS="NVRM NVAccel NVRMFB NVRMAGDC"; verify_kc "$OUT/inspect.txt"', 1)

    @unittest.skipIf(os.name == 'nt', 'Real filesystem symlinks and plist tools exercised on macOS')
    def test_symlink_cannot_redirect_managed_path(self):
        other = self.root / 'outside'
        other.mkdir()
        (self.data / 'Library/Extensions').symlink_to(other)
        self.run_shell('txn_begin; txn_save Library/Extensions/NVRM.kext', 1)

    @unittest.skipIf(os.name == 'nt', 'macOS PlistBuddy/plutil integration')
    def test_native_config_install_remove_preserve_bar_and_unrelated_keys(self):
        fixture = {
            'Booter': {'Quirks': {'ResizeAppleGpuBars': -1}},
            'UEFI': {'Quirks': {'ResizeGpuBars': -1}},
            'PlatformInfo': {'Generic': {'SystemSerialNumber': 'KEEP-TEST-IDENTITY'}},
            'NVRAM': {'Add': {'7C436110-AB2A-4BBB-A880-FE41995C9F82': {'boot-args': '-v nvfb=1 nvaccel=1 -nvoff custom=abc', 'nullmoth-remove': '1'}}, 'Delete': {'7C436110-AB2A-4BBB-A880-FE41995C9F82': []}},
            'Kernel': {'Block': [{'Identifier': 'com.apple.iokit.IONDRVSupport', 'Enabled': False}]},
        }
        self.cfg.write_bytes(plistlib.dumps(fixture))
        for action, enabled in [('install', True), ('remove', False)]:
            self.run_shell(f'PB=/usr/libexec/PlistBuddy; GUID=7C436110-AB2A-4BBB-A880-FE41995C9F82; BAR_APPLE=-1; BAR_UEFI=-1; ACTION={action}; prepare_config')
            d = plistlib.loads((self.work / 'config-next.plist').read_bytes())
            self.assertEqual(d['Booter']['Quirks']['ResizeAppleGpuBars'], -1)
            self.assertEqual(d['UEFI']['Quirks']['ResizeGpuBars'], -1)
            self.assertEqual(d['PlatformInfo'], fixture['PlatformInfo'])
            self.assertEqual(d['Kernel']['Block'][0]['Enabled'], enabled)
            self.assertNotIn('-nvoff', d['NVRAM']['Add']['7C436110-AB2A-4BBB-A880-FE41995C9F82']['boot-args'])
            self.assertIn('custom=abc', d['NVRAM']['Add']['7C436110-AB2A-4BBB-A880-FE41995C9F82']['boot-args'])
            self.assertIn('nullmoth-remove', d['NVRAM']['Delete']['7C436110-AB2A-4BBB-A880-FE41995C9F82'])

    @unittest.skipIf(os.name == 'nt', 'Apple native staged install/commit/remove integration')
    def test_native_staged_install_commit_and_remove_without_desktop_utilities(self):
        guid = '7C436110-AB2A-4BBB-A880-FE41995C9F82'
        kit = self.root / 'kit'; support = kit / 'Support'
        support.mkdir(parents=True); (kit / 'Packages').mkdir()
        payload = self.root / 'pkgroot'; payload.mkdir()
        system = self.root / 'Mac System'
        userplist = system / 'System/Library/DirectoryServices/DefaultLocalDB/Default/users/_windowserver.plist'
        userplist.parent.mkdir(parents=True)
        userplist.write_bytes(plistlib.dumps({'uid': ['88'], 'gid': ['88']}))
        for rel in ['Library/Extensions/' + k + '.kext' for k in ['NVRM', 'NVAccel', 'NVRMFB', 'NVRMAGDC']] + ['Library/NullMoth/kexts/' + v + '/NVAccel.kext' for v in ['15', '26']]:
            d = payload / rel / 'Contents'; (d / 'MacOS').mkdir(parents=True)
            k = Path(rel).stem
            (d / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': 'com.nullmoth.' + k}))
            (d / 'MacOS' / k).write_bytes(('test binary ' + rel).encode())
        for b in ['NVMTLDriver', 'NVIDIAShared']:
            d = payload / f'Library/GPUBundles/{b}.bundle/Contents/MacOS'; d.mkdir(parents=True)
            (d / b).write_bytes(b'test userland')
        for rel, content in {'Library/GPUBundles/nvmtl/nvrm610.conf': '# fixture\n', 'Library/GPUBundles/nvmtl-allow.txt': '*\n', 'Users/Shared/nvfw/firmware.bin': 'firmware', 'VERSION': '2.0.4\n', 'EXPERIMENTAL-BUILD.json': '{}\n'}.items():
            p = payload / rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(content, newline='\n')
        manifest = ''.join(hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.relative_to(payload).as_posix() + '\n' for p in sorted(payload.rglob('*')) if p.is_file())
        (payload / 'SHA256SUMS').write_text(manifest, newline='\n')
        package = kit / 'Packages/test.tar.gz'
        with tarfile.open(package, 'w:gz') as t:
            t.add(payload, arcname='pkgroot')
        (kit / 'kit.plist').write_bytes(plistlib.dumps({'Package': {'File': 'Packages/test.tar.gz', 'SHA256': hashlib.sha256(package.read_bytes()).hexdigest(), 'Version': '2.0.4'}}))
        # GPU payload is synthetic; the real runtime checker belongs to the paired package.
        # This fixture covers all shell staging paths, including SIP and both cache manifests.
        for name in ['nullmoth-runtime-check.sh', 'nullmoth-setup.sh', 'uninstall.sh', 'nullmoth-recover.sh', 'nullmoth-displaytrace.sh', 'nullmoth-wsreset.sh']:
            (support / name).write_text('#!/bin/bash\nexit 0\n', newline='\n')
        for name in ['recover', 'displaytrace', 'wsreset']:
            (support / f'com.nullmoth.{name}.plist').write_bytes(plistlib.dumps({'Label': name}))
        fixture = {'Booter': {'Quirks': {'ResizeAppleGpuBars': -1}}, 'UEFI': {'Quirks': {'ResizeGpuBars': -1}}, 'NVRAM': {'Add': {guid: {'boot-args': '-v nvfb=1 nvaccel=1 -nvoff', 'csr-active-config': bytes.fromhex('430a0000')}}, 'Delete': {guid: []}}, 'Kernel': {'Block': [{'Identifier': 'com.apple.iokit.IONDRVSupport', 'Enabled': False}]}}
        self.cfg.write_bytes(plistlib.dumps(fixture))
        self.original()
        r = self.run_shell(f'''
PB=/usr/libexec/PlistBuddy
GUID={guid}; BAR_APPLE=-1; BAR_UEFI=-1; ESP_UUID=TEST-ESP; KEXTS='NVRM NVAccel NVRMFB NVRMAGDC'
HERE={shlex.quote(posix(kit))}; MANIFEST="$HERE/kit.plist"; SYSTEM_ROOT={shlex.quote(posix(system))}; OS_VERSION=15.7.8
. {shlex.quote(posix(LIB.with_name('install.sh')))}
ACTION=install
prepare_config
prepare_install
verify_manifest "$WORK/stage/Library/NullMoth/kexts/15" "$WORK/stage/Library/NullMoth/kexts/15/SHA256SUMS" || exit 1
verify_manifest "$WORK/stage/Library/NullMoth/kexts/26" "$WORK/stage/Library/NullMoth/kexts/26/SHA256SUMS" || exit 1
CFG_SHA=$(sha "$CFG") || exit 1
txn_begin
while IFS= read -r rel; do txn_save "$rel"; txn_put "$rel" "$WORK/stage/$rel"; done < "$WORK/install-list.txt"
txn_commit
cp "$CFG" "$OUT/config-installed.plist"
WORK="$DATA_ROOT/Users/Shared/NullMoth-Offline.remove"; mkdir -p "$WORK/old"
ACTION=remove
prepare_config
CFG_SHA=$(sha "$CFG") || exit 1
txn_begin
for k in $KEXTS; do txn_save "Library/Extensions/$k.kext"; done
txn_commit
''')
        self.assertIn('Verified', r.stdout)
        installed = plistlib.loads((self.out / 'config-installed.plist').read_bytes())
        removed = plistlib.loads(self.cfg.read_bytes())
        self.assertTrue(installed['Kernel']['Block'][0]['Enabled'])
        self.assertFalse(removed['Kernel']['Block'][0]['Enabled'])
        for config in [installed, removed]:
            self.assertEqual(config['Booter']['Quirks']['ResizeAppleGpuBars'], -1)
            self.assertEqual(config['UEFI']['Quirks']['ResizeGpuBars'], -1)
        for k in ['NVRM', 'NVAccel', 'NVRMFB', 'NVRMAGDC']:
            self.assertFalse((self.data / f'Library/Extensions/{k}.kext').exists())
        state = (self.st / 'state').read_text(encoding='utf-8')
        self.assertIn("CONFIG_SHA_AFTER='", state)
        self.assertNotIn("CONFIG_SHA_AFTER=''", state)

    @unittest.skipIf(os.name == 'nt', 'macOS account database integration')
    def test_native_windowserver_uid_from_offline_plist(self):
        import pwd
        candidates = [Path('/private/var/db/dslocal/nodes/Default/users/_windowserver.plist'), Path('/System/Library/DirectoryServices/DefaultLocalDB/Default/users/_windowserver.plist')]
        file = next((p for p in candidates if p.is_file()), None)
        self.assertIsNotNone(file, 'No offline-readable WindowServer account plist found')
        account = plistlib.loads(file.read_bytes())
        live = pwd.getpwnam('_windowserver')
        self.assertEqual(int(account['uid'][0]), live.pw_uid)
        self.assertEqual(int(account['gid'][0]), live.pw_gid)


if __name__ == '__main__':
    unittest.main(verbosity=2)
