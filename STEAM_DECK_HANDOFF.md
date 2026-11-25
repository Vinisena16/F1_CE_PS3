# Steam Deck optimisation: handoff (2026-11-25)

For a Claude Code session running on the Steam Deck. The Windows session that wrote this did all of today's work on a
desktop PC (Ryzen 5 5600G 6c/12t, RX 6600) and could never run the Linux build. Your job: find out what limits the game
on the Deck and make it faster there. Read this first, then `GEOMETRY_JOB.md` (today's SPU / renderer work in detail)
and `README.md` ("Linux and Steam Deck").

## Where things stand

- `main` at 25d5d10 = release 0.6 (zips made on Windows, not yet published by the user when this was written). It has
  the native SPU code (below), the renderer index-list change, the AI pace / pit strategy / Discord options (PR #18)
  and the Linux folder package.
- Branch `rsx-parse-thread` (75c623d, not merged): FIFO parsing on its own thread, renderer fed through an ordered
  queue (`F1CE_FIFO_THREAD=0` switches it off). On the PC it took the renderer thread from 0.90 to 0.80 core but gave
  no frame rate, because the renderer no longer limited the PC. **On the Deck's slower cores the renderer thread may
  well be the limit: measure this branch there.**

## What changed today (the things to measure on the Deck)

All switchable at run time, so each can be A/B tested without rebuilding:

| Change | Switch (0 = off) | PC result |
|---|---|---|
| Geometry job (w1/w2) as generated native C++ call trees | `F1CE_GEOM_NATIVE` | part of the gain below |
| w0's job (frame job lists) and the job manager (SPURS policy module) generated, entered by the dispatcher | `F1CE_W0_NATIVE`, `F1CE_PM_GEN` | 60 fps + `F1CE_CAR_LOD=0`: ~29 -> ~34 fps; 4 CPUs (`F1CE_CPUS=4`): 23 -> 25 fps |
| The other 9 job binaries (audio, rain, ...) generated | `F1CE_JOBS_GEN` | no measurable change |
| Index lists drawn once skip hashing and the cache | (none) | +2.6% at 60 fps (renderer 0.95 -> 0.92 core) |

The generator is `tools/spu_native_fn.py` (reads `input/EBOOT.elf`, writes `generated/spu_geom/*.cpp`, which are
committed). Diagnostics: `F1CE_NATIVE_STATS=1` (entries and early stops per second; costly), `F1CE_NATIVE_CHECK=1`
(native entries against the interpreter on copies; only noise differences known: decrementer reads, the atomic line at
LS 0x80-0xFF), `F1CE_GEOM_CHECK=1`.

## Known Deck facts (from earlier sessions)

- The user ran the AppImage on a Deck on 2026-11-13: runs well, below 30 fps at the race start, 22-23 W at peak.
- 2026-11-14: the Deck is **power-limited**: the GPU clock drops at the race start (the CPU takes the power budget).
  The next target then was the race start's SPU CPU cost - today's native SPU code is aimed at exactly that, so the
  first question is how much of it shows on the Deck.
- 4 cores / 8 threads: 5 SPU workers + main PPU thread + renderer + audio compete. On Linux the critical threads cannot
  be raised (no permission on SteamOS), so background threads are lowered instead (`lower_thread_priority`).
- Deck defaults (AppRun): Vulkan, 1280x800, full screen, original 30 fps mode. 60 fps mode and `F1CE_CUBE_HZ` skips can
  freeze: test in **30 fps mode** unless the user asks otherwise.
- "Cannot mount AppImage, please check your FUSE setup" showed up on the user's Deck today, also with the old 0.4.5
  AppImage (so the Deck changed, not the package). Workarounds: `APPIMAGE_EXTRACT_AND_RUN=1`, `--appimage-extract`, or
  the 0.6 zip's `F1CE-x86_64-folder.tar.gz` (run `F1CE/AppRun`). If you can, find the real cause (`ls -l /dev/fuse`,
  `which fusermount fusermount3`, SteamOS version).
- Data folder: `~/.local/share/f1ce-recomp/` (launcher ini, `ps3_data` saves, `f1ce_recomp.log`, MODS, wheels);
  shader cache `~/.cache/f1ce-recomp/shader_cache/`. `--where` prints them.

## Setting up a build on the Deck

Not in git, copy them from the user's PC (or ask the user):

- `input/EBOOT.elf`: the decrypted game executable (the user's own copy). Needed by `tools/recomp.py` (PPU translation
  into `generated/`, git-ignored) and by `tools/spu_native_fn.py`. Without it you can still build if `generated/` is
  copied over from the PC.
- `third_party/SDL3-3.4.18` and `third_party/glslang` (sources).
- The game folder `PS3_GAME` (disc version 01.00, EUR BCES00005).
- The scripted-test save data `dbg/menu/ps3_data` (a profile already exists, so menu scripts start at Press Start).

SteamOS has a read-only root: build inside a container (`distrobox`/`toolbox` if present, or podman with
`linux/Dockerfile`). `linux/build.sh --deps` prints the packages for the container's distribution;
`linux/build.sh --no-appimage` builds `build-linux-native/f1ce`. Regenerate the PPU code first with
`python3 tools/recomp.py` (16 s on the PC). The Windows release build used the zig cross build
(`linux/build_from_windows.sh`); a native build has Wayland too.

## Running tests

`tools/race_test.sh` / `tools/menu_test.sh` are written for Windows: `GAME=` holds a Windows path and the exe defaults to
`../../build-win/f1ce.exe` (override with `EXE=`). Adapt both before use. What they do:

- `F1CE_SKIP_INTRO=1 F1CE_LANGUAGE=english` boot straight to Press Start; `F1CE_INPUT="9:start,13:cross,..."` presses
  buttons at absolute seconds (`47:cross:900` = hold for 900 s); `DRIVE=1` builds the input list for a Quick Race at
  Silverstone with the throttle held; `F1CE_AI_DRIVE=5` lets the AI drive the player's car; `F1CE_QUIT_AFTER_3D=<s>`
  quits that long after the race scene and saves the last frame; `F1CE_HIDDEN=1` no window, `SDL_AUDIO_DRIVER=dummy`
  silent.
- Track / weather / distance: `F1CE_QR_TRACK=spain`, `F1CE_QR_WEATHER=heavyrain`, `F1CE_QR_DISTANCE=10`.
- The PC benchmark ("carbench"): race_test with `DRIVE=1 QUIT_AFTER=40 F1CE_RENDERER=vulkan F1CE_AI_DRIVE=5
  F1CE_MOTION_BLUR=0 F1CE_WHEEL=0`, then the averages of the 6 log windows after the race scene of: `cpu: N cores at
  F fps`, `thread cpu:` (per thread: spu w0..w4, rsx, ppu main), `SPURS runs/instructions` (SPU instructions per
  frame), `perf:` (draws). Noise run to run is a few %: judge only with **alternating on/off pairs** (3 pairs).

## Log lines worth reading

- `cpu: X cores at Y fps` and `thread cpu: spu w0 .. rsx .. ppu main ..` every 5 s: who is busy.
- `perf: ... per frame ms: program / textures / vertices / swap` and `perf front:` (renderer front end),
  `perf vtx:` (index / hash / convert / draw call), `perf gpu: busy X ms per frame` (GPU time, Vulkan timestamps).
- `pacing: slow frame ...` (frames over 45 ms); frames over 300 ms also dump `spurs workers:` / `spurs workloads:`.
- `F1CE_SPU_PROFILE=<seconds>`: 10 s of SPU pc samples per workload (`spu profile ...` lines); works on Linux.
- `F1CE_HOST_PROFILE` (host function sampling) is **Windows only** (`core.cpp`, `#ifdef _WIN32`). On the Deck use
  `perf record -g` from a container, or port the sampler; build the hot files with `-gline-tables-only` for lines.

## What to look at first

1. **Baseline at the race start, 30 fps mode:** fps, `thread cpu`, and power/clocks while it runs
   (`/sys/class/drm/card*/device/gpu_busy_percent`, `pp_dpm_sclk`, `hwmon*/power1_average`, CPU MHz). Decide: CPU-bound
   (which thread), GPU-bound, or power-limited (both clocks dropping together).
2. **Today's SPU work on the Deck:** alternating pairs with `F1CE_W0_NATIVE=0 F1CE_PM_GEN=0 F1CE_JOBS_GEN=0
   F1CE_GEOM_NATIVE=0` against the default. On a power-limited Deck less SPU work should leave more watts for the GPU:
   look at GPU clock and power, not only fps.
3. **Branch `rsx-parse-thread`** on the Deck: if `rsx` is near 1.0 core in `thread cpu`, it may help there.
4. **Thread count vs 4 cores:** the 5 SPU workers mostly wait (on the PC, 70% of worker samples were waits); check
   whether spinning (GETLLAR polls yield after 8 unchanged reads, `spu.cpp`) costs power on the Deck.
5. If GPU-bound: the video options (resolution scale, MSAA, post effects, car reflections, crowd) and their GPU cost.

## Open problems you may run into

- A rare stall: 5-15 s of 0.4-1.3 s frames, then normal. 2 of ~80 PC runs with the native SPU code, never caught
  without it (but too few runs to be sure). The log then shows all workers idle, every ready count 0, workloads 6/7/8
  holding queued jobs, main sleeping in fn 0x48AC34.
- Unexplained: the PC's 4-CPU benchmark ran ~25 fps in the morning and ~37 fps in the evening (main with PR #18 and
  regenerated PPU code, fewer SPU instructions per frame). Not looked into yet.
- OpenGL: an intermittent boot fault "RSX: unmapped IO offset 1f800000" (FIFO parse of float data before any draw).

## The user's standing rules

- Test in the original 30 fps mode unless told otherwise.
- Never change the shadow update frequency; optimise per-pass cost only.
- Never kill the game or launcher if the user is playing; ask.
- Compare with alternating pairs, report measured numbers with the hardware, say what was not tested.
- Commit or push only when asked. The user communicates in English; the Windows session also sent Telegram status
  notes (that skill drives Telegram Desktop on Windows and will not work on the Deck).
