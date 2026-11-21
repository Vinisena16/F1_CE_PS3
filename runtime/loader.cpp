// Loads the game's executable segments (EBOOT.BIN, or ONLINE.BIN in f1ce_online; decrypted by self_decrypt.cpp) into guest memory at runtime
// and starts the main PPU thread. The recompiled code never embeds game data; it is read from the user's copy.
#include "host.h"
#include "kernel.h"
#include "self_decrypt.h"

#include <filesystem>
#include <fstream>
#include <vector>
#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <shellapi.h>
#else
#include <spawn.h>
extern char** environ;
#endif

u32 g_tls_addr = 0, g_tls_filesz = 0, g_tls_memsz = 0;
u32 g_malloc_pagesize = 0x100000;
std::string g_game_dir;
std::string g_data_dir;
std::string g_title_id = "UNKNOWN00";

// PARAM.SFO: little-endian key/value table; we only need TITLE_ID.
static void read_title_id(const std::string& sfo_path) {
    std::ifstream f(std::filesystem::u8path(sfo_path), std::ios::binary);
    std::vector<u8> d((std::istreambuf_iterator<char>(f)), {});
    if (d.size() < 20 || memcmp(d.data(), "\0PSF", 4)) return;
    auto le32 = [&](size_t o) { u32 v; memcpy(&v, &d[o], 4); return v; };
    auto le16 = [&](size_t o) { u16 v; memcpy(&v, &d[o], 2); return v; };
    u32 keys = le32(8), data = le32(12), n = le32(16);
    for (u32 i = 0; i < n && 20 + i * 16 + 16 <= d.size(); i++) {
        size_t e = 20 + i * 16;
        std::string key((const char*)&d[keys + le16(e)]);
        if (key == "TITLE_ID") {
            g_title_id = std::string((const char*)&d[data + le32(e + 12)]);
            LOG_I("title id: %s", g_title_id.c_str());
        }
    }
}

namespace {
template <typename T> T be(const u8* p) {
    T v;
    memcpy(&v, p, sizeof(T));
    if constexpr (sizeof(T) == 2) return __builtin_bswap16(v);
    else if constexpr (sizeof(T) == 4) return __builtin_bswap32(v);
    else return __builtin_bswap64(v);
}
}  // namespace

struct LoadedProcess { u32 entry_opd; u32 stack_size; s32 prio; };

u32 g_ticks_per_frame_ea = 0;   // set by F1CE_60FPS

LoadedProcess load_elf(const std::vector<u8>& d) {
    LoadedProcess p{be<u32>(&d[24 + 4]), 0x100000, 1001};
    u64 phoff = be<u64>(&d[32]);
    u16 phnum = be<u16>(&d[56]);
    for (u16 i = 0; i < phnum; i++) {
        const u8* ph = &d[phoff + i * 56];
        u32 type = be<u32>(ph);
        u64 off = be<u64>(ph + 8), va = be<u64>(ph + 16), fsz = be<u64>(ph + 32), msz = be<u64>(ph + 40);
        if (type == 1 && msz) {
            memcpy(gmem((u32)va), &d[off], fsz);
            memset(gmem((u32)(va + fsz)), 0, msz - fsz);
            LOG_I("segment %08llx filesz=%llx memsz=%llx", (unsigned long long)va, (unsigned long long)fsz,
                  (unsigned long long)msz);
        } else if (type == 7) {
            g_tls_addr = (u32)va; g_tls_filesz = (u32)fsz; g_tls_memsz = (u32)msz;
        } else if (type == 0x60000001 && fsz >= 0x1c) {   // sys_process_param
            const u8* pp = &d[off];
            p.prio = (s32)be<u32>(pp + 0x10);
            // SYS_PROCESS_PRIMARY_STACK_SIZE_* codes: 0x10=32K, 0x20=64K, 0x30=96K, 0x40=128K, 0x50=256K, 0x60=512K, 0x70=1M
            static const u32 sizes[] = {0x100000, 0x8000, 0x10000, 0x18000, 0x20000, 0x40000, 0x80000, 0x100000};
            u32 code = be<u32>(pp + 0x14);
            p.stack_size = code <= 0x70 && !(code & 0xF) ? sizes[code >> 4] : code;
            g_malloc_pagesize = be<u32>(pp + 0x18);
            LOG_I("process param: sdk=%08x prio=%d stack=%x malloc_pagesize=%x", be<u32>(pp + 0xc), p.prio,
                  p.stack_size, g_malloc_pagesize);
        }
    }
    LOG_I("TLS image %08x filesz=%x memsz=%x", g_tls_addr, g_tls_filesz, g_tls_memsz);
    if (getenv("F1CE_60FPS")) {
        // game ticks per frame, read by the main loop (0x2494a4, ONLINE.BIN 0x1a2674) every frame; 2 on the 01.00 disc.
        // do_flip sets it from the time each frame took and the vblank thread doubles its rate for the game's frame limiter
        const u32 ticks_per_frame = g_online_image ? 0x101DCB5C : 0x10044288;
        u32 v = ld32(ticks_per_frame);
        if (v == 2) { g_ticks_per_frame_ea = ticks_per_frame; LOG_I("60 fps: adaptive ticks per frame"); }
        else LOG_W("60 fps: unexpected ticks per frame %08x (not the 01.00 executable?): not patched", v);
    }
    return p;
}

