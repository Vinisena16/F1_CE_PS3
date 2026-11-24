"""Native C++ for whole call trees of the SPU geometry job (GEOMETRY_JOB.md).

Each SPU function reachable from the roots becomes `bool geomfn_XXXXX(SPUThread& s)`: the instruction handlers of
runtime/spu_ops.h with constant words (as tools/spu_aot.py emits them), direct branches as gotos and `brsl` as C++ calls,
so returns no longer go through the dispatcher. A function returns true at its `bi r0` (pc = the return address) and
false when it stops early with the pc set where the SPU code must continue: a callee that returned elsewhere, an
indirect branch to an unknown place, a channel instruction or `stop` that halted, interrupts enabled with an event
pending. All SPU state lives in the registers and the local store, so the dispatcher can always continue from there.
Native loops already in spu_ops.h are entered at their block heads (SKIPS), and the code continues where they leave.

Output: generated/spu_geom/geom_native.cpp with geom_native_handler(s, handler) for the command handlers in ROOTS.
usage: python -I tools/spu_native_fn.py
"""
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))
import spu  # noqa: E402
import spu_aot  # noqa: E402

GUEST, LS_BASE, SIZE = 0x100A1280, 0x4000, 0xEC20   # the job blob in the EBOOT's data segment
SEG_VADDR, SEG_OFF = 0x10040000, 0x750000
OUT = ROOT / "generated" / "spu_geom" / "geom_native.cpp"

CODE_END = 0x12180   # the handler table: code lies below it
# command handlers made native: handler entry (table 0x12180) -> opcode (the 15 ever recorded in play)
ROOTS = {0x08C80: 0, 0x07790: 4, 0x079B0: 6, 0x07888: 8, 0x0B300: 9, 0x07C50: 12, 0x07E40: 14, 0x07E70: 15,
         0x0B700: 23, 0x0B280: 24, 0x09FD8: 30, 0x0B980: 32, 0x0AFD0: 35, 0x05D30: 37, 0x06A30: 40}
# native loops entered at a block head: pc -> (helper, pcs where they leave)
NATIVE_AT = {0x0D7F8: ("spu_skip_triangle_cull", [0x0DDF0]),
             0x0D628: ("spu_skip_vertex_transform", [0x0D6D8])}
# indirect jumps with known targets (anything else stops there)
INDIRECT = {0x0D7F4: [0x0D7F8, 0x0DA18]}

BRANCH_DIRECT = {"br", "bra", "brz", "brnz", "brhz", "brhnz"}
CHANNEL = {"rdch", "wrch", "rchcnt", "stop", "stopd"}


def load_blob():
    d = (ROOT / "input" / "EBOOT.elf").read_bytes()
    o = SEG_OFF + GUEST - SEG_VADDR
    return d[o:o + SIZE]


