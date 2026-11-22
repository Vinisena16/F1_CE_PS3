// Reflection cube map (foaDynamicCubeMap). The game renders all six faces every frame: at 30 fps that is 30 updates per
// second. With F1CE_60FPS the pass would run 60 times per second and costs ~20% of a grid frame (its culling and
// transform jobs on the SPUs, ~500 draws). So a frame starting less than 3/4 of the update interval after the last
// update keeps the previous faces (the cube's render target is not reused in between). F1CE_CUBE_HZ=<n> sets the
// updates per second (default 30 with F1CE_60FPS, as the original game at 30 fps, and no limit without it; 0: every
// frame); at 30 per second reflections refresh every other frame at 60 fps and every frame below ~40 fps. The main
// view's wait for the faces' job fences (0x2F0390) is skipped with the pass, since the fences would then be the
// previous frame's. F1CE_CUBE_SKIP=1 (experiment) never renders it. Hooks in tools/recomp.py HOOKS.
// F1CE_REFLECTIONS=static or F1CE_CUBE_STATIC=1 (static reflections): the faces are rendered during the first 2 s of a
// scene and then kept until the next one (a scene starts when the pass was not called for over a second: loading,
// menus). 2 s and not a single frame, since programs compiled in the background and streamed textures are missing from
// a scene's first frames.
// F1CE_REFLECTIONS=0 (reflections off): as static, but the faces' camera far plane (f2 of the scene render 0x43B528,
// the main camera's 1000 m) becomes 0.2 m, just past its 0.1 m near plane, so the faces hold only the clear colour
// and the sky: the cars get a plain sheen and the pass costs nothing after a scene's first 2 s. (Never rendering the
// faces, F1CE_CUBE_SKIP, leaves old memory in them: noise on the car bodies.)
// F1CE_CUBE_DIST=<m> (reflection distance): the faces' far plane at most m metres. Silverstone race, 30 m: ~115 of the
// pass's ~550 draws per frame and ~0.2-0.3 cores less (the 1000.0 stored at r25+0x110 +36 before the call changed
// nothing); most of the pass is what is near the car.
#include <atomic>
#include <cstdlib>
#include <cstring>

#include "kernel.h"

namespace {
const bool g_never = getenv("F1CE_CUBE_SKIP") != nullptr;
const u64 g_interval = [] {
    const char* e = getenv("F1CE_CUBE_HZ");
    const int hz = e ? atoi(e) : getenv("F1CE_60FPS") ? 30 : 0;
    return hz > 0 ? (u64)(750000 / hz) : 0;
}();
const char* const g_mode = getenv("F1CE_REFLECTIONS");   // unset: live, "static", "0": off
const bool g_off = g_mode && !strcmp(g_mode, "0");
const double g_dist = getenv("F1CE_CUBE_DIST") ? atof(getenv("F1CE_CUBE_DIST")) : 0;
const bool g_static = g_off || (g_mode && !strcmp(g_mode, "static")) ||
                      (getenv("F1CE_CUBE_STATIC") && atoi(getenv("F1CE_CUBE_STATIC")) != 0);
u64 g_last_render = 0;
u64 g_last_call = 0, g_scene_start = 0;
bool g_skipped = false;   // the last pass was skipped: its fence wait goes too
u32 g_face_mask = 0x3F;   // faces the last pass rendered (F1CE_CUBE_SPLIT)
u32 g_renders = 0, g_skips = 0;
u64 g_window = 0;

void report(u64 now) {
    if (!g_window) g_window = now;
    if (now - g_window < 5000000) return;
    LOG_I("cube map: %.1f updates/s, %.1f skipped/s", g_renders * 1e6 / (now - g_window), g_skips * 1e6 / (now - g_window));
    g_window = now;
    g_renders = g_skips = 0;
}
}  // namespace

