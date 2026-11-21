// Car handling: steering response and kerbs (README "Handling").
// Steering (F1CE_STEER_RESPONSE, launcher "Pad steering response"): 0x112818 slews the steering angle (+0x4b0)
// towards input x lock at the rate of the car's steering set (+0x5d0, 8 floats copied from dynamics.bin section 6:
// ?, min lock, max lock, lock loss per m/s, min rate, rate at rest, rate loss per m/s, centring). A wheel loads set
// 0x580da796 (20 lock units a second at any speed), a pad 0xe9b1cb9d: max(0.6, 2.4 - 0.025 v), so at 180 km/h the
// angle needs about 0.9 s to reach full lock. Unset = responsive: the pad's rate x ~3 (max(2, 6 - 0.05 v), centring
// 20); 2 = direct (the wheel's rates); 0 = original. Lock (and its loss with speed) stays the game's. The set's words
// are swapped in only for the call and put back after it (unless the call loaded another set), so the car object,
// the replays and the Time Trial ghost snapshots never hold the changed values; the replays re-run 0x112818 with the
// same setting (a replay or ghost made with another setting drifts off its line).
// Debug: F1CE_HANDLING_LOG=1 logs the player's car once per tick (steering, speed, wheel surfaces, loads, grip, tyre
// forces, compression, heading).
#include <algorithm>
#include <bit>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <string>

#include "kernel.h"

namespace {
float ldf(u32 ea) { return std::bit_cast<float>(ld32(ea)); }

bool log_on() {
    static const bool on = getenv("F1CE_HANDLING_LOG") && atoi(getenv("F1CE_HANDLING_LOG"));
    return on;
}

// the physics parameter block (TOC r2 - 0x5e8c)
u32 g_toc = 0;
u32 g_player = 0;   // the first human car seen by 0x112818 (kept when F1CE_AI_DRIVE gives it to the AI)
u32 params() { return ld32(ld32(g_toc - 0x5e8c)); }

// 0x112818's test: the car's driver is driven by its controller (0 in the race's table at +0x1a5c), not by the AI
bool human(u32 car) {
    const u32 entity = ld32(car + 0x115c);
    if (!entity) return false;
    const u32 race = ld32(ld32(0x7092c8));
    return race && ld32(race + 0x1a5c + 4 * ld32(entity + 0x6c)) == 0;
}

void log_car(u32 car, u32 sample) {
    static u32 n = 0;
    static bool dumped = false;
    bool kerb = false;
    for (int w = 0; w < 4; w++) kerb |= (s32)ld32(car + 0x920 + 4 * w) >= 9 && (s32)ld32(car + 0x920 + 4 * w) <= 16;
    if (n == 0 || (kerb && !dumped)) {
        if (n) dumped = true;
        const u32 p = params();
        std::string s;
        char b[24];
        for (u32 o = 0x3300; o < 0x33a0; o += 4) { snprintf(b, sizeof(b), " %x:%g", o, ldf(p + o)); s += b; }
        LOG_I("handling: toc %08x params %08x%s", g_toc, p, s.c_str());
        // 0x119c78's constants: floats at r2 - 24096 / 24136 / 24092 / 24260 and behind the pointers at r2 - 24100 /
        // 23540 / 23544 (speed clamp of the bump amplitude)
        LOG_I("handling: k %g %g %g %g *%g *%g *%g", ldf(g_toc - 24096), ldf(g_toc - 24136), ldf(g_toc - 24092),
              ldf(g_toc - 24260), ldf(ld32(g_toc - 24100)), ldf(ld32(g_toc - 23540)), ldf(ld32(g_toc - 23544)));
        s.clear();
        for (u32 o = 0x5d0; o < 0x5f0; o += 4) { snprintf(b, sizeof(b), " %g", ldf(car + o)); s += b; }
        LOG_I("handling: car %08x steering set%s", car, s.c_str());
    }
    n++;
    auto q = [&](u32 o) {
        static char b[16][64];
        static int k = 0;
        char* s = b[k++ & 15];
        snprintf(s, 64, "%.3g/%.3g/%.3g/%.3g", ldf(car + o), ldf(car + o + 4), ldf(car + o + 8), ldf(car + o + 12));
        return s;
    };
    LOG_I("handling: %u in %d steer %.3f lock %.3f v %.1f surf %d %d %d %d (%d %d %d %d) contact %x slip %x load %s "
          "grip %.3f l890 %s l8a0 %s la30 %s la50 %s la60 %s f4c0 %s f4a0 %s f510 %s c3b0 %s v3a0 %s m1c0 %s m1d0 %s m1e0 %s",
          n, (s16)ld16(sample + 2), ldf(car + 0x4b0), ldf(car + 0x9a0), ldf(car + 0x3c0), (s32)ld32(car + 0x920),
          (s32)ld32(car + 0x924), (s32)ld32(car + 0x928), (s32)ld32(car + 0x92c), (s32)ld32(car + 0x930),
          (s32)ld32(car + 0x934), (s32)ld32(car + 0x938), (s32)ld32(car + 0x93c), ld32(car + 0x980) & 0xf,
          (ld32(car + 0x97c) >> 8) & 0xf, q(0x530), ldf(car + 0x554), q(0x890), q(0x8a0), q(0xa30), q(0xa50), q(0xa60),
          q(0x4c0), q(0x4a0), q(0x510), q(0x3b0), q(0x3a0), q(0x1c0), q(0x1d0), q(0x1e0));
}
}  // namespace

