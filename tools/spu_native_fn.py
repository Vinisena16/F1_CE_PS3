"""Native C++ for whole call trees of SPU code: the geometry job (GEOMETRY_JOB.md), workload w0's job and the job
manager (SPURS policy module).

Each SPU function reachable from an image's roots becomes `bool <p>fn_XXXXX(SPUThread& s)`: the instruction handlers of
runtime/spu_ops.h with constant words (as tools/spu_aot.py emits them), direct branches as gotos and `brsl` as C++ calls,
so returns no longer go through the dispatcher. A function returns true at its return (`bi` of the link register: the
pc is the return address) and false when it stops early with the pc set where the SPU code must continue: a callee that
returned elsewhere, an indirect branch to an unknown place, a channel instruction or `stop` that halted, interrupts
enabled with an event pending. All SPU state lives in the registers and the local store, so the dispatcher can always
continue from there. Native loops already in spu_ops.h are entered at their block heads (native_at), and the code
continues where they leave.

Outputs (generated/spu_geom/):
  geom_native.cpp  geom_native_handler(s, handler) for the command handlers (called by the job's native command loop)
  w0_native.cpp, pm_native.cpp  g_spu_native_w0 / g_spu_native_pm: entry points for the dispatcher (runtime/spu.cpp
                   spu_native_entry): the roots and the resume points (return points of calls, targets of interrupt
                   enables), so code that stopped early (an interrupt taken, a halt) runs natively again from the next
                   such point. Each function can be entered at any of its resume points (`at`).
usage: python -I tools/spu_native_fn.py
"""
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))
import spu  # noqa: E402
import spu_aot  # noqa: E402

SEG_VADDR, SEG_OFF = 0x10040000, 0x750000   # the EBOOT's data segment


class Image:
    def __init__(self, name, guest, ls, size, code_end, roots, pointer_tables=(), native_at=None, indirect=None,
                 ila_targets=True, entries=False, header=None):
        self.name, self.guest, self.ls, self.size, self.code_end = name, guest, ls, size, code_end
        self.roots = roots                      # function entry -> comment
        self.pointer_tables = pointer_tables    # [(lo, hi)): words that are function entries (reached by bisl)
        self.native_at = native_at or {}        # pc -> (helper, pcs where it leaves)
        self.indirect = dict(indirect or {})    # pc of an indirect jump -> its known targets
        self.ila_targets = ila_targets          # `ila` immediates inside a function are labels (absolute code only)
        self.entries = entries                  # dispatcher entry points (else called by native code only)
        self.header = header                    # (ls, words): identifies the image in the local store


GEOM = Image(
    "geom", 0x100A1280, 0x4000, 0xEC20, 0x12180,   # the job blob; the handler table at 0x12180, code lies below it
    # command handlers (table 0x12180) -> opcode (the 15 ever recorded in play)
    {0x08C80: "op 0", 0x07790: "op 4", 0x079B0: "op 6", 0x07888: "op 8", 0x0B300: "op 9", 0x07C50: "op 12",
     0x07E40: "op 14", 0x07E70: "op 15", 0x0B700: "op 23", 0x0B280: "op 24", 0x09FD8: "op 30", 0x0B980: "op 32",
     0x0AFD0: "op 35", 0x05D30: "op 37", 0x06A30: "op 40"},
    # function-pointer tables of the job (indirect calls, bisl): de-interleave per element type (0x12260), vertex format
    # decoders (0x12A60-0x12B5F, 0x12B60-0x12BDF), one more at 0x12780
    pointer_tables=[(0x12260, 0x12280), (0x12780, 0x12784), (0x12A60, 0x12BE0)],
    native_at={0x0D7F8: ("spu_skip_triangle_cull", [0x0DDF0]),
               0x0D628: ("spu_skip_vertex_transform", [0x0D6D8])},
    indirect={0x0D7F4: [0x0D7F8, 0x0DA18]})

# Workload w0's job (writes the frame job lists, PROGRESS.md "job list race"): position-independent code, entry at 0x4030
# (header word 0x30), the constructor list at 0xE004 (0x41D8), main at 0x41F0 (reached by `br` with r0 set by hand). The
# job manager's services are called through the table pointer the entry saves at 0xE020.
W0 = Image("w0", 0x10094700, 0x4000, 0xA040, 0xE000, {0x04030: "job entry", 0x041D8: "constructor"},
           ila_targets=False, entries=True)

