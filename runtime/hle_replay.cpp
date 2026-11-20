// Advanced Replay Mode (F1CE_REPLAY_DIRECTOR=1, launcher "Advanced replay mode"; plan in MODDING_IDEAS.md "Advanced
// Replay Mode"). Unset, nothing here acts and the replay is the original.
//
// How the game's replay works (recomp/PROGRESS.md 2026-11-15): every tick each car's control sample goes into a ring
// and in replay the physics re-runs those inputs from a saved world state. The session (*(*0x70ab54)) holds the replay
// object at +0x10 and its "replaying" word at +0x28 (Lua game.isReplaying). Replay object: +0x2c state (1 recording,
// 2 playing), +0x18 play length and +0x1c play position (ticks, 60 a second in both frame rate modes); the ring keeps the
// last 7200 ticks (2 minutes) and playback loops back to its oldest keyframe at the end (0x14c8fc). World = session
// +0x18fc: +0x21c the car entity being watched (its index, entity +0x6c, at +0x208; 0x1539a0), +0x220 the player's.
//
// Speed: the main loop (0x2494a4) runs the number of ticks in r31 each frame (2 in 30 fps mode, 1-2 with F1CE_60FPS),
// then the per-frame code (cameras, drawing). A frame with 0 ticks still draws, so the probes before its two tests of
// r31 (0x249504, 0x249654) give the frame speed * r31 ticks through an accumulator: 0 = pause, 1/4 and 1/2 = slow
// motion (a tick every few frames), 2-8 = fast forward. Ticks are whole: nothing the tick logic sees is changed, so the
// input re-simulation stays exact.
//
// The original replay's own pad controls (seen 2026-11-20): triangle changes the camera angle (inside the replay
// camera, id 0 on the world's camera stack) and select toggles "TV MODE" manual / auto; start opens the replay menu.
// They stay the game's. Added, while a replay plays: square play / pause, R2 faster, L2 slower, R1 / L1 next /
// previous car. These four are hidden from the game then (cellPadGetData). When start is pressed the speed control
// steps aside until the replay moves again (the game's own pause menu may need ticks).
// The bar (overlay.cpp) shows the time in the replay window, its length, the speed and the car watched; it hides
// itself 4 s after the last director button.
// F1CE_REPLAY_LOG=1 logs the world's cameras and cars when a replay starts and what changes during it, with or without
// the director (reverse-engineering aid).
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "host.h"
#include "kernel.h"
#include "overlay.h"

bool replay_director_on() {
    static const bool on = getenv("F1CE_REPLAY_DIRECTOR") && atoi(getenv("F1CE_REPLAY_DIRECTOR")) != 0;
    return on;
}