void sub_00112818(PPUContext& __restrict c);

// 0x112818(car, control sample): throttle, brake and steering of a tick into the car (steering angle +0x4b0 slewed
// towards input x lock at the rate of the car's steering set +0x5d0, dynamics.bin section 6)
int steer_response() {   // 0 original, 1 responsive, 2 direct
    static const int r = [] {
        const char* e = getenv("F1CE_STEER_RESPONSE");
        const int v = e ? atoi(e) : 1;
        LOG_I("handling: pad steering response %s", v == 0 ? "original" : v == 2 ? "direct" : "responsive");
        return v == 0 || v == 2 ? v : 1;
    }();
    return r;
}

bool steer_input_hook(PPUContext& c) {
    static thread_local bool inside = false;
    if (inside) return false;
    const u32 car = (u32)c.r[3], sample = (u32)c.r[4];
    g_toc = (u32)c.r[2];
    // min rate, rate at rest, rate loss per m/s, centring (+0x5e0..+0x5ec) of a pad's set (a wheel's rate is 20)
    u32 orig[4], mine[4];
    const bool swap = steer_response() != 0 && ldf(car + 0x5e4) < 10.0f;
    if (swap) {
        static const float responsive[4] = {2.0f, 6.0f, 0.05f, 20.0f}, direct[4] = {20.0f, 20.0f, 0.0f, 20.0f};
        const float* v = steer_response() == 2 ? direct : responsive;
        for (int i = 0; i < 4; i++) {
            orig[i] = ld32(car + 0x5e0 + 4 * i);
            mine[i] = std::bit_cast<u32>(v[i]);
            st32(car + 0x5e0 + 4 * i, mine[i]);
        }
    }
    inside = true;
    sub_00112818(c);
    inside = false;
    if (swap)
        for (int i = 0; i < 4; i++)
            if (ld32(car + 0x5e0 + 4 * i) == mine[i]) st32(car + 0x5e0 + 4 * i, orig[i]);
    if (!g_player && human(car)) g_player = car;
    if (log_on() && car == g_player) log_car(car, sample);
    return true;
}

int kerb_mode() {   // 0 original, 1 improved low, 2 improved high
    static const int m = [] {
        const char* e = getenv("F1CE_KERBS");
        const int v = !e ? 1 : atoi(e) == 0 ? 0 : atoi(e) == 2 ? 2 : 1;
        LOG_I("handling: kerbs %s", v == 2 ? "improved high" : v ? "improved low" : "original");
        return v;
    }();
    return m;
}