// Starts the executable `stem` (f1ce or f1ce_online) from this one's folder on `game` with the guest arguments, for
// sys_game_process_exitspawn. A suffix of this executable's name (f1ce_pgo.exe -> f1ce_online_pgo.exe) is kept when
// such a file exists.
bool spawn_sibling(const char* stem, const std::string& game, const std::vector<std::string>& guest_args) {
    namespace fs = std::filesystem;
#ifdef _WIN32
    wchar_t self[32768];
    const DWORD n = GetModuleFileNameW(nullptr, self, 32768);
    if (!n || n >= 32768) return false;
    const fs::path me(self);
    const std::wstring ext = me.extension().wstring();
#else
    std::error_code ec;
    const fs::path me = fs::read_symlink("/proc/self/exe", ec);
    if (ec) return false;
    const std::string ext;
#endif
    const std::string mine = PATH_STR(me.stem());
    const std::string base = g_online_image ? "f1ce_online" : "f1ce";
    fs::path exe = me.parent_path() / fs::u8path(std::string(stem));
    exe += ext;
    if (mine.size() > base.size() && mine.compare(0, base.size(), base) == 0) {   // keep a suffix
        fs::path alt = me.parent_path() / fs::u8path(std::string(stem) + mine.substr(base.size()));
        alt += ext;
        if (fs::exists(alt)) exe = alt;
    }
    std::vector<std::string> argv{PATH_STR(exe), game};
    for (const std::string& a : guest_args) argv.insert(argv.end(), {"--guest-arg", a});
    LOG_I("starting %s", argv[0].c_str());
    std::fflush(nullptr);
#ifdef _WIN32
    // CommandLineToArgvW quoting: backslashes double only before a quote
    std::wstring cmd;
    for (const std::string& a : argv) {
        const std::wstring w = fs::u8path(a).wstring();
        if (!cmd.empty()) cmd += L' ';
        cmd += L'"';
        size_t bs = 0;
        for (wchar_t ch : w) {
            if (ch == L'\\') { bs++; continue; }
            cmd.append(ch == L'"' ? 2 * bs + 1 : bs, L'\\');
            bs = 0;
            cmd += ch;
        }
        cmd.append(2 * bs, L'\\');
        cmd += L'"';
    }
    STARTUPINFOW si{};
    si.cb = sizeof(si);
    PROCESS_INFORMATION pi{};
    // (no console window of its own when this one has none: started by the launcher)
    const DWORD flags = GetConsoleWindow() ? 0 : CREATE_NO_WINDOW;
    if (!CreateProcessW(exe.wstring().c_str(), cmd.data(), nullptr, nullptr, FALSE, flags, nullptr, nullptr, &si, &pi)) return false;
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return true;
#else
    std::vector<char*> cargv;
    for (std::string& a : argv) cargv.push_back(a.data());
    cargv.push_back(nullptr);
    pid_t pid;
    return posix_spawn(&pid, argv[0].c_str(), nullptr, nullptr, cargv.data(), environ) == 0;
#endif
}

