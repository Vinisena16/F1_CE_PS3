# Geometry job HLE outside SPURS: handoff (2026-11-26)

For the Claude Code session on the user's Windows PC (Ryzen 5 5600G 6c/12t, RX 6600). Written by the Steam Deck
session. The user chose the PC for development and the Deck for final measurements. Read this, then
`GEOMETRY_JOB.md` (what the w1/w2 geometry job is and what is native today) and the end of `PROGRESS.md`.

## The task (user decision 2026-11-26: steps 1 and 2 together)

An outside review of the project proposed this order. The user asked for **steps 1 and 2** (not Android, not yet the
renderer steps):

1. **Take w1/w2 out of SPURS.** Do not run the SPURS kernel, the job manager and its atomics for the frame job lists
   (queues `0x10C15100` = w1 and `0x10C29100` = w2, see `runtime/hle_joblist.cpp`). Instead:
   - read the job descriptors straight from the queues in a host thread pool;
   - run a semantic version of the geometry job (blob `0x100A1280`) that reads and writes guest memory directly: no
     LS, no DMA, no GETLLAR/PUTLLC;
   - advance the same counters / fences the PPU waits on (fn `0x551228`, `0x55127c`, `0x5513b8`, the non-blocking
     `0x551410`; see `runtime/spu_pm.cpp` `jobwait_*` and `runtime/core.cpp` `ppu_spin`).
   Leave w0 (frame job list writer), the audio jobs (w6/w7/w8) and everything else on the emulated SPUs.
2. **Rewrite the geometry for what it does, not instruction for instruction.** Only 15 of the 41 opcodes are ever
   used in play (0 4 6 8 9 12 14 15 23 24 30 32 35 37 40). Together they decompress / de-interleave vertices, transform
   them, cull them and compact the indices. Write that as an SoA algorithm with SIMD over 4-8 vertices at a time.
   Lesson of 2026-11-24 (in `GEOMETRY_JOB.md`): 1:1 rewrites of leaf loops saved nothing. The gain has to come from
   doing less work.

Later steps, not asked yet: (4) renderer write tracking instead of `hash_range` / `hash_fp` (SPU DMA puts mark a
dirty-page bitmap, page protection for PPU writes) and merging branch `rsx-parse-thread`; (3) culling on the GPU with
static index buffers per mesh.

## Why (the numbers it rests on)

- Profile (60 fps + `F1CE_CAR_LOD=0`, PC): w1/w2 ~62% of SPU worker samples, w0 ~16%, audio ~10%.
- `F1CE_CPUS=4`: the workers are 70% in waits (line waits, the GETLLAR yield). The busy time goes to w0's main, the
  DMA copies (`write_ls`) and the dispatcher. GETLLARs are mostly atomic updates (480k/s, 20% of PUTLLCs fail).
- The generated native SPU code gave +15-20% through dispatch, not less work (SPU instructions per frame 108 M ->
  ~100 M).
- **Steam Deck, 2026-11-25** (release 0.6, 30 fps mode, 3 alternating pairs): native SPU code on vs off, grid
  28.0-28.7 vs 24.1-25.1 fps. The Deck is **power-limited at 15 W**: the CPU takes the budget and the GPU runs at
  ~420-600 MHz of 1600. APU power was 11.9-14.0 W with the native code on and 14.3-14.7 W (at the cap) with it off.
  So on the Deck, less CPU work means more watts for the GPU and more fps, even where the CPU does not look like the
  limit.