void sub_001177F4(PPUContext& __restrict c);

// 0x1177f4(car): the suspension of a substep (3 a tick): per wheel spring +0x320 x compression +0x3b0 plus damper
// +0x330 x compression speed, the damper clamped to +-(-0x340) (dynamics.bin section 9: -24000 N), the sum (>= 0) is
// the load +0x530 (only an output: rumble, sound) and pushes the body; +0x4c0 = the spring part low-passed (0.99 a
// substep for the player's car), the load the tyre forces (0x11b0dc) scale with.
// Kerbs (F1CE_KERBS, launcher "Kerb handling"): a kerb's edge moves the wheel so fast that the damper alone makes 10-17
// kN at one corner (2.5 kN at rest); on the rebound it pulls the load to 0 and the car leaves the ground for ~0.15 s
// with a yaw kick of 100+ deg/s (the force along the kerb's tilted face), then lands sliding. Normal tarmac stays
// under 2.6 kN of damper 99% of the time (a Bahrain log, 2026-11-21); the spikes were on the kerbs (ids 9-16) and on
// the other surfaces next to them (ids 3, 4, 5, 8, 19), never much on plain tarmac (id 1).
// Unset / 1 = improved low: the damper limit is 4500 N. 2 = improved high: also no tyre grip in the air. The tyres
// take their load from +0x4c0, which follows the spring over ~0.5 s, so a wheel that has left the ground keeps 2-3 kN
// of side force for half a second: on a 2026-11-21 Imola log the rear hung (compression 0) for 12 ticks after the
// Variante Alta's outer kerb and the phantom forces of the locked fronts turned the car from 39 to 228 deg/s in the air.
// High: a wheel hanging for two substeps in a row (+0x690, the compression before this call, and +0x3b0 both 0; a
// one-substep rattle is left alone) loses 20% of +0x4c0 a substep; once it touches again, a +0x4c0 under half its
// spring load catches up 20% a substep instead of 1%. (An earlier high, a 40% softer spring and 2500 N on kerbs, made
// the kerb throw the wheel back harder and was dropped.)
// 0 = original. The damper limits are swapped in only while the call runs and put back after it; high's +0x4c0 change
// is a function of the car's own state. So the car object keeps the game's values otherwise and replays / ghosts
// re-run the same physics with the same setting.
bool suspension_hook(PPUContext& c) {
    static thread_local bool inside = false;
    if (inside || !kerb_mode()) return false;
    const u32 car = (u32)c.r[3];
    u32 orig[4], mine[4];
    for (int w = 0; w < 4; w++) {
        orig[w] = ld32(car + 0x340 + 4 * w);
        mine[w] = std::bit_cast<u32>(std::max(std::bit_cast<float>(orig[w]), -4500.0f));
        st32(car + 0x340 + 4 * w, mine[w]);
    }
    inside = true;
    sub_001177F4(c);
    inside = false;
    for (int w = 0; w < 4; w++)
        if (ld32(car + 0x340 + 4 * w) == mine[w]) st32(car + 0x340 + 4 * w, orig[w]);
    if (kerb_mode() == 2)
        for (int w = 0; w < 4; w++) {
            const float before = ldf(car + 0x690 + 4 * w), now = ldf(car + 0x3b0 + 4 * w);
            const float spring = ldf(car + 0x320 + 4 * w) * now, load = ldf(car + 0x4c0 + 4 * w);
            float out = load;
            if (before <= 0.0f && now <= 0.0f) out = load * 0.8f;
            else if (load < 0.5f * spring) out = load + (spring - load) * 0.2f;
            if (out != load) st32(car + 0x4c0 + 4 * w, std::bit_cast<u32>(out));
        }
    return true;
}
