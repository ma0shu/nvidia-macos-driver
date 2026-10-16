#pragma once
#include <stdint.h>

// The framebuffer currently shares one NVKMS device across its heads. Keep the
// handoff tied to that device, not a particular head or a BAR1 address.
struct NVRMConsoleLifetime {
    const void *device = nullptr;
    bool kernelDisabled = false;
    bool retired = false;
    uint32_t submittedHeads = 0;

    void kernelConsole(const void *d, bool disabled) {
        if (!d || (device && device != d)) return;
        device = d;
        kernelDisabled = disabled;
        if (!disabled) { retired = false; submittedHeads = 0; }
    }
    void submitted(const void *d, unsigned head) {
        if (device == d && head < 32) submittedHeads |= uint32_t(1) << head;
    }
    bool eligible(const void *d, uint64_t barBytes, int bootHead, uint32_t claimed) const {
        // Limit this experiment to the measured 256 MiB case. An unknown boot
        // head, an uncommitted head or a different device keeps the old guard.
        return d && device == d && kernelDisabled && !retired &&
            barBytes == (256ull << 20) && bootHead >= 0 && bootHead < 32 &&
            (claimed & (uint32_t(1) << bootHead)) &&
            claimed && (submittedHeads & claimed) == claimed;
    }
    bool retiredFor(const void *d) const { return d && device == d && retired; }
};

static inline bool nvrmConsoleRangesOverlap(uint64_t a, uint64_t n, uint64_t b, uint64_t m)
{
    if (!n || !m) return false;
    if (a > UINT64_MAX - n || b > UINT64_MAX - m) return true;
    return a < b + m && b < a + n;
}