// 0x2F0D14 foaDynamicCubeMap::Render (main thread, once per frame)
bool cube_render_hook(PPUContext&) {
    const u64 now = time_usec();
    report(now);
    if (now - g_last_call > 1000000) g_scene_start = now;
    g_last_call = now;
    g_skipped = g_never || (g_static ? now - g_scene_start > 2000000 : g_interval && now - g_last_render < g_interval);
    // The shadow map passes start from the state the faces leave behind: without them the depth function was ALWAYS and
    // shadows flickered on alternate frames at a steady 60 fps (thin ones under a low sun most of all: guardrail at
    // Bahrain turn 1 with Golden Hour). RenderShadowMap now gets LEQUAL whatever ran before (hle_skipfn.cpp, 2026-11-22).
    // Debug: F1CE_CUBE_ALT=1 skips every other pass whatever the timing (with F1CE_TICKS_FIXED=1 and frame dumps).
    static const bool alt = getenv("F1CE_CUBE_ALT") != nullptr;
    static u32 calls = 0;
    if (alt) g_skipped = calls++ & 1;
    if (g_skipped) { g_skips++; return true; }
    g_last_render = now;
    g_renders++;
    return false;
}
// 0x2F3B34: the other Render (seen once, at load)
bool cube_render2_hook(PPUContext&) { return false; }
// 0x2F0390: waits for the six faces' job fences
void sub_00551228(PPUContext& __restrict c);   // waits for a job fence (r3 = list, r4 = index)
bool cube_wait_hook(PPUContext& c) {
    if (g_skipped) {
        g_skipped = false;
        return true;
    }
    if (g_face_mask == 0x3F) return false;
    // as the original (r3 = the cube; six fences of halfword index + word list at +16, 8 bytes each), for the faces
    // the pass rendered only: the others' fences are a frame older and never come
    const u32 cube = (u32)c.r[3], mask = g_face_mask;
    const u64 lr = c.lr, sp = c.r[1];
    const u32 fn = c.cur_fn;
    g_face_mask = 0x3F;
    if (!ld32(ld32((u32)c.r[2] - 4464))) return true;   // (the original waits only while this is set)
    c.r[1] = sp - 128;   // (a frame of our own: the callee saves its link register in its caller's)
    for (u32 i = 0; i < 6; i++) {
        if (!(mask >> i & 1)) continue;
        c.r[4] = ld16(cube + 16 + i * 8);
        c.r[3] = ld32(cube + 20 + i * 8);
        sub_00551228(c);
    }
    c.r[1] = sp;
    c.lr = lr;
    c.cur_fn = fn;
    return true;
}
// 0x43B528 (scene render), called by the cube pass at 0x2F0FB0 for each face with the main camera's near and far planes
// in f1/f2 (0.1 and 1000 m)
// The cube pass also passes a mask of the faces to render on its stack (r1 + 112 at the call, 0x3F = all six).
// F1CE_CUBE_HZ=half (launcher "Every frame, 3 faces at a time"): a pass every frame with three faces, the other three
// on the next (masks 0x07 / 0x38; F1CE_CUBE_SPLIT=1 does the same with any rate). Every face is renewed at half the pass
// rate and the cube still changes every frame. Silverstone race start, 60 fps: draws 3161 -> 2943 per frame, SPU
// instructions 60 -> 57 M per frame (~30% of the pass's; the rest does not depend on the faces), worst 5 s 42 -> 46 fps.
// The main view's fence wait (cube_wait_hook) then waits for the rendered faces only (the others' never come: hang).
bool cube_scene_hook(PPUContext& c) {
    if ((u32)c.lr != 0x2F0FB4) return false;
    if (g_off) c.f[2].d = 0.2;
    else if (g_dist > 0 && c.f[2].d > g_dist) c.f[2].d = g_dist;
    static const bool split = (getenv("F1CE_CUBE_SPLIT") && atoi(getenv("F1CE_CUBE_SPLIT")) != 0) ||
                              (getenv("F1CE_CUBE_HZ") && !strcmp(getenv("F1CE_CUBE_HZ"), "half"));
    const u32 mask_ea = (u32)c.r[1] + 112;
    if (split && !g_static && ld64(mask_ea) == 0x3F) {
        static u32 half = 0;
        // a scene's first pass renders all six (no old faces yet)
        if (time_usec() - g_scene_start > 500000) {
            g_face_mask = (half ^= 1) ? 0x07 : 0x38;
            st64(mask_ea, g_face_mask);
        }
    }
    return false;
}
