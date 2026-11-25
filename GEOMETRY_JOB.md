# Native geometry job (branch `native-cpp-geometry-job`)

Goal: run the SPU geometry job of SPURS workloads w1/w2 as native C++ (user decision 2026-11-24, option A1), to cut the
race-start SPU load (most visible with `F1CE_CAR_LOD=0`).

## The job

- One raw SPU blob in the EBOOT data: guest `0x100A1280`, loaded at LS `0x4000`, `0xEC20` bytes (the size is a word of
  its header at LS `0x4014`; `0xC0DEC0DE` at `0x4020`). Code and data mixed, ~15k words, 31 direct call targets.
- Job manager services through a table whose LS address is stored at `0x12C00` (`0x1340` in the dumps): `+0x44` =
  `0x1670` (job end, native in `spu_pm.cpp`), `+0x48` = `0x1968` (releases buffers through `0x2CE8`, native), `+0x34` =
  `0x2D48` (DMA info, native), `+0x30` `0x1580`, `+0x38` `0x15F0`, `+0x3C` `0x1648`, `+0x40` `0x1650`, `+0x4C` `0x1988`,
  `+0x50` `0x1908`.
- Command interpreter: function `0x5960` (called from `0x4BBC`) saves r80-r99, sets constants (r84-r97 = small offsets,
  r98 = handler table `0x12180`, r99 = length table `0x12230`) and loops at `0x5A28`: it gathers the next 16 halfwords of
  the command stream (pointer kept at `[r82]`, `r82` = context + 352) into two quadwords on the stack (64(r1), 80(r1)) and
  packed into r4 / r5, the opcode is the low byte of the first halfword, `bisl` to the handler at `0x5C84` with r3 =
  context (r83), returns to `0x5C88`, which advances the stream by 2 * length bytes (length table) and loops back to
  `0x5A28` while the opcode was not 0 (`0x5CC4`: `brhz r80`, r80 = opcode == 0 from `0x5C58`): opcode 0 is the last
  command of every stream, run before the loop ends.
- 41 opcodes, handler table at LS `0x12180`; only 15 handlers were ever recorded in play: 0 4 6 8 9 12 14 15 23 24 30
  32 35 37 40.
- De-interleave routines per element type, table at LS `0x12260`: `0xBE00`, `0xBF80`, `0xC190` (x2), `0xC380`, `0xC180`
  (x2).

## Where the time goes (60 fps + LOD0 race start, `SPU_AOT_GEOM_OPS=1` build)

Handlers ~2.5 cores in all. Per mesh piece ops 0 4 6 8 9 12 14 30 35 37 run once (~110k/s); 23 24 32 40 once per ~11
pieces; 15 ~0.7 per piece.

| op | handler | ms/s | us/call | what |
|---|---|---|---|---|
| 30 | `0x9FD8` | ~615 | 5.5 | piece end: job manager `0x1968` (release) then `0x1670` (job end: outputs out, next job in) |
| 0 | `0x8C80` | ~465 | 4.2 | DMA streaming engine: ring space (`0x4C18`), DMA issue (`0x4368`, `wrch ch16..`), ... |
| 35 | `0xAFD0` | ~390 | 3.5 | |
| 9 | `0xB300` | ~365 | 3.3 | |
| 37 | `0x5D30` | ~165 | 1.5 | |
| others | | ~400 | | |

Host profile of the workers (same case): waits 19%, block dispatcher 13% (~65 M dispatches/s, mostly returns of the
job's compiled C functions), DMA path ~10%, native cull + transform 4.5%, translated code ~35% spread thin.

Lesson (2026-11-24): rewriting leaf loops instruction for instruction (`0xBD80`, `0xBE00`) was exact and saved nothing;
the gain must come from whole call trees running natively (no dispatcher round trips) and from doing less work.

## Design

1. Native `0x5960`: runs the command stream itself. At its loop head the SPU-visible state (stack frame and saved
   registers, r82 / r83, the operand copies on the stack, r84-r99) matches the SPU loop at `0x5A28`, so the native code
   can stop there at any time and leave the rest to the SPU code (`pc = 0x5A28`).
2. Opcodes with a native handler run inline; others call the SPU handler through a nested run until the return
   address `0x5C88` (`SPUThread::run_until`). If the SPU halts inside (blocking channel), the native loop gives up: the
   thread resumes in the handler and the SPU loop carries on.
3. Native code takes the common path only; anything unusual (an event pending where the SPU code enables interrupts,
   unexpected values, unknown opcodes) stops at a block boundary with the pc set.
4. Checking: `F1CE_GEOM_CHECK` (full state against the interpreter). Handlers that issue DMA need the dry-DMA mode of
   the job manager check (`spu_pm.cpp` check_fn: puts logged, not written) so a check never writes guest memory twice.
5. Speed: judge only with 60 fps + `F1CE_CAR_LOD=0`, alternating pairs (`F1CE_GEOM_NATIVE=0` vs default).

## Status

- [x] Job located, command set and per-command cost measured, checker (`F1CE_GEOM_CHECK`) and the per-command
  diagnostic (`SPU_AOT_GEOM_OPS=1`).
- [x] `SPUThread::run_until`, native command loop (`spu_fast_cmdlist`, skip at `0x5960`) calling the SPU handlers through
  nested runs: every list runs natively (~70-76k lists/s at the 30 fps race start, ~10.8 commands each), none handed
  back (`geom lists:` log line).
- [x] All 15 handlers as native call trees (`tools/spu_native_fn.py` -> `generated/spu_geom/geom_native.cpp`, 35
  functions, ~6100 instructions): exact where checkable (ops 6 8 12 14 15 23 35, 40k-507k calls each, 0 differences
  with `F1CE_GEOM_CHECK=2`; the cull / transform loops are bypassed in a check, they keep only live state); the others
  touch DMA or the job manager and are verified by game runs. Only stop: op30 at `0x0A088`, interrupts enabled right
  after job end with the job manager's own event pending (correct, the SPU code takes it). 60 fps + LOD0, vsync off,
  three alternating pairs: 30.2/31.7, 28.1/28.7, 27.4/28.6 fps = +4% (on better in every pair).
- [x] Targets of the job's function-pointer tables (de-interleave `0x12260`, vertex format decoders `0x12A60-0x12BDF`,
  `0x12780`) as native functions too, reached through indirect calls (`geom_call`), checked at their own entry: 82
  functions, ~12100 instructions; `0xBE00` 436k / `0xBF80` 46k checked calls, 0 differences. Same benchmark:
  30.3/32.9, 30.5/32.5, 30.1/31.1 fps = +6%.
