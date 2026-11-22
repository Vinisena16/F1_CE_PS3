// Measurement only: F1CE_SKIP_FN=<hex>,<hex> makes the listed render functions return at once, to price a pass by its
// fps (as F1CE_CUBE_SKIP did for the cube map). The hooks also count calls (log `skipfn:` every 5 s). Hooks in
// tools/recomp.py HOOKS.
#include <cstdlib>
#include <cstring>
#include <string>

#include "kernel.h"

namespace {
bool skipped(u32 fn) {
    static const std::string list = getenv("F1CE_SKIP_FN") ? "," + std::string(getenv("F1CE_SKIP_FN")) + "," : "";
    char k[16];
    snprintf(k, sizeof(k), ",%x,", fn);
    return list.find(k) != std::string::npos;
}

struct Counter {
    u32 fn;
    bool skip;
    u32 n = 0;
    u64 window = 0;
    explicit Counter(u32 f) : fn(f), skip(skipped(f)) {}
    bool hit() {
        const u64 now = time_usec();
        if (!window) window = now;
        n++;
        if (now - window >= 5000000) {
            LOG_I("skipfn: %06x %.1f calls/s%s", fn, n * 1e6 / (now - window), skip ? " (skipped)" : "");
            window = now;
            n = 0;
        }
        return skip;
    }
};
}  // namespace

// foaLightingManager::RenderStaticShadows
bool skip_2f141c_hook(PPUContext&) { static Counter c(0x2f141c); return c.hit(); }
// foaLightingManager::RenderBlurredShadows
bool skip_2f1bec_hook(PPUContext&) { static Counter c(0x2f1bec); return c.hit(); }
// RenderShadowMap. The pass never sets a depth function: it runs with the one the frame left, LEQUAL after the reflection
// cube map's faces, but ALWAYS (the depth reductions of the post effects) when no faces were drawn before it, as in the
// driver garage (the last caster drawn won: the rear wing, a rear tyre and the wall pillars were lit through the roof)
// and on 60 fps frames that skip the faces (flickering shadows, hle_cubemap.cpp). LEQUAL is written to the game's command
// list (0x10C3E1AC: begin, end, current) before the pass.
bool skip_43f620_hook(PPUContext&) {
    static Counter c(0x43f620);
    if (c.hit()) return true;
    const u32 ctx = 0x10C3E1AC, cur = ld32(ctx + 8);
    if (cur && cur + 8 <= ld32(ctx + 4)) {
        st32(cur, 0x00040A6C);       // NV4097_SET_DEPTH_FUNC
        st32(cur + 4, 0x00000203);   // LEQUAL
        st32(ctx + 8, cur + 8);
    }
    return false;
}
// RenderShadowToAlpha
bool skip_43ffa0_hook(PPUContext&) { static Counter c(0x43ffa0); return c.hit(); }
// foaCarModelManagerPS3::Render
bool skip_30388c_hook(PPUContext&) { static Counter c(0x30388c); return c.hit(); }
