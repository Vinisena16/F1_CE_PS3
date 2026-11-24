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
- [ ] Native handlers, by cost: 30, 0, 35, 9, 37, then the rest.