- Host profile after it (workers, 60 fps + LOD0): dispatcher 9.1% (was 13.3%), `spu_fast_cmdlist` 4.4% (the operand
  gathering, 152 instructions per command), native cull 4.2%, DMA path ~10%, `geomfn_08970` 3.3%, `geomfn_08c80` 2.3%,
  `geomfn_0bd80` 1.4%, `geomfn_04c18` 1.3%. Translated code still running there: the job manager and workload w0's job.
- [x] Generated functions on local register copies (SpuLocal, 74 of 82; the 8 with channel instructions stay on the
  thread): exact, but no measurable change (30.4/31.2, 29.9/32.2, 30.4/32.1 fps = +5% against the SPU job, same as
  before within noise): register memory traffic is not what limits this code.
- [x] (2026-11-25) The same generator for workload w0's job and the job manager (`tools/spu_native_fn.py`, images
  GEOM / W0 / PM -> `generated/spu_geom/{geom,w0,pm}_native.cpp`).
  - w0's job: blob guest `0x10094700`, `0xA040` bytes at LS `0x4000` (the frame job list writer; a second small blob
    `0x10093700` alternates with it in w0). Position-independent: entry `0x4030` (header word `0x30`), constructor
    `0x41D8`, main `0x41F0` reached by `br` with r0 set by hand; `brsl rX,next` = pc into a register; interrupts
    off / on with `brsl rT,next; ai rT,rT,8; bi rT (d/e)`; main's switch at `0x5298` through the offset table at
    `0x52A0`; job manager services through the table pointer at `0xE020`. 9 functions, 8329 instructions.
  - Job manager: roots `0xA00` (main loop at `0x2A10`, continuations set by hand: `ila r0,next; br f`), event handler
    `0x21C0`, the services; the hand-written natives of `spu_pm.cpp` entered at their heads. 39 functions, 2510
    instructions. Its hot spots were already native (the DMA and atomics of `spu_pm.cpp`).
  - Entered by the dispatcher (`spu.cpp` `spu_native_entry`, when a block is built and the image's id words at its
    base match): the roots and resume points (return points of calls, targets of interrupt enables), 395 + 63. These
    images never nest runs: code that cannot finish natively (a call out of the image, a job launch, a halt, a pending
    interrupt) unwinds to the dispatcher, which comes back in at the next resume point (a nested run could hold the
    thread for good when the job manager leaves for the SPURS kernel). Every `bi` goes through the label switch;
    backward branches take pending interrupts. `F1CE_W0_NATIVE=0` / `F1CE_PM_GEN=0` switch them off,
    `F1CE_NATIVE_STATS=1` logs entries and early stops (costly), `F1CE_NATIVE_CHECK=1/2/3` compares entries with the
    interpreter on copies (every 4096th / 64th / every entry, DMA puts only logged).
  - Check: 37 w0 and 15 job manager entry points compared, no real difference (the differences seen are the queue line
    other SPUs change between the two runs, at LS `0x90`, and a decrementer read in the w0 entry).
  - Speed, 60 fps + LOD0, vsync off: off 29.2 / 27.9 / 29.5 / 28.5 fps, on 34.0 / 33.1 / 35.2 / 32.9 / 32.6 / 33.7 / 34.3 /
    34.1 fps = +15-20% (SPU instructions per frame 108 M -> ~100 M; the gain is dispatch, not work).
  - OPEN: an intermittent stall with both images on: 1 of 10 stress runs (on3, ~5 s at 1.5-17 fps) and 1 of 5 150 s
    races at 30 fps (`long30`, 15 s of 0.4-1.3 s frames ~105 s into the race; all workers idle, every ready count 0,
    wkl 6/7/8 queues holding jobs, main sleeping in fn 0x48AC34). Not seen in 7 runs with one image only or 5 with
    both off (too few to pin it on one image).
  - Stall hunt (a dump of the SPURS state on frames over 300 ms, `hle_gcm.cpp`): 0 stalls in 10 + 10 paired stress
    runs and 5 + 5 paired 150 s races at 30 fps. In all, 2 stalls in ~44 runs with the generated code, 0 in ~24 without.