def main():
    blob = load_blob()
    word = lambda pc: struct.unpack_from(">I", blob, pc - LS_BASE)[0]
    table = spu_aot.decode_table()

    def ops(pc):
        t = spu.disasm(word(pc), pc)
        return t, t.split()[0]

    def target(t):
        return int(t.split(",")[-1], 16)

    def scan(entry):
        """instructions of the function at entry (direct branches followed, stops after returns), its calls and the
        `ila` immediates that point into its code: targets of its indirect branches (the interrupt idiom
        `ila r2,next; bi r2 (d/e)`, loop variants chosen by selb)"""
        seen, todo, calls, ilas = set(), [entry], set(), set()
        while True:
            while todo:
                pc = todo.pop()
                while pc not in seen and LS_BASE <= pc < LS_BASE + SIZE:
                    seen.add(pc)
                    t, op = ops(pc)
                    if op == "ila":
                        v = int(t.split(",")[-1], 16)
                        if LS_BASE <= v < CODE_END and v % 4 == 0:
                            ilas.add(v)
                    if op in ("brsl", "brasl"):
                        calls.add(target(t))
                    elif op in ("br", "bra"):
                        todo.append(target(t)); break
                    elif op in ("brz", "brnz", "brhz", "brhnz"):
                        todo.append(target(t))
                    elif op in ("bi", "iret", "stop", "stopd"):
                        todo += INDIRECT.get(pc, []); break
                    if pc in NATIVE_AT:
                        todo += NATIVE_AT[pc][1]
                    pc += 4
            lo, hi = min(seen), max(seen) + 0x100
            more = [v for v in ilas if lo <= v < hi and v not in seen]
            if not more:
                return seen, calls, ilas
            todo += more

    funcs, callees, todo = {}, {}, list(ROOTS)
    while todo:
        f = todo.pop()
        if f in funcs:
            continue
        seen, calls, ilas = scan(f)
        funcs[f] = (seen, ilas)
        callees[f] = calls
        todo += sorted(calls)

    def tree(f):
        out, todo = set(), [f]
        while todo:
            g = todo.pop()
            if g not in out:
                out.add(g)
                todo += callees[g]
        return out

    # a check replays the handler on copies of the SPU: only for trees without DMA, channels or indirect calls (the job
    # manager's queue atomics and DMA must not run twice)
    def checkable(root):
        return not any(ops(pc)[1] in CHANNEL | {"bisl", "bisled", "iret"} for g in tree(root) for pc in funcs[g][0])

    out = ["// Generated by tools/spu_native_fn.py - do not edit.", "#define SPU_AOT_FAST", '#include "spu_ops.h"', "",
           "namespace {"]
    out += [f"bool geomfn_{f:05x}(SPUThread& s);" for f in sorted(funcs)]
    out += ["// an indirect call (bisl) to the pc: a generated function, else the SPU code through a nested run; true when it",
            "// returned to ret (the caller goes on natively)",
            "bool geom_call(SPUThread& s, u32 ret) {",
            "    switch (s.pc & 0x3FFFC) {"]
    out += [f"    case 0x{f:05X}: if (!geomfn_{f:05x}(s)) s.run_until(ret); break;" for f in sorted(funcs)]
    out += ["    default: s.run_until(ret); break;", "    }", "    return !s.halted && s.pc == ret;", "}"]
    for f in sorted(funcs):
        seen, ilas = funcs[f]
        pcs = sorted(seen)
        labels = {f} | ilas
        for pc in pcs:
            t, op = ops(pc)
            if op in BRANCH_DIRECT:
                labels.add(target(t))
            if op in ("brsl", "brasl"):
                labels.add(pc + 4)
            labels.update(NATIVE_AT.get(pc, ("", []))[1])
            labels.update(INDIRECT.get(pc, []))
        labels &= seen
        body = [f"bool geomfn_{f:05x}(SPUThread& s) {{", "    u32 to;", f"    goto L_{f:05x};",
                "jump:   // an indirect target or the pc a native loop left at",
                "    switch (to) {"]
        body += [f"    case 0x{pc:05X}: goto L_{pc:05x};" for pc in sorted(labels)]
        # a jump to the link register's address is a return (bi r10 with r10 = r0 ends the triangle cull)
        body += ["    default: s.pc = to; return to == (s.pref(0) & 0x3FFFC);", "    }"]
        prev = None
        for pc in pcs:
            if prev is not None and pc != prev + 4:
                body.append(f"    s.pc = 0x{(prev + 4) & 0x3FFFC:05X}; return false;   // (falls out of the scanned code)")
            prev = pc
            w = word(pc)
            t, op = ops(pc)
            name = table[w >> 21]
            if pc in labels:
                body.append(f"L_{pc:05x}:")
            if pc in NATIVE_AT:
                h, _ = NATIVE_AT[pc]
                # (not in an exact check: these loops only keep the registers and bytes later code reads)
                body.append(f"    s.pc = 0x{pc:05X}; if (!t_geom_exact && {h}(s)) {{ to = s.pc; goto jump; }}")
            c = f"   // {pc:05x} {t}"
            nxt = (pc + 4) & 0x3FFFC
            if name == "op_nop":
                continue
            if op in ("br", "bra"):
                body.append(f"    s.instructions++; goto L_{target(t):05x};{c}")
            elif op in ("brz", "brnz", "brhz", "brhnz"):
                rt = w & 0x7F
                cond = {"brz": f"s.pref({rt}) == 0", "brnz": f"s.pref({rt}) != 0",
                        "brhz": f"(s.pref({rt}) & 0xFFFF) == 0", "brhnz": f"(s.pref({rt}) & 0xFFFF) != 0"}[op]
                body.append(f"    s.instructions++; if ({cond}) goto L_{target(t):05x};{c}")
            elif op in ("brsl", "brasl"):
                rt = w & 0x7F
                body.append(f"    s.instructions++; s.set_pref({rt}, 0x{nxt:05X});{c}")
                body.append(f"    if (!geomfn_{target(t):05x}(s)) s.run_until(0x{nxt:05X});")
                body.append(f"    if (s.halted || s.pc != 0x{nxt:05X}) return false;")
            elif op in ("bi", "iret", "biz", "binz", "bihz", "bihnz", "bisl", "bisled"):
                # the handler sets the pc and the interrupt state; a return (bi r0) goes back to the caller
                rt, ra = w & 0x7F, (w >> 7) & 0x7F
                cond = {"biz": f"s.pref({rt}) == 0", "binz": f"s.pref({rt}) != 0",
                        "bihz": f"(s.pref({rt}) & 0xFFFF) == 0", "bihnz": f"(s.pref({rt}) & 0xFFFF) != 0"}.get(op)
                ind = "        " if cond else "    "
                body.append(f"    s.instructions++;{c}")
                if cond:
                    body.append(f"    if ({cond}) {{")
                body.append(f"{ind}s.pc = 0x{pc:05X}; {name}(s, 0x{w:08X}u);")
                body.append(f"{ind}if (s.interrupts && (s.event_stat & s.event_mask)) return false;")
                if op == "bisl":
                    body.append(f"{ind}if (!geom_call(s, 0x{nxt:05X})) return false;   // indirect call")
                elif op in ("bisled", "iret"):
                    body.append(f"{ind}return false;   // left to the dispatcher")
                elif ra == 0:
                    body.append(f"{ind}return true;")
                else:
                    body.append(f"{ind}to = s.pc; goto jump;")
                if cond:
                    body.append("    }")
            elif op in CHANNEL:
                body.append(f"    s.pc = 0x{pc:05X}; s.instructions++; {name}(s, 0x{w:08X}u);{c}")
                body.append(f"    if (s.halted) {{ s.pc = 0x{nxt:05X}; return false; }}")
            elif op in ("lqr", "stqr", "hbr", "hbra", "hbrr"):
                body.append(f"    s.pc = 0x{pc:05X}; s.instructions++; {name}(s, 0x{w:08X}u);{c}")
            else:
                body.append(f"    s.instructions++; {name}(s, 0x{w:08X}u);{c}")
        last_t, last_op = ops(pcs[-1])
        if last_op not in ("br", "bra", "bi", "iret", "stop", "stopd"):
            body.append(f"    s.pc = 0x{(pcs[-1] + 4) & 0x3FFFC:05X}; return false;")
        body.append("}")
        out += body
    out += [f"bool run_{h:05x}(SPUThread& s) {{ geomfn_{h:05x}(s); return true; }}   // (for the check)" for h in sorted(ROOTS)]
    out.append("}  // namespace")
    out.append("")
    out.append("// a command handler as native code: false when there is none (nothing changed); true when it ran, with the")
    out.append("// pc at the loop's 05c88 or where the SPU code continues the handler")
    out.append("bool geom_native_handler(SPUThread& s, u32 handler) {")
    out.append("    switch (handler) {")
    for h in sorted(ROOTS):
        out.append(f"    case 0x{h:05X}:   // op {ROOTS[h]}")
        if checkable(h):
            out.append(f"        if (__builtin_expect(g_geom_check != 0, 0)) spu_geom_check(\"op{ROOTS[h]}\", 0x{h:05X}, run_{h:05x}, s);")
        out.append(f"        geomfn_{h:05x}(s);")
        out.append("        return true;")
    out.append("    default: return false;")
    out.append("    }")
    out.append("}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(out) + "\n", newline="\n")
    n = sum(len(v[0]) for v in funcs.values())
    print(f"{len(funcs)} functions, {n} instructions -> {OUT.relative_to(ROOT)}; checked: "
          + " ".join(f"op{ROOTS[h]}" for h in sorted(ROOTS) if checkable(h)))


if __name__ == "__main__":
    main()