namespace {
constexpr u32 kToc = 0x70F138;

u32 session() {
    const u32 p = ld32(kToc - 17892);
    return p ? ld32(p) : 0;
}

struct Replay {
    u32 session = 0, obj = 0, world = 0;
    bool playing = false;
};
Replay find_replay() {
    Replay r;
    r.session = session();
    if (!r.session || !ld32(r.session + 0x28)) return r;
    r.obj = ld32(r.session + 0x10);
    r.world = ld32(r.session + 0x18FC);
    r.playing = r.obj && r.world && ld32(r.obj + 0x2C) == 2;
    return r;
}

bool log_on() {
    static const bool on = getenv("F1CE_REPLAY_LOG") != nullptr;
    return on;
}

// The cars of the race, by car index (entity +0x6c). The race manager's driver list (*0x100de2a0 +0x44) holds only the
// human driver objects (the "world" above), so the entities are found once per replay by their shape: objects in game
// memory with an index 0..31 at +0x6c whose physics car (+0x1c) has the class (vtable) of the player's physics car.
std::vector<u32> g_cars;
u32 g_cars_world = 0;
const std::vector<u32>& race_cars(u32 world) {
    if (world == g_cars_world && !g_cars.empty()) return g_cars;
    g_cars_world = world;
    g_cars.clear();
    const u32 player = ld32(world + 0x220);
    const u32 pphys = player ? ld32(player + 0x1C) : 0;
    if (!pphys || !mem::readable(pphys, 4)) return g_cars;
    const u32 pvt = ld32(pphys), evt = ld32(player);
    const u64 t0 = time_usec();
    u32 by_index[32] = {}, n = 0, others = 0;
    for (u32 a = mem::USER_BASE; a < mem::USER_BASE + mem::USER_SIZE - 0x80; a += 16) {
        const u32 idx = ld32(a + 0x6C);
        if (idx >= 32) continue;
        const u32 phys = ld32(a + 0x1C);
        if (phys < mem::USER_BASE || phys >= mem::USER_BASE + mem::USER_SIZE || (phys & 15) || ld32(phys) != pvt) continue;
        if (by_index[idx]) { others++; continue; }
        by_index[idx] = a;
        n++;
        if (ld32(a) != evt && log_on()) LOG_I("replay:   car %u entity %08x class %08x (player's %08x)", idx, a, ld32(a), evt);
    }
    for (u32 i = 0; i < 32; i++)
        if (by_index[i]) g_cars.push_back(by_index[i]);
    LOG_I("replay: %u cars (%u other matches), %.1f ms", n, others, (time_usec() - t0) / 1e3);
    return g_cars;
}

bool printable(u32 ea, std::string* out) {   // a NUL-terminated ASCII string of 3..40 characters at ea
    if (!mem::readable(ea, 41)) return false;
    std::string s;
    for (u32 i = 0; i < 41; i++) {
        const u8 ch = ld8(ea + i);
        if (!ch) break;
        if (ch < 0x20 || ch > 0x7E || i == 40) return false;
        s += (char)ch;
    }
    if (s.size() < 3) return false;
    *out = s;
    return true;
}

// "Car <n>  <team>": the team object is entity +4, its name at +0xc (the game's own caption shows the driver)
std::string car_name(u32 entity) {
    char s[24];
    snprintf(s, sizeof s, "Car %d", (s32)ld32(entity + 0x6C) + 1);
    std::string team;
    if (const u32 t = ld32(entity + 4); mem::readable(t, 0x10) && printable(ld32(t + 0xC), &team)) return s + ("  " + team);
    return s;
}

// ---- diagnostics (F1CE_REPLAY_LOG) ----------------------------------------------------------------------------------
void hex_line(const char* tag, u32 ea, u32 n) {
    char b[200];
    int k = snprintf(b, sizeof b, "replay:   %s %08x:", tag, ea);
    for (u32 i = 0; i < n && k < (int)sizeof b - 4; i += 4) k += snprintf(b + k, sizeof b - k, " %08x", ld32(ea + i));
    LOG_I("%s", b);
}
void log_tree(u32 node, u32 header, int depth) {   // the camera stack's id -> camera map (std::map, in order)
    if (!node || node == header || depth > 24 || ld8(node + 0x15)) return;
    log_tree(ld32(node), header, depth + 1);
    const u32 cam = ld32(node + 0x10);
    LOG_I("replay:   camera id %d -> %08x vtable %08x", (s32)ld32(node + 0xC), cam, cam ? ld32(cam) : 0);
    log_tree(ld32(node + 8), header, depth + 1);
}
// the camera on top of the world's camera stack (a deque at stack +0xd0: +4 block map, +8 map size, +0xc first,
// +0x10 count; 4 entries a block), as 0x1571b0 reads it
u32 top_camera(u32 world) {
    const u32 dq = world + 0x18 + 0xD0;
    const u32 count = ld32(dq + 0x10), map = ld32(dq + 4), msize = ld32(dq + 8);
    if (!count || !map || !msize) return 0;
    const u32 i = ld32(dq + 0xC) + count - 1;
    u32 block = i >> 2;
    if (block >= msize) block -= msize;
    const u32 b = ld32(map + block * 4);
    return b ? ld32(b + (i & 3) * 4) : 0;
}
s32 camera_id(u32 world, u32 cam) {   // its id in the stack's map, -1 if absent
    const u32 header = ld32(world + 0x18 + 0xE4 + 4);
    s32 id = -1;
    std::vector<u32> todo{header ? ld32(header + 4) : 0};
    for (int k = 0; !todo.empty() && k < 256; k++) {
        const u32 n = todo.back();
        todo.pop_back();
        if (!n || n == header || ld8(n + 0x15)) continue;
        if (ld32(n + 0x10) == cam) id = (s32)ld32(n + 0xC);
        todo.push_back(ld32(n));
        todo.push_back(ld32(n + 8));
    }
    return id;
}
// F1CE_REPLAY_LOG: what changes in a replay (watched car, camera on top, its id, pad), with or without the director
void log_changes(const Replay& r) {
    static u32 watched = 0, cam = 0;
    static u16 d1 = 0, d2 = 0;
    const host::PadState p = host::pad_state();
    if (p.digital1 != d1 || p.digital2 != d2) {
        d1 = p.digital1;
        d2 = p.digital2;
        LOG_I("replay: pad %04x %04x at %.2f s (position %u)", d1, d2, time_usec() / 1e6, ld32(r.obj + 0x1C));
    }
    const u32 w = ld32(r.world + 0x21C), c = top_camera(r.world);
    if (w == watched && c == cam) return;
    watched = w;
    cam = c;
    LOG_I("replay: watching %08x (index %d), camera %08x id %d vtable %08x, position %u", w, w ? (s32)ld32(w + 0x6C) : -1,
          c, camera_id(r.world, c), c ? ld32(c) : 0, ld32(r.obj + 0x1C));
}

void log_world(const Replay& r) {
    if (!log_on()) return;
    LOG_I("replay: session %08x object %08x world %08x watched %08x (index %d) player %08x", r.session, r.obj, r.world,
          ld32(r.world + 0x21C), (s32)ld32(r.world + 0x208), ld32(r.world + 0x220));
    hex_line("object", r.obj, 0x40);
    hex_line("world+200", r.world + 0x200, 0x30);
    const u32 stack = r.world + 0x18;
    hex_line("stack", stack, 0x10);
    hex_line("stack+d0", stack + 0xD0, 0x20);
    const u32 header = ld32(stack + 0xE4 + 4);
    if (header) log_tree(ld32(header + 4), header, 0);
    for (u32 e : race_cars(r.world)) {
        LOG_I("replay:   car %d entity %08x class %08x phys %08x", (s32)ld32(e + 0x6C), e, ld32(e), ld32(e + 0x1C));
        if (e != ld32(r.world + 0x220) && e != race_cars(r.world)[0]) continue;
        // strings reachable from the entity (directly, or through one more object), to find the driver's name
        std::string str;
        for (u32 o = 0; o < 0x400; o += 4) {
            const u32 p = ld32(e + o);
            if (printable(p, &str)) LOG_I("replay:     +%03x -> '%s'", o, str.c_str());
            else if (mem::readable(p, 0x100) && p >= 0x10000)
                for (u32 o2 = 0; o2 < 0x100; o2 += 4)
                    if (printable(ld32(p + o2), &str)) LOG_I("replay:     +%03x -> %08x +%02x -> '%s'", o, p, o2, str.c_str());
        }
    }
}

// ---- director state ------------------------------------------------------------------------------------------------
const float kSpeeds[] = {0.25f, 0.5f, 1, 2, 4, 8};
constexpr int kNormal = 2;
struct Director {
    bool active = false;      // a replay is playing
    bool paused = false;
    int speed = kNormal;      // index into kSpeeds
    float acc = 0;            // ticks owed
    bool menu = false;        // start was pressed: the game's ticks until the replay moves again
    u32 menu_pos = 0;
    u64 bar_until = 0;        // the bar hides itself after a while without director input
    u16 prev1 = 0, prev2 = 0;
    u32 last_pos = 0;
    u32 session = 0, world = 0;
    bool car_changed = false;  // the watched car is not the game's choice: back to the player's car at the end
} g;

constexpr u16 D1_START = 0x08;
constexpr u16 D2_L2 = 0x01, D2_R2 = 0x02, D2_L1 = 0x04, D2_R1 = 0x08, D2_SQUARE = 0x80;
constexpr u16 kMask2 = D2_L2 | D2_R2 | D2_L1 | D2_R1 | D2_SQUARE;

void show_bar() { g.bar_until = time_usec() + 4000000; }

void step_car(const Replay& r, int dir) {
    const std::vector<u32>& cars = race_cars(r.world);
    if (cars.empty()) return;
    const u32 cur = ld32(r.world + 0x21C);
    int i = 0;
    for (int k = 0; k < (int)cars.size(); k++)
        if (cars[k] == cur) i = k;
    i = (i + dir + (int)cars.size()) % (int)cars.size();
    const u32 e = cars[i];
    st32(r.world + 0x21C, e);   // = 0x1539a0(world, entity)
    st32(r.world + 0x208, ld32(e + 0x6C));
    LOG_I("replay: watching car %d/%zu entity %08x (index %d)", i + 1, (int)cars.size(), e, (s32)ld32(e + 0x6C));
}

void handle_input(const Replay& r) {
    const host::PadState p = host::pad_state();
    const u16 d1 = p.digital1, d2 = p.digital2;
    auto pressed1 = [&](u16 b) { return (d1 & b) && !(g.prev1 & b); };
    auto pressed2 = [&](u16 b) { return (d2 & b) && !(g.prev2 & b); };
    bool any = false;
    if (pressed2(D2_SQUARE)) { g.paused = !g.paused; any = true; }
    if (pressed2(D2_R2)) {
        if (g.paused) g.paused = false;
        else g.speed = std::min(g.speed + 1, (int)std::size(kSpeeds) - 1);
        any = true;
    }
    if (pressed2(D2_L2)) {
        if (g.paused) g.paused = false;
        else g.speed = std::max(g.speed - 1, 0);
        any = true;
    }
    if (pressed2(D2_R1)) { step_car(r, 1); any = g.car_changed = true; }
    if (pressed2(D2_L1)) { step_car(r, -1); any = g.car_changed = true; }
    if (pressed1(D1_START)) {
        g.menu = true;
        g.menu_pos = ld32(r.obj + 0x1C);
    }
    if (any) {
        show_bar();
        LOG_I("replay: %s, speed %gx", g.paused ? "paused" : "playing", kSpeeds[g.speed]);
    }
    g.prev1 = d1;
    g.prev2 = d2;
}

std::string speed_text() {
    if (g.paused) return "PAUSED";
    char s[16];
    const float v = kSpeeds[g.speed];
    if (v < 1) snprintf(s, sizeof s, "1/%dx", (int)(1 / v + 0.5f));
    else snprintf(s, sizeof s, "%dx", (int)v);
    return s;
}

void update_bar(const Replay& r) {
    overlay::ReplayBar b;
    b.shown = g.active && time_usec() < g.bar_until;
    if (b.shown) {
        b.paused = g.paused;
        b.pos = ld32(r.obj + 0x1C) / 60.0f;
        b.length = ld32(r.obj + 0x18) / 60.0f;
        b.speed = speed_text();
        if (const u32 cur = ld32(r.world + 0x21C)) b.driver = car_name(cur);
    }
    overlay::set_replay_bar(b);
}
}  // namespace

