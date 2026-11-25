// Discord Rich Presence over Discord's local IPC socket / named pipe (zero external SDK dependencies).
// Enabled with F1CE_DISCORD_RPC=1 and F1CE_DISCORD_APP_ID=<application_id> (created at
// https://discord.com/developers/applications; optional Rich Presence art asset key "f1ce").
// Runs on a detached background thread, polls guest memory safely every 4 s with mem::readable(), and reconnects
// automatically if Discord starts or restarts while the game is open.
#include <atomic>
#include <cctype>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <string>
#include <thread>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#else
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#endif

#include "kernel.h"

namespace {

std::string json_escape(const std::string& s) {
    std::string out;
    out.reserve(s.size() + 8);
    for (unsigned char ch : s) {
        if (ch == '"' || ch == '\\') { out.push_back('\\'); out.push_back((char)ch); }
        else if (ch >= 0x20) out.push_back((char)ch);
    }
    return out;
}

std::string read_guest_cstr(u32 ea, u32 max_len = 64) {
    if (!ea) return {};
    std::string out;
    for (u32 i = 0; i < max_len && mem::readable(ea + i, 1); i++) {
        const u8 ch = ld8(ea + i);
        if (!ch) break;
        if (ch >= 0x20) out.push_back((char)ch);
    }
    return out;
}

struct TrackLabel {
    const char* stem;
    const char* display;
};

constexpr TrackLabel kTracks[] = {
    {"bahrain",     "Sakhir, Bahrain"},
    {"sepang",      "Sepang, Malaysia"},
    {"melbourne",   "Albert Park, Melbourne"},
    {"imola",       "Imola, San Marino"},
    {"nurburgring", "Nurburgring, Europe"},
    {"barcelona",   "Catalunya, Spain"},
    {"montecarlo",  "Circuit de Monaco"},
    {"silverstone", "Silverstone, Great Britain"},
    {"montreal",    "Montreal, Canada"},
    {"indy",        "Indianapolis, USA"},
    {"magnycours",  "Magny-Cours, France"},
    {"hockenheim",  "Hockenheim, Germany"},
    {"hungaroring", "Hungaroring, Hungary"},
    {"istanbul",    "Istanbul Park, Turkey"},
    {"monza",       "Monza, Italy"},
    {"shanghai",    "Shanghai, China"},
    {"suzuka",      "Suzuka, Japan"},
    {"interlagos",  "Interlagos, Brazil"},
    {"jerez",       "Circuito de Jerez"},
};

constexpr const char* kTeamNames[11] = {
    "Renault", "McLaren", "Ferrari", "Toyota", "Williams", "Honda",
    "Red Bull", "BMW Sauber", "MF1", "Toro Rosso", "Super Aguri",
};

std::string pretty_track(const std::string& raw) {
    std::string lo;
    lo.reserve(raw.size());
    for (unsigned char ch : raw) lo.push_back((char)std::tolower(ch));
    for (const auto& t : kTracks)
        if (lo == t.stem) return t.display;
    return raw.empty() ? "On Track" : raw;
}

struct PresenceState {
    std::string details;
    std::string state;
    bool in_session = false;
    bool operator==(const PresenceState& o) const {
        return in_session == o.in_session && details == o.details && state == o.state;
    }
};

PresenceState sample_game_state() {
    PresenceState ps;
    if (!mem::readable(0x100DB3B8, 4)) {
        ps.details = "In Menus";
        ps.state = "Formula One Championship Edition";
        return ps;
    }
    const u32 track_logic = ld32(0x100DB3B8);
    if (!track_logic || !mem::readable(track_logic + 0x7F0, 4)) {
        ps.details = "In Menus";
        ps.state = "Formula One Championship Edition";
        return ps;
    }
    const u32 track_info = ld32(track_logic + 0x7F0);
    if (!track_info || !mem::readable(track_info, 0x20)) {
        ps.details = "In Menus";
        ps.state = "Formula One Championship Edition";
        return ps;
    }

    ps.in_session = true;
    std::string track_stem;
    const s32 name_off = (s32)ld32(track_info + 0x1C);
    if (name_off >= 0) track_stem = read_guest_cstr(ld32(track_info + 0x00) + (u32)name_off, 48);
    ps.details = pretty_track(track_stem);

    // Total laps (0x11EB94: TrackLogic + 0x1A24)
    u32 total_laps = 0;
    if (mem::readable(track_logic + 0x1A24, 4)) total_laps = ld32(track_logic + 0x1A24);

    // Locate human car in TrackLogic + 0x1AC8 (11 teams x 12-byte stride)
    int human_team = -1;
    u32 human_car = 0;
    if (mem::readable(track_logic + 0x1AC8, 11 * 12)) {
        for (int t = 0; t < 11 && !human_car; t++) {
            for (int s = 0; s < 2 && !human_car; s++) {
                const u32 car = ld32(track_logic + 0x1AC8 + (u32)(t * 12 + s * 4));
                if (!car || !mem::readable(car + 0x70, 4)) continue;
                bool is_human = (ld32(car + 0x64) & 2u) != 0;
                if (!is_human) {
                    const u32 slot = ld32(car + 0x6C);
                    if (slot < 22 && mem::readable(track_logic + 0x1A5C + 4 * slot, 4) &&
                        ld32(track_logic + 0x1A5C + 4 * slot) == 0)
                        is_human = true;
                }
                if (is_human) {
                    human_car = car;
                    human_team = t;
                }
            }
        }
    }

    std::string who = (human_team >= 0 && human_team < 11) ? kTeamNames[human_team] : "";
    if (human_car && mem::readable(human_car + 0x5C, 2) && total_laps >= 1 && total_laps <= 100) {
        const u32 cur_lap = std::min<u32>((u32)ld16(human_car + 0x5C) + 1u, total_laps);
        char buf[96];
        if (!who.empty()) snprintf(buf, sizeof(buf), "%s - Lap %u/%u", who.c_str(), cur_lap, total_laps);
        else snprintf(buf, sizeof(buf), "Race - Lap %u/%u", cur_lap, total_laps);
        ps.state = buf;
    } else if (!who.empty()) {
        ps.state = who + " - On Track";
    } else {
        ps.state = "On Track";
    }
    return ps;
}

class DiscordIpc {
public:
    ~DiscordIpc() { close_conn(); }