**Caveat on step 1 (Deck session's view):** the waits no longer spin, so they cost little CPU in themselves. And on
a 4-core Deck, a pool of "cores minus 2" threads is 2 threads, the same as w1 + w2 today. On the Deck, most of the gain
should come from step 2 (less work) and from dropping LS / DMA / atomics / the job manager. The extra parallelism of
step 1 matters more on 6+ cores. That is why the two go together.

## What is already known (pointers)

- The job: `GEOMETRY_JOB.md` "The job". It covers the command interpreter `0x5960`, the opcode handler table
  `0x12180`, the length table `0x12230`, the de-interleave table `0x12260`, the vertex format decoders
  `0x12A60-0x12BDF`, and the job manager services it calls (table at LS `0x12C00`).
- Generated native code to read as a specification: `generated/spu_geom/geom_native.cpp` (from
  `tools/spu_native_fn.py`). The job manager is in `generated/spu_geom/pm_native.cpp` and the hand-written natives
  (job end `0x1670`, buffer release `0x1968`, grab, DMA info) are in `runtime/spu_pm.cpp`.
- Native cull / transform loops already exist (bypassed in a check). Find them from `F1CE_GEOM_NATIVE` /
  `spu_fast_cmdlist` in `runtime/`.
- How the frame lists are built and consumed: `runtime/hle_joblist.cpp` header (`F1CE_JOBLIST_LOG=1`). PPU list
  builders `0x55165c` / `0x5516f0` (job), `0x551780` / `0x5517f8` (raw entry), `0x551878` (wait entry), `0x5515d0`
  (reserve skips), `0x5514a4` (atomic push), frame end w0 drain `0x489cb8`, previous-list wait `0x48aca8`, compaction
  `0x48acc8`.
- **Ordering, the main risk.** w1/w2 have an **in-order job type** (binary `0x100A1280`). Each such job waits until
  the first incomplete job is within 95 of its own index. It reads that from the per-SPU done windows at `0x10414F00`
  (base + 96-bit mask per SPU). Skip entries (kind 3) and wait entries are in the same queues. A w0 job appends
  in-order jobs to a frame list. The PPU compacts a list right after a wait (fn `0x48ac34`). Two old freezes came from
  exactly this (`PROGRESS.md` 2026-11-07, and the `Taken` comment in `spu_pm.cpp`). An HLE consumer has to keep the
  same guarantees: a job counts as done only when its outputs are in guest memory; the per-SPU halfwords / done
  windows the PPU and other jobs read must stay consistent; skips and wait entries must be honoured.
- The PPU waits read the per-SPU halfwords of the queue line, which the job manager rewrites from its job slots
  (`0x3260`, LS `0x1430 + s*32`). If w1/w2 no longer run on SPUs, either keep writing those halfwords from the HLE, or
  hook the waits (`jobwait_*` are already PROBES at the waits' returns) to ask the HLE pool.
- The SPURS side: `runtime/hle_spurs.cpp` (workers, ready counts, `g_worker_wid`). A workload's ready count must stay
  0 or the workers will still enter w1/w2.
- OPEN from 2026-11-25: an intermittent stall with the generated w0 + job manager images (2 in ~44 runs). If w1/w2 no
  longer go through the job manager it may go away or move. Keep the stall dump (`hle_gcm.cpp`, frames over 300 ms) on.

## Suggested way in

1. Measure first: with `F1CE_JOBLIST_LOG` and a counter, find out what the queues hold per frame. Count jobs, entry
   kinds and in-order jobs, and see which input/output buffers a geometry job DMAs (from `spu_pm.cpp` grab / DMA info).
   This gives the descriptor format the HLE must read.
2. Build the HLE consumer behind a switch (e.g. `F1CE_GEOM_HLE=1`, default off at first). For the job body, start
   with the existing generated code running on a private host LS image fed by plain memcpy instead of DMA. That
   separates "outside SPURS" (step 1) from "rewritten" (step 2) and gives a checkable middle step.
3. Then replace the job body opcode by opcode with the SoA / SIMD version. Check each against the generated path with
   the same input: compare output vertices / indices byte for byte, or with float tolerance where the order of
   operations changes. Extend `F1CE_GEOM_CHECK` for this.
4. Speed: PC with `F1CE_CPUS=4` (Deck-like) and 60 fps + `F1CE_CAR_LOD=0` (stress), alternating pairs, default vs
   the switch off. Watch worker CPU too, not only fps: on the PC the stress case is renderer-bound (rsx 0.92 core).
5. Scenario set before calling it done: the pre-merge list in `GEOMETRY_JOB.md` (long race, heavy rain Spain, Monaco
   Time Trial, pause -> replay, OpenGL race, pit stop, a 150 s race repeated for stalls).

## Steam Deck side

- `tools/deck_bench.sh` (committed with this note): scripted Quick Race on the Deck, AI driving, summary of the
  perf / cpu lines. It runs through `flatpak-spawn --host` from the VS Code flatpak. The Deck session will use it for
  the final A/B. A Linux build of the branch (the zig cross build from Windows, `linux/build_from_windows.sh`, or a
  test AppImage) is enough. The Deck can also build natively with gcc 16 in the flatpak SDK, but that has not been
  tried yet.
- Deck rules (user): 30 fps mode, never change the shadow update frequency, alternating pairs, report hardware and
  what was not tested, commit / push only when asked.
