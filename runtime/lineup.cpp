// Season packs with another order of teams and drivers (tools/entities.py arrange).
//
// The driver records' 'order' (+0x14C) is the slot on the entry list, but the game also uses it as the number of a
// sound: order / 2 picks the car's engine sound and the team the radio talks about, order the driver's name. In the
// game's own database those agree (Renault 0-1, McLaren 2-3, ...); once a pack moves teams or drivers they would not.
// The probes below turn an order into the sound numbers of the team and driver that hold it now, found by their
// database ids. Career mode copies the displaced driver's order into the player's record (0x28E078), so the order,
// not the record, is what identifies the car. With the original order every number stays as it is.
#include <array>

#include "kernel.h"
#include "mods.h"

namespace {
constexpr u32 TEAMS = 0x1011087C, DRIVERS = 0x1011088C;   // vectors of record pointers: +4 begin, +8 end
constexpr u32 RACE_TEAMS = 11, RACE_DRIVERS = 22;

// the 2006 teams and drivers in the order of their sounds
const char* const TEAM_IDS[RACE_TEAMS] = {"Renault", "McLaren", "Ferrari", "Toyota", "Williams", "Honda", "RedBull",
                                          "BMW", "Midland", "ToroRosso", "SuperAguri"};
const char* const DRIVER_IDS[RACE_DRIVERS] = {
    "falonso", "gfisichella", "kraikkonen", "jmontoya", "mschumacher", "fmassa", "jtrulli", "rschumacher",
    "mwebber", "nrosberg", "jbutton", "rbarrichello", "dcoulthard", "cklien", "nheidfeld", "jvilleneuve",
    "calbers", "tmonteiro", "vliuzzi", "sspeed", "tsato", "yide"};

struct Sounds {
    std::array<u32, RACE_DRIVERS> driver, team;   // by order
};

u32 find(const char* const* ids, u32 count, u32 hash, u32 fallback) {
    for (u32 n = 0; n < count; n++)
        if (mods::name_hash(ids[n]) == hash) return n;
    return fallback;
}

// the driver id hash of a team's seat (+0x40 driver1, +0x44 driver2: offsets into the string pool)
u32 seat(u32 team, u32 field) {
    const u32 off = ld32(team + field);
    return off == 0xFFFFFFFF ? 0 : mods::name_hash((const char*)gmem(ld32(team + 0xC) + off));
}

Sounds make_sounds() {
    Sounds s;
    for (u32 n = 0; n < RACE_DRIVERS; n++) s.driver[n] = n, s.team[n] = n / 2;
    const u32 drivers = ld32(DRIVERS + 4), teams = ld32(TEAMS + 4);
    if (!drivers || !teams || (ld32(DRIVERS + 8) - drivers) / 4 < RACE_DRIVERS ||
        (ld32(TEAMS + 8) - teams) / 4 < RACE_TEAMS)
        return s;
    bool moved = false;
    for (u32 n = 0; n < RACE_DRIVERS; n++) {
        const u32 rec = ld32(drivers + 4 * n), id = ld32(rec + 0xD8), order = ld32(rec + 0x14C);
        if (order >= RACE_DRIVERS) continue;
        s.driver[order] = find(DRIVER_IDS, RACE_DRIVERS, id, order);
        for (u32 t = 0; t < RACE_TEAMS; t++) {
            const u32 team = ld32(teams + 4 * t);
            if (seat(team, 0x40) == id || seat(team, 0x44) == id)
                s.team[order] = find(TEAM_IDS, RACE_TEAMS, ld32(team + 0x24), order / 2);
        }
        moved = moved || s.driver[order] != order || s.team[order] != order / 2;
    }
    if (moved) {
        std::string line;
        for (u32 n = 0; n < RACE_DRIVERS; n++)
            line += " " + std::to_string(s.driver[n]) + "/" + std::to_string(s.team[n]);
        LOG_I("lineup: driver / team sounds by entry list order:%s", line.c_str());
    }
    return s;
}

const Sounds& sounds() {   // first used in a race: the database was loaded at boot and stays
    static const Sounds s = make_sounds();
    return s;
}
}  // namespace

// 0x652494 in 0x6523AC (a line of the radio): r9 = the driver's order, r0 = order / 2, about to be stored as the
// numbers of the spoken driver and team
void lineup_speech(PPUContext& c) {
    const u32 order = (u32)c.r[9];
    if (order >= RACE_DRIVERS) return;
    c.r[9] = sounds().driver[order];
    c.r[0] = sounds().team[order];
}

// 0x4C71CC in 0x4C7168 (a car's engine sound): r3 = the driver's order, about to be halved into the sound's number
void lineup_engine(PPUContext& c) {
    const u32 order = (u32)c.r[3];
    if (order < RACE_DRIVERS) c.r[3] = sounds().team[order] * 2;
}

// 0x4BE910 in 0x4BE1C0 (the race's sounds): r0 = the order of the player's driver, about to be stored
void lineup_player(PPUContext& c) {
    const u32 order = (u32)c.r[0];
    if (order < RACE_DRIVERS) c.r[0] = sounds().team[order] * 2 + (sounds().driver[order] & 1);
}