- [x] Geometry job resume points (dispatcher entries, nesting kept; targets of `ila rT,next; bi rT (e/d)` too: 124
  entries) and indirect calls from job images straight into the job manager's generated code (`spu_native_call_pm`):
  dispatcher entries 1.8 M/s -> 1.3 M/s, no measurable fps change (33.6 / 33.9 / 33.6 against 29.2 / 29.1 / 29.0 off).
  With the generated code the stress test is renderer-bound now (rsx 0.92-0.93 core): further SPU savings show as
  lower CPU use, not fps, on this machine.
- Profile after (60 fps + LOD0): w1/w2 (geometry) ~62% of samples, w0 ~16%, w6/w7/w8 ~10% (the audio jobs, blob
  `0x10080880` `0x1C30` bytes, hot loop `0x5778` already the native idiom `spu_idiom_audio_voice`; code ~5.6%, job
  manager ~4.2%).
- Weak CPU benchmark (`F1CE_CPUS=4`, 60 fps, normal car detail, vsync off): all generated code off 23.4 / 22.9 fps,
  on 25.2 / 25.4 (+9.5%). Host profile there: the workers are 70% in waits (line waits, the GETLLAR yield); of the
  busy time, w0's job main ~16%, the DMA copies (`write_ls`) and the dispatcher, the rest spread thin. GETLLARs are
  mostly atomic updates (480k/s against 420k PUTLLCs, 20% of those fail), not idle polling.
- [x] The other job binaries of the EBOOT (`OTHER_JOBS`: 9 blobs, LS base from their recorded blocks; among them the
  audio jobs and the rain job) as generated images with dispatcher entries, the translated code's idioms and skips
  called where their words match (`F1CE_JOBS_GEN=0`). Checked (no real difference); 4 CPUs: 25.7 / 26.4 / 26.0 fps off,
  25.8 / 26.3 / 26.0 on = no change (their share was small).
- [x] The command loop's statistics (a counter shared by all workers on every command, a mutex on every early stop)
  only with `F1CE_NATIVE_STATS`: 26.1 / 26.0 / 26.1 fps, worker CPU 2.43 against ~2.48 cores.
- Renderer (60 fps normal, rsx 0.95 core): `draw()` 23% + 14% in the driver, `hash_range` 10%, FIFO parsing 9%, flip 5%
  (the index cache eviction walk); per frame vertices ~7 ms, textures 2.2, programs 1.3. Tried: a mixing hash with
  power-of-two buckets for the draw path's maps (their lookups were the hottest addresses of `draw()`): 56.4 fps both
  ways, reverted.
- [x] Renderer index lists (commit 2de4f10): timers in `gather_indices` showed the per-draw list path (lookup + hash
  1.2 ms, build 0.6 ms per frame) behind index regions that fall back for rewritten buffers; ~1350 of ~1700 rebuilt
  lists per frame are one-shot (geometry job output at new places). Keys seen for the first time skip the hash and
  the map; pointer caches in front of the maps. 56.4 / 57.0 / 57.4 -> 57.6 / 58.8 / 58.9 fps, rsx 0.95 -> 0.92, index
  ~2.4 -> ~1.7 ms, flip 5.1% -> 1.2% of the renderer (smaller map to evict).
- Renderer profile after (line tables via `-gline-tables-only` on rsx_vk / rsx_common / rsx.cpp, `llvm-symbolizer`):
  `draw()` 22%, FIFO parsing 10.6%, `hash_range` 9.3% (5.3% the sparse sampling loop: static mesh / texture checks),
  `hash_fp` 2.8% (fragment program hash every draw). Left: fewer sparse checks (a streamed mesh seen a frame later),
  FP hash per address (programs patched in place: needs write tracking), or FIFO parsing on its own thread (large).
  One GL boot run hit "RSX: unmapped IO offset 1f800000" (FIFO parse of float data, before any draw); the rerun and the
  baseline were fine: intermittent, see the FIFO stale tail notes.
- Pre-merge checks (2026-11-25): Linux cross build (zig) clean after fixing three generated functions that ran off
  their end after a trailing stop (commit 4c79354); test AppImage `out/F1CE-x86_64-native-test.AppImage`. Scenario set
  at 30 fps, no FATAL and no stall in any: 420 s race (Silverstone, Vulkan), heavy rain (Spain), Monaco Time Trial
  (AI drive, 240 s), Quick Race -> pause -> Replay (viewer works), OpenGL race (Monza), f1ce_online boot to its menus.
  Not covered: a LAN / online race, the PGO build (not used for releases).
- [ ] Semantic rewrites of the hottest generated functions (host profile names them), DMA-safe check mode for the
  handlers that issue DMA.