    bool connected() const {
#ifdef _WIN32
        return pipe_ != INVALID_HANDLE_VALUE;
#else
        return fd_ >= 0;
#endif
    }

    void close_conn() {
#ifdef _WIN32
        if (pipe_ != INVALID_HANDLE_VALUE) { CloseHandle(pipe_); pipe_ = INVALID_HANDLE_VALUE; }
#else
        if (fd_ >= 0) { close(fd_); fd_ = -1; }
#endif
    }

    bool connect_and_handshake(const std::string& app_id) {
        close_conn();
#ifdef _WIN32
        for (int i = 0; i < 10; i++) {
            char name[64];
            snprintf(name, sizeof(name), "\\\\.\\pipe\\discord-ipc-%d", i);
            HANDLE h = CreateFileA(name, GENERIC_READ | GENERIC_WRITE, 0, nullptr, OPEN_EXISTING, 0, nullptr);
            if (h != INVALID_HANDLE_VALUE) { pipe_ = h; break; }
        }
#else
        const char* bases[3] = {getenv("XDG_RUNTIME_DIR"), getenv("TMPDIR"), "/tmp"};
        for (const char* base : bases) {
            if (!base || !*base) continue;
            for (int i = 0; i < 10 && fd_ < 0; i++) {
                sockaddr_un addr{};
                addr.sun_family = AF_UNIX;
                snprintf(addr.sun_path, sizeof(addr.sun_path), "%s/discord-ipc-%d", base, i);
                int s = socket(AF_UNIX, SOCK_STREAM, 0);
                if (s < 0) continue;
                if (connect(s, (sockaddr*)&addr, sizeof(addr)) == 0) fd_ = s;
                else close(s);
            }
            if (fd_ >= 0) break;
        }
#endif
        if (!connected()) return false;
        const std::string hs = "{\"v\":1,\"client_id\":\"" + json_escape(app_id) + "\"}";
        if (!send_frame(0, hs)) { close_conn(); return false; }
        // Read handshake response so Discord accepts subsequent SET_ACTIVITY frames
        if (!read_frame_blocking()) { close_conn(); return false; }
        return true;
    }