void limit_cpus();   // core.cpp

int main(int argc, char** argv) {
    namespace fs = std::filesystem;
    limit_cpus();
    // Usage: f1ce [path/to/PS3_GAME] [--trace] [--guest-arg <argument>]...   or: f1ce_online --room (F1CE_NET_PORT)
    // (--guest-arg: the game's own arguments, passed by sys_game_process_exitspawn between f1ce and f1ce_online)
    std::string game = "PS3_GAME";
    std::vector<std::string> guest_args;
    std::vector<std::string> args(argv, argv + argc);
#ifdef _WIN32
    // argv is in the ANSI code page, which mangles accented folder names (C:\Users\José\...): take the UTF-16
    // command line and keep the arguments as UTF-8 like every other path here
    if (wchar_t** wargv = CommandLineToArgvW(GetCommandLineW(), &argc)) {
        args.clear();
        for (int i = 0; i < argc; i++) args.push_back(PATH_STR(fs::path(wargv[i])));
        LocalFree(wargv);
    }
#endif
    for (int i = 1; i < argc; i++) {
        const std::string& a = args[i];
        if (a == "--room") {   // a multiplayer room alone, no game (hle_net.cpp)
            [[noreturn]] void net_room_main();
            net_room_main();
        }
        if (a == "--trace") g_log_trace = true;
        else if (a == "--guest-arg" && i + 1 < argc) guest_args.push_back(args[++i]);
        else game = a;
    }
    // f1ce_online started on its own (the launcher's Multiplayer button): the argument the front end's exitspawn passes,
    // {language code}, which ONLINE.BIN's main looks for and hands back to EBOOT.BIN when the player leaves
    if (g_online_image && guest_args.empty()) {
        int boot_language_code();   // hle_boot.cpp
        guest_args.push_back("{" + std::to_string(boot_language_code()) + "}");
    }
    if (!fs::exists(fs::u8path(game) / "USRDIR"))
        fatal("PS3_GAME folder not found at '%s'. Pass it as first argument.", game.c_str());
    // the disc's EBOOT.BIN, decrypted here (or a checked EBOOT.elf)
    std::vector<u8> elf;
    if (const std::string e = eboot::load(fs::u8path(game), elf, g_online_image ? "ONLINE" : "EBOOT"); !e.empty())
        fatal("%s\n\nGame folder: %s", e.c_str(), game.c_str());
    g_game_dir = PATH_STR(fs::absolute(fs::u8path(game)));
    g_data_dir = PATH_STR(fs::current_path() / "ps3_data");
    fs::create_directories(fs::u8path(g_data_dir) / "dev_hdd0" / "game");
    LOG_I("F1 Championship Edition static recompilation - game dir: %s", g_game_dir.c_str());

    read_title_id(PATH_STR(fs::u8path(g_game_dir) / "PARAM.SFO"));
    host::init();
    mem::init();
    extern void start_watchdog(u32);
    start_watchdog(20);
    extern void start_profiler();
    start_profiler();
    extern void start_host_profiler();
    start_host_profiler();
    if (getenv("F1CE_SPU_BENCH")) { extern void spu_bench(); spu_bench(); exit(0); }
    LoadedProcess p = load_elf(elf);
    std::vector<u8>().swap(elf);
    for (const std::string& a : guest_args) LOG_I("guest argument: %s", a.c_str());
    run_main_thread(p.entry_opd, p.stack_size, p.prio, guest_args);
    LOG_I("main thread returned");
    return 0;
}