// main loop 0x2494a4, before its tests of the frame's tick count (r31): 0x249504 on entry, 0x249654 for each next
// frame, so once per frame on the main thread
void replay_ticks_probe(PPUContext& c) {
    if (!replay_director_on()) {
        if (!log_on()) return;
        const Replay r = find_replay();
        static bool was = false;
        if (r.playing && !was) log_world(r);
        was = r.playing;
        if (r.playing) log_changes(r);
        return;
    }
    const Replay r = find_replay();
    if (r.playing && log_on()) log_changes(r);
    if (!r.playing) {
        if (g.active) {
            // stop replay (0x5eb6d4) leaves the watched car as it is; the replay's start had set the player's
            const bool same = session() == g.session && ld32(g.session + 0x18FC) == g.world;
            if (g.car_changed && same && ld32(g.world + 0x220)) {
                const u32 e = ld32(g.world + 0x220);
                st32(g.world + 0x21C, e);
                st32(g.world + 0x208, ld32(e + 0x6C));
            }
            LOG_I("replay: director off (replay ended%s)", g.car_changed && same ? ", player's car watched again" : "");
            g = Director{};
            update_bar(r);
        }
        return;
    }
    if (!g.active) {
        g = Director{};
        g.active = true;
        g.session = r.session;
        g.world = r.world;
        g.prev1 = host::pad_state().digital1;
        g.prev2 = host::pad_state().digital2;
        show_bar();
        LOG_I("replay: director on (length %.1f s)", ld32(r.obj + 0x18) / 60.0f);
        log_world(r);
    }
    handle_input(r);
    const u32 pos = ld32(r.obj + 0x1C);
    if (g.menu && pos != g.menu_pos && pos != g.last_pos) g.menu = false;   // the menu closed and the replay moves
    g.last_pos = pos;
    update_bar(r);
    if (g.menu) return;   // the game's ticks
    const u32 base = (u32)c.r[31];
    if (g.paused) {
        c.r[31] = 0;
        return;
    }
    g.acc += base * kSpeeds[g.speed];
    const u32 n = (u32)g.acc;
    g.acc -= (float)n;
    c.r[31] = n;
}

// cellPadGetData: the director's buttons are not the game's while a replay plays
void replay_pad_filter(host::PadState& s) {
    if (!replay_director_on() || !g.active) return;
    s.digital2 &= (u16)~kMask2;
    // pressures: [7] square, [8] L1, [9] R1, [10] L2, [11] R2
    for (int i = 7; i < 12; i++) s.pressure[i] = 0;
}