# The job manager (SPURS policy module, runtime/spu_pm.cpp): entry 0xA00 (main loop at 0x2A10), the event handler 0x21C0
# (called by the interrupt entry), the job services (table at 0x1340). Its native functions are entered at their heads.
PM = Image("pm", 0x100BE000, 0xA00, 0x2B80, 0x3580,
           {0x00A00: "entry", 0x021C0: "event handler", 0x01580: "service +0x30", 0x015F0: "service +0x38",
            0x01648: "service +0x3C", 0x01650: "service +0x40", 0x01670: "service +0x44 (job end)",
            0x01968: "service +0x48", 0x01988: "service +0x4C", 0x01908: "service +0x50",
            0x02D48: "service +0x34"},
           native_at={0x019F8: ("spu_skip_pm_buffers", []), 0x02D48: ("spu_skip_pm_dma_info", []),
                      0x02CE8: ("spu_skip_pm_release", []), 0x01670: ("spu_skip_pm_job_done", [0x018E0]),
                      0x02288: ("spu_skip_pm_grab", [])},
           entries=True)

BRANCH_DIRECT = {"br", "bra", "brz", "brnz", "brhz", "brhnz"}
CHANNEL = {"rdch", "wrch", "rchcnt", "stop", "stopd"}


def load_blob(img):
    d = (ROOT / "input" / "EBOOT.elf").read_bytes()
    o = SEG_OFF + img.guest - SEG_VADDR
    return d[o:o + img.size]