    bool send_activity(const PresenceState& ps, s64 start_ts, u32 nonce) {
        if (!connected()) return false;
        drain_pending();
#ifdef _WIN32
        const u32 pid = (u32)GetCurrentProcessId();
#else
        const u32 pid = (u32)getpid();
#endif
        char body[1024];
        snprintf(body, sizeof(body),
                 "{\"cmd\":\"SET_ACTIVITY\",\"args\":{\"pid\":%u,\"activity\":{"
                 "\"details\":\"%s\",\"state\":\"%s\","
                 "\"timestamps\":{\"start\":%lld},"
                 "\"assets\":{\"large_image\":\"f1ce\",\"large_text\":\"Formula One Championship Edition\"}"
                 "}},\"nonce\":\"%u\"}",
                 pid, json_escape(ps.details).c_str(), json_escape(ps.state).c_str(), (long long)start_ts, nonce);
        if (!send_frame(1, body)) { close_conn(); return false; }
        drain_pending();
        return true;
    }

private:
    bool write_all(const void* data, size_t n) {
        const u8* p = (const u8*)data;
        size_t done = 0;
        while (done < n) {
#ifdef _WIN32
            DWORD w = 0;
            if (!WriteFile(pipe_, p + done, (DWORD)(n - done), &w, nullptr) || w == 0) return false;
            done += w;
#else
            ssize_t w = write(fd_, p + done, n - done);
            if (w <= 0) return false;
            done += (size_t)w;
#endif
        }
        return true;
    }

    bool read_exact(void* dst, size_t n) {
        u8* p = (u8*)dst;
        size_t done = 0;
        while (done < n) {
#ifdef _WIN32
            DWORD r = 0;
            if (!ReadFile(pipe_, p + done, (DWORD)(n - done), &r, nullptr) || r == 0) return false;
            done += r;
#else
            ssize_t r = read(fd_, p + done, n - done);
            if (r <= 0) return false;
            done += (size_t)r;
#endif
        }
        return true;
    }

    bool send_frame(u32 opcode, const std::string& payload) {
        u32 hdr[2] = {opcode, (u32)payload.size()};
        return write_all(hdr, sizeof(hdr)) && write_all(payload.data(), payload.size());
    }

    bool read_frame_blocking() {
        u32 hdr[2] = {0, 0};
        if (!read_exact(hdr, sizeof(hdr))) return false;
        if (hdr[1] > 65536) return false;
        std::string tmp(hdr[1], '\0');
        return hdr[1] == 0 || read_exact(tmp.data(), hdr[1]);
    }

    void drain_pending() {
#ifdef _WIN32
        DWORD avail = 0;
        while (PeekNamedPipe(pipe_, nullptr, 0, nullptr, &avail, nullptr) && avail >= 8) {
            if (!read_frame_blocking()) { close_conn(); break; }
        }
#else
        char tmp[512];
        while (recv(fd_, tmp, sizeof(tmp), MSG_DONTWAIT) > 0) {}
#endif
    }

#ifdef _WIN32
    HANDLE pipe_ = INVALID_HANDLE_VALUE;
#else
    int fd_ = -1;
#endif
};

void discord_worker(std::string app_id) {
    DiscordIpc ipc;
    PresenceState last;
    bool has_sent = false;
    s64 start_ts = (s64)std::time(nullptr);
    u32 nonce = 1;
    bool logged_connect = false;

    for (;;) {
        if (!ipc.connected()) {
            if (ipc.connect_and_handshake(app_id)) {
                if (!logged_connect) {
                    LOG_I("discord: connected to Discord IPC (app %s)", app_id.c_str());
                    logged_connect = true;
                }
                has_sent = false;
            } else {
                std::this_thread::sleep_for(std::chrono::seconds(10));
                continue;
            }
        }

        const PresenceState cur = sample_game_state();
        if (!has_sent || !(cur == last)) {
            if (has_sent && cur.in_session != last.in_session) start_ts = (s64)std::time(nullptr);
            if (ipc.send_activity(cur, start_ts, nonce++)) {
                last = cur;
                has_sent = true;
            }
        }
        std::this_thread::sleep_for(std::chrono::seconds(4));
    }
}

}  // namespace

void start_discord_rpc() {
    const char* en = getenv("F1CE_DISCORD_RPC");
    if (!en || !*en || strcmp(en, "0") == 0) return;
    const char* id = getenv("F1CE_DISCORD_APP_ID");
    if (!id || !*id) {
        LOG_W("discord: F1CE_DISCORD_RPC is enabled but F1CE_DISCORD_APP_ID is not set (create an app at "
              "https://discord.com/developers/applications and set its Application ID)");
        return;
    }
    std::string app_id(id);
    for (char ch : app_id) {
        if (!std::isdigit((unsigned char)ch)) {
            LOG_W("discord: invalid F1CE_DISCORD_APP_ID '%s' (expected numeric Discord Application ID)", id);
            return;
        }
    }
    LOG_I("discord: Rich Presence enabled (app %s)", app_id.c_str());
    std::thread(discord_worker, std::move(app_id)).detach();
}
