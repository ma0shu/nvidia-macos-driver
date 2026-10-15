"""Exercise actual shell transaction/KC functions; native plist tests run on macOS."""
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import tempfile
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
'''
        if os.name == 'nt':
            # MSYS stat format differs; permissions/real PlistBuddy are checked by the macOS job.
            self.prefix += 'stat() { echo 0; }; ditto() { cp -Rp "$1" "$2"; };\n'

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

    def test_running_data_mount_is_not_an_offline_target(self):
        r = self.run_shell('get() { printf /System/Volumes/Data; }; physical_mount unused', 1)
        self.assertEqual(r.stdout, '')

    def test_running_root_mount_is_not_an_offline_target(self):
        r = self.run_shell('get() { printf /; }; physical_mount unused', 1)
        self.assertEqual(r.stdout, '')

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
        args = (self.out / 'actual-args.txt').read_text().splitlines()
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