def generate(img):
    blob = load_blob(img)
    LS_BASE, SIZE, CODE_END = img.ls, img.size, img.code_end
    word = lambda pc: struct.unpack_from(">I", blob, pc - LS_BASE)[0]
    table = spu_aot.decode_table()
    p = img.name

    def ops(pc):
        t = spu.disasm(word(pc), pc)
        return t, t.split()[0]

    def target(t):
        return int(t.split(",")[-1], 16)

    def in_code(a):
        return LS_BASE <= a < CODE_END and a % 4 == 0

    def call_kind(pc, t):
        """brsl / brasl: 'pc' (the pc into a register: target = next instruction), 'call' (a function of the image),
        'out' (code outside the image)"""
        tg = target(t)
        if tg == pc + 4:
            return "pc"
        return "call" if in_code(tg) else "out"

    indirect = dict(img.indirect)

    def pic_jump(pc):
        """`brsl rT,next; ai rT,rT,k; bi rT` (position-independent code: interrupts off / on, then on at next + k)"""
        if pc - 8 < LS_BASE:
            return None
        w0, w1, w2 = word(pc - 8), word(pc - 4), word(pc)
        t0, o0 = ops(pc - 8)
        t1, o1 = ops(pc - 4)
        rt = w0 & 0x7F
        if o0 == "brsl" and target(t0) == pc - 4 and o1 == "ai" and (w1 & 0x7F) == rt and ((w1 >> 7) & 0x7F) == rt \
                and ((w2 >> 7) & 0x7F) == rt:
            k = (w1 >> 14) & 0x3FF
            k -= (k & 0x200) << 1
            return pc - 4 + k
        return None

    def scan(entry):
        """instructions of the function at entry (direct branches followed, stops after returns), its calls and the
        `ila` immediates that point into its code: targets of its indirect branches (the interrupt idiom
        `ila r2,next; bi r2 (d/e)`, loop variants chosen by selb)"""
        seen, todo, calls, ilas = set(), [entry], {}, set()
        while True:
            while todo:
                pc = todo.pop()
                while pc not in seen and LS_BASE <= pc < LS_BASE + SIZE:
                    seen.add(pc)
                    t, op = ops(pc)
                    if op == "ila" and img.ila_targets:
                        v = int(t.split(",")[-1], 16)
                        if in_code(v):
                            ilas.add(v)
                    if op in ("brsl", "brasl"):
                        if call_kind(pc, t) == "call":
                            calls.setdefault(target(t), set()).add(word(pc) & 0x7F)
                    elif op in ("br", "bra"):
                        todo.append(target(t)); break
                    elif op in ("brz", "brnz", "brhz", "brhnz"):
                        todo.append(target(t))
                    elif op in ("bi", "iret", "stop", "stopd"):
                        if op == "bi" and pc not in indirect:
                            tg = pic_jump(pc)
                            if tg is not None:
                                indirect[pc] = [tg]
                        todo += indirect.get(pc, []); break
                    if pc in img.native_at:
                        todo += img.native_at[pc][1]
                    pc += 4
            lo, hi = min(seen), max(seen) + 0x100
            more = [v for v in ilas if lo <= v < hi and v not in seen]
            if not more:
                return seen, calls, ilas
            todo += more

    extra = sorted({word(a) for lo, hi in img.pointer_tables for a in range(lo, hi, 4) if in_code(word(a))}
                   - set(img.roots))
    funcs, callees, todo = {}, {}, list(img.roots) + extra
    links = {f: {0} for f in todo}   # link registers a function is called with (roots and table targets: r0)
    while todo:
        f = todo.pop()
        if f in funcs:
            continue
        seen, calls, ilas = scan(f)
        funcs[f] = (seen, ilas)
        callees[f] = set(calls)
        for g, regs in calls.items():
            links.setdefault(g, set()).update(regs)
        todo += sorted(calls)

    def tree(f):
        out, todo = set(), [f]
        while todo:
            g = todo.pop()
            if g not in out:
                out.add(g)
                todo += callees[g]
        return out

    # a check replays a function on copies of the SPU: only for trees without DMA, channels, indirect calls or code
    # outside the image (the job manager's queue atomics and DMA must not run twice)
    def leaves(pc):
        t, op = ops(pc)
        return op in CHANNEL | {"bisl", "bisled", "iret"} or (op in ("brsl", "brasl") and call_kind(pc, t) == "out")

    checkable_memo = {}

    def checkable(root):
        if root not in checkable_memo:
            checkable_memo[root] = not any(leaves(pc) for g in tree(root) for pc in funcs[g][0])
        return checkable_memo[root]

    chk_call = "__builtin_expect(g_geom_check != 0, 0) && !t_geom_exact"
    out = [f"// Generated by tools/spu_native_fn.py ({p}) - do not edit.", "#define SPU_AOT_FAST", '#include "spu_ops.h"', "",
           "namespace {"]
    at = ", u32 at = 0" if img.entries else ""
    out += [f"bool {p}fn_{f:05x}(SPUThread& s{at});" for f in sorted(funcs)]
    out += [f"bool run_{f:05x}(SPUThread& s);" for f in sorted(funcs)]
    out += ["// an indirect call (bisl) to the pc: a generated function, else the SPU code through a nested run; true when it",
            "// returned to ret (the caller goes on natively)",
            f"bool {p}_call(SPUThread& s, u32 ret) {{",
            "    switch (s.pc & 0x3FFFC) {"]
    for f in sorted(funcs):
        chk = (f"if ({chk_call}) spu_geom_check(\"{p}{f:05x}\", 0x{f:05X}, run_{f:05x}, s); " if checkable(f) else "")
        out.append(f"    case 0x{f:05X}: {chk}if (!{p}fn_{f:05x}(s)) s.run_until(ret); break;")
    out += ["    default: s.run_until(ret); break;", "    }", "    return !s.halted && s.pc == ret;", "}"]
    resume = {}   # dispatcher entry pc -> function
    for f in sorted(funcs):
        seen, ilas = funcs[f]
        pcs = sorted(seen)
        labels = {f} | ilas
        used, written, sync = set(), set(), False
        for pc in pcs:
            t, op = ops(pc)
            if op in BRANCH_DIRECT:
                labels.add(target(t))
            if op in ("brsl", "brasl") and call_kind(pc, t) != "pc":
                labels.add(pc + 4)
                resume.setdefault(pc + 4, f)
            if op in ("bisl",):
                labels.add(pc + 4)
                resume.setdefault(pc + 4, f)
            labels.update(img.native_at.get(pc, ("", []))[1])
            labels.update(indirect.get(pc, []))
            if pc in indirect and pc not in img.indirect:   # interrupt enables / disables of position-independent code
                for tg in indirect[pc]:
                    resume.setdefault(tg, f)
            w = word(pc)
            r, wr = spu_aot.regs_of(w, table[w >> 21])
            used |= r | wr
            written |= wr
            sync |= op in CHANNEL
        labels &= seen
        if f in img.roots:
            resume[f] = f
        # local register copy (SpuLocal, as the translated clusters with loops do) unless the function has channel
        # instructions; the registers go back to the thread before anything outside runs and are read again after
        local = not sync
        x = "L" if local else "s"
        WB = (" ".join(f"s.gpr[{r}] = L.gpr[{r}];" for r in sorted(written)) + " ") if local else ""
        RL = (" " + " ".join(f"L.gpr[{r}] = s.gpr[{r}];" for r in sorted(used))) if local else ""
        lk = sorted(links.get(f, {0}))
        is_ret = " || ".join(f"to == (s.pref({r}) & 0x3FFFC)" for r in lk)
        body = [f"bool {p}fn_{f:05x}(SPUThread& s{', u32 at' if img.entries else ''}) {{", "    u32 to;"]
        if local:
            body.append(f"    SpuLocal L(s);{RL}")
        if img.entries:
            body.append("    if (at) { to = at; goto jump; }   // entered at a resume point")
        body += [f"    goto L_{f:05x};", "jump:   // an indirect target or the pc a native loop left at", "    switch (to) {"]
        body += [f"    case 0x{pc:05X}: goto L_{pc:05x};" for pc in sorted(labels)]
        # a jump to the link register's address is a return (bi r10 with r10 = r0 ends the triangle cull)
        body += [f"    default: {WB}s.pc = to; return {is_ret};", "    }"]
        prev = None
        for pc in pcs:
            if prev is not None and pc != prev + 4:
                body.append(f"    {WB}s.pc = 0x{(prev + 4) & 0x3FFFC:05X}; return false;   // (falls out of the scanned code)")
            prev = pc
            w = word(pc)
            t, op = ops(pc)
            name = table[w >> 21]
            if pc in labels:
                body.append(f"L_{pc:05x}:")
            if pc in img.native_at:
                h, _ = img.native_at[pc]
                # (not in an exact check: these loops only keep the registers and bytes later code reads; a loop that
                # declines changes nothing, so the local copy stays good)
                body.append(f"    {WB}s.pc = 0x{pc:05X}; if (!t_geom_exact && {h}(s)) {{{RL} to = s.pc; goto jump; }}")
            c = f"   // {pc:05x} {t}"
            nxt = (pc + 4) & 0x3FFFC
            if name == "op_nop":
                continue
            if op in ("br", "bra"):
                body.append(f"    s.instructions++; goto L_{target(t):05x};{c}")
            elif op in ("brz", "brnz", "brhz", "brhnz"):
                rt = w & 0x7F
                cond = {"brz": f"{x}.pref({rt}) == 0", "brnz": f"{x}.pref({rt}) != 0",
                        "brhz": f"({x}.pref({rt}) & 0xFFFF) == 0", "brhnz": f"({x}.pref({rt}) & 0xFFFF) != 0"}[op]
                body.append(f"    s.instructions++; if ({cond}) goto L_{target(t):05x};{c}")
            elif op in ("brsl", "brasl"):
                rt = w & 0x7F
                body.append(f"    s.instructions++; {x}.set_pref({rt}, 0x{nxt:05X});{c}")
                kind = call_kind(pc, t)
                if kind == "call":
                    g = target(t)
                    chk = (f"if ({chk_call}) spu_geom_check(\"{p}{g:05x}\", 0x{g:05X}, run_{g:05x}, s); "
                           if checkable(g) and img.entries else "")
                    body.append(f"    {WB}{chk}if (!{p}fn_{g:05x}(s)) s.run_until(0x{nxt:05X});")
                    body.append(f"    if (s.halted || s.pc != 0x{nxt:05X}) return false;{RL}")
                elif kind == "out":
                    body.append(f"    {WB}s.pc = 0x{target(t):05X}; s.run_until(0x{nxt:05X});   // outside the image")
                    body.append(f"    if (s.halted || s.pc != 0x{nxt:05X}) return false;{RL}")
                else:
                    body.append(f"    goto L_{nxt:05x};" if nxt in labels else "    // (the pc into a register)")
            elif op in ("bi", "iret", "biz", "binz", "bihz", "bihnz", "bisl", "bisled"):
                # the handler sets the pc and the interrupt state; a return (bi of the link register) goes back
                rt, ra = w & 0x7F, (w >> 7) & 0x7F
                cond = {"biz": f"{x}.pref({rt}) == 0", "binz": f"{x}.pref({rt}) != 0",
                        "bihz": f"({x}.pref({rt}) & 0xFFFF) == 0", "bihnz": f"({x}.pref({rt}) & 0xFFFF) != 0"}.get(op)
                ind = "        " if cond else "    "
                body.append(f"    s.instructions++;{c}")
                if cond:
                    body.append(f"    if ({cond}) {{")
                body.append(f"{ind}{x}.pc = 0x{pc:05X}; {name}({x}, 0x{w:08X}u);")
                body.append(f"{ind}if (s.interrupts && (s.event_stat & s.event_mask)) {{ {WB}s.pc = {x}.pc; return false; }}")
                if op == "bisl":
                    body.append(f"{ind}{WB}s.pc = {x}.pc; if (!{p}_call(s, 0x{nxt:05X})) return false;{RL}   // indirect call")
                elif op in ("bisled", "iret"):
                    body.append(f"{ind}{WB}s.pc = {x}.pc; return false;   // left to the dispatcher")
                elif ra in lk and pc not in indirect:
                    body.append(f"{ind}{WB}s.pc = {x}.pc; return true;")
                else:
                    body.append(f"{ind}to = {x}.pc; goto jump;")
                if cond:
                    body.append("    }")
            elif op in CHANNEL:
                body.append(f"    s.pc = 0x{pc:05X}; s.instructions++; {name}(s, 0x{w:08X}u);{c}")
                body.append(f"    if (s.halted) {{ s.pc = 0x{nxt:05X}; return false; }}")
            elif op in ("lqr", "stqr", "hbr", "hbra", "hbrr"):
                body.append(f"    {x}.pc = 0x{pc:05X}; s.instructions++; {name}({x}, 0x{w:08X}u);{c}")
            else:
                body.append(f"    s.instructions++; {name}({x}, 0x{w:08X}u);{c}")
        last_t, last_op = ops(pcs[-1])
        if last_op not in ("br", "bra", "bi", "iret", "stop", "stopd"):
            body.append(f"    {WB}s.pc = 0x{(pcs[-1] + 4) & 0x3FFFC:05X}; return false;")
        body.append("}")
        out += body
    out += [f"bool run_{h:05x}(SPUThread& s) {{ {p}fn_{h:05x}(s); return true; }}   // (for the check)" for h in sorted(funcs)]
    if img.entries:
        # a resume point in several functions (shared code): any of them, they run the same instructions from there
        resume = {pc: f for pc, f in resume.items() if pc in funcs[f][0]}
        for pc in sorted(resume):
            f = resume[pc]
            arg = "" if pc == f else f", 0x{pc:05X}"
            chk = (f"if ({chk_call}) spu_geom_check(\"{p}{f:05x}\", 0x{f:05X}, run_{f:05x}, s); "
                   if pc == f and checkable(f) else "")
            out.append(f"void entry_{pc:05x}(SPUThread& s) {{ {chk}{p}fn_{f:05x}(s{arg}); }}")
    out.append("}  // namespace")
    out.append("")
    if img.name == "geom":
        out.append("// a command handler as native code: false when there is none (nothing changed); true when it ran, with the")
        out.append("// pc at the loop's 05c88 or where the SPU code continues the handler")
        out.append("bool geom_native_handler(SPUThread& s, u32 handler) {")
        out.append("    switch (handler) {")
        for h in sorted(img.roots):
            out.append(f"    case 0x{h:05X}:   // {img.roots[h]}")
            if checkable(h):
                out.append(f"        if (__builtin_expect(g_geom_check != 0, 0)) spu_geom_check(\"{img.roots[h].replace(' ', '')}\", 0x{h:05X}, run_{h:05x}, s);")
            out.append(f"        geomfn_{h:05x}(s);")
            out.append("        return true;")
        out.append("    default: return false;")
        out.append("    }")
        out.append("}")
    else:
        hls, hn = img.header if img.header else (LS_BASE, 4)
        hw = [word(hls + 4 * i) for i in range(hn)]
        out.append(f"// dispatcher entry points (pc order): the roots and the resume points")
        out.append(f"static const SpuNativeEntry k_entries[] = {{")
        out += [f"    {{0x{pc:05X}, entry_{pc:05x}}},{'   // ' + img.roots[pc] if pc in img.roots else ''}" for pc in sorted(resume)]
        out.append("};")
        out.append(f"static const u32 k_header[] = {{{', '.join(f'0x{v:08X}' for v in hw)}}};   // LS 0x{hls:05X}: the image's id")
        out.append(f"extern const SpuNativeImage g_spu_native_{p} = {{\"{p}\", 0x{hls:05X}, k_header, {hn}, k_entries, "
                   f"{len(resume)}, 0x{LS_BASE:05X}, 0x{CODE_END:05X}}};")
    out_path = ROOT / "generated" / "spu_geom" / f"{p}_native.cpp"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(out) + "\n", newline="\n")
    n = sum(len(v[0]) for v in funcs.values())
    print(f"{p}: {len(funcs)} functions, {n} instructions, {len(resume)} entry points -> "
          f"{out_path.relative_to(ROOT)}; checked roots: "
          + " ".join(f"{h:05x}" for h in sorted(img.roots) if checkable(h)))


def main():
    for img in (GEOM, W0, PM):
        generate(img)


if __name__ == "__main__":
    main()
