// Debug: F1CE_CAR_LOG=<file.csv> logs every car of a race, for car performance work (dynamics.bin, tools/dynamics.py).
// Called for each car and physics substep (wheel_update_hook in hle_physics.cpp, car = the physics car object).
//  - "car" lines, once per car and control type: the dynamics.bin key and the values the car really uses, from its
//    copies of the records: aero (section 1) at +0x560, engine (section 2) at +0x1028, gearbox (section 3) at +0x760.
//  - "s" lines every half second of game time: speed (+0x3C0, m/s), distance driven and peak speed since the log began.
// The entity of a car is at +0x115C: driver id hash +0xC, active controller +0x138 (= +0x28 while the AI drives).
#include <bit>
#include <cstdio>
#include <cstdlib>
#include <unordered_map>

#include "kernel.h"

void car_log(u32 car) {
    static const char* path = getenv("F1CE_CAR_LOG");
    if (!path) return;
    static FILE* f = [] {
        FILE* o = fopen(path, "w");
        if (o) fprintf(o, "car,car_ea,key,driver,ai,mass19,aero20,drag24,down25,aero26,eng16,eng17,eng18,gear1,gear7\n"
                          "s,time,car_ea,ai,speed,dist,peak\n");
        return o;
    }();
    if (!f) return;
    struct State { u32 calls = 0; int ai = -1; double dist = 0; float peak = 0; };
    static std::unordered_map<u32, State> cars;
    const u32 entity = ld32(car + 0x115C);
    if (!entity) return;
    auto fl = [&](u32 off) { return std::bit_cast<float>(ld32(car + off)); };
    State& s = cars[car];
    const int ai = ld32(entity + 0x138) == ld32(entity + 0x28);
    if (ai != s.ai) {
        s.ai = ai;
        fprintf(f, "car,%08x,%08x,%08x,%d,%g,%g,%g,%g,%g,%g,%g,%g,%g,%g\n", car, ld32(car + 0x100C), ld32(entity + 0xC), ai,
                fl(0x560 + 76), fl(0x560 + 80), fl(0x560 + 96), fl(0x560 + 100), fl(0x560 + 104), fl(0x1028 + 64),
                fl(0x1028 + 68), fl(0x1028 + 72), fl(0x760 + 8), fl(0x760 + 32));
    }
    constexpr double dt = 1.0 / 180;   // three substeps per tick of 1/60 s
    const float v = fl(0x3C0);
    s.dist += v * dt;
    s.peak = std::max(s.peak, v);
    if (++s.calls % 90 == 0) {
        fprintf(f, "s,%.2f,%08x,%d,%.2f,%.1f,%.2f\n", s.calls * dt, car, ai, v, s.dist, s.peak);
        fflush(f);
    }
}
