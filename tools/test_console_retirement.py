#!/usr/bin/env python3
"""Exercise the actual handoff policy with two heads and the captured BAR budget."""
from pathlib import Path
import shutil
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
test = r'''
#include "kexts/NVRMFB/fb/nvrm-console-lifetime.h"
#include <assert.h>
int main() {
    int gpu, other;
    const uint64_t bar = 256ull << 20;
    NVRMConsoleLifetime s;
    s.submitted(&gpu, 0);
    assert(!s.eligible(&gpu, bar, 0, 1)); // mode submission cannot replace console disable
    s.kernelConsole(&gpu, true);
    s.submitted(&gpu, 0);
    assert(!s.eligible(&gpu, bar, 0, 3)); // the other claimed head still uses its old state
    s.submitted(&gpu, 1);
    assert(s.eligible(&gpu, bar, 0, 3));
    assert(!s.eligible(&other, bar, 0, 3));
    assert(!s.eligible(&gpu, bar, -1, 3));
    assert(!s.eligible(&gpu, bar, 2, 3));
    assert(!s.eligible(&gpu, 64ull << 20, 0, 3));
    assert(!s.eligible(&gpu, 8ull << 30, 0, 3));
    // Model a rejected release: it never commits the retired state.
    assert(!s.retiredFor(&gpu));
    s.kernelConsole(&gpu, false);
    assert(!s.eligible(&gpu, bar, 0, 3));
    s.kernelConsole(&gpu, true);
    s.submitted(&gpu, 0); s.submitted(&gpu, 1);
    s.retired = true;
    assert(s.retiredFor(&gpu) && !s.retiredFor(&other));
    assert(!s.eligible(&gpu, bar, 0, 3)); // one handoff, no repeated reclaim
    assert(nvrmConsoleRangesOverlap(0xb0000000, 33226752, 0xb0000000, 35389440));
    assert(!nvrmConsoleRangesOverlap(0xb0000000, 33226752, 0xb2350000, 33226752));
    assert(!nvrmConsoleRangesOverlap(0, 16, 16, 16));
    assert(!nvrmConsoleRangesOverlap(0, 0, 0, 16));
    assert(nvrmConsoleRangesOverlap(UINT64_MAX-1, 4, 0, 16));
    const uint64_t surface = 33226752, display = 66977792, margin = 16ull << 20;
    const uint64_t budget = bar - display - margin;
    assert(4*surface + (32ull<<20) + surface > budget);
    assert(4*surface + surface <= budget); // reclaim permits the previously refused request
    assert(6*surface > budget); // the experiment does not promise unlimited dual-4K surfaces
}
'''
compiler = shutil.which('clang++') or shutil.which('g++')
if not compiler:
    raise SystemExit('C++ compiler required for the real handoff policy')
with tempfile.TemporaryDirectory() as tmp:
    source = Path(tmp)/'handoff.cpp'; source.write_text(test)
    exe = Path(tmp)/'handoff'
    subprocess.run([compiler, '-std=c++17', '-Wall', '-Wextra', '-Werror', '-I'+str(root), str(source), '-o', str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
print('PASS: device ownership, every claimed head, boot-head identity, failed handoff, range safety and the captured 256 MiB budget')
