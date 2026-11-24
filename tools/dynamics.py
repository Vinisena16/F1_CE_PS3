"""F1 Championship Edition dynamics.bin: per-team car performance (aero, engine, gearbox, reliability) and more.

  python -I tools/dynamics.py list <dynamics.bin>                       sections and record keys
  python -I tools/dynamics.py export <dynamics.bin> <out.json>          the identified per-team values as JSON
  python -I tools/dynamics.py import <dynamics.bin> <edited.json> <out.bin>

dynamics.bin is in offline.wad (read by 0x1A43C8, loaded once by 0x1A4824); update.wad's copy differs only in padding
bytes and update.wad is not opened.

Format (big endian): u32 30, then the record size of each of the 10 sections; then the sections, each
{u32 count, count x {u32 key, record}} (keys: tools/wad.py name_hash of a lower-case name). Records are floats;
some words are padding holding whatever memory the tool that wrote the file had (they differ between copies).
  section 0  0x80  1 record "offline"
  section 1  0x70  per team (the 11 of 2006, the 8 classics, "ai", 2 unnamed): aero (words 20, 24, 25, 26 differ
                   between teams; 24 rises from Ferrari 0.72 to Super Aguri 0.85: drag; 25 falls from Renault 0.50
                   to Super Aguri 0.39: downforce)
  section 2  0x90  per team: engine. Words 1-6 and 17 are engine speeds in rad/s (1916 = 18300 rpm; Toro Rosso's
                   restricted V10 1749 = 16700 rpm), 10-15 another speed table, 16 and 18 per-team values (18 from
                   Super Aguri 300 to Toro Rosso 340), 29-34 failure rates (McLaren 0.05 ... Super Aguri 0.15)
  section 3  0x50  per team: gearbox, words 2-8 the seven overall ratios (19.6 ... 6.8), 11-17 their inverses
  section 4  0x10, section 5 0x80 per team, the same for every 2006 team
  section 6  0x20  6 records, section 7 0x20 per track, section 8 0x10 per classic car, section 9 0xb0 18 records
  then more data read by 0x1A2BA4 / 0x1A2530 (not decoded)
Which record a car uses (0x10FC74, measured 2026-11-22 with F1CE_CAR_LOG): a car driven by a human uses its team's
records; a car driven by the AI uses its team's engine (2), gearbox (3) and drag (section 1 word 24, put back by
0x10926C) and the "ai" record for the rest of section 1 and sections 4, 5, 6, 8, 9.
Meanings from the code: section 1 word 19 = mass without fuel (the AI's fuel is not added), 24 = drag and 25 =
downforce (both x speed squared, 0x11CF6C), 26 = grip; section 2 word 16 = idle revs, 17 = rev limit, 18 = torque factor.
AI cars drive to a target speed: worse values slow them, better ones than the original change nothing on their own.
Import changes only the words named below.
"""
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import wad  # noqa: E402

NAMES = ['renault', 'mclaren', 'ferrari', 'toyota', 'williams', 'honda', 'redbull', 'bmw', 'midland', 'tororosso',
         'superaguri', 'lotus72e', 'cooperclimax', 'renaultrs01', 'hondara272', 'lotus49c', 'williamsfw18',
         'williamsfw06', 'alfa158', 'ai', 'offline',
         'imola', 'melbourne', 'bahrain', 'hockenheim', 'silverstone', 'shanghai', 'sepang', 'montreal', 'interlagos',
         'monza', 'istanbul', 'magnycours', 'hungaroring', 'suzuka', 'montecarlo', 'barcelona', 'indy', 'nurburgring',
         'jerez']
KEY_NAMES = {wad.name_hash(n): n for n in NAMES}

# per-team values: JSON group -> {field: (section, first word, word count)}
FIELDS = {
    'aero': {'drag': (1, 24, 1), 'downforce': (1, 25, 1), 'aero_w20': (1, 20, 1), 'aero_w26': (1, 26, 1)},
    'engine': {'speeds_rad_s': (2, 1, 6), 'max_speed_rad_s': (2, 17, 1), 'speed_table_rad_s': (2, 10, 6),
               'engine_w16': (2, 16, 1), 'engine_w18': (2, 18, 1), 'failure_rates': (2, 29, 6)},
    'gearbox': {'ratios': (3, 2, 7)},
}


def parse(data: bytes):
    """[{size, records: {key: bytearray}}] in file order, plus the undecoded tail."""
    head = struct.unpack_from('>11I', data, 0)
    p, sections = 44, []
    for size in head[1:]:
        (count,) = struct.unpack_from('>I', data, p)
        p += 4
        recs = {}
        for _ in range(count):
            (k,) = struct.unpack_from('>I', data, p)
            recs[k] = bytearray(data[p + 4:p + 4 + size])
            p += 4 + size
        sections.append({'size': size, 'records': recs})
    return {'head': head, 'sections': sections, 'tail': data[p:]}


def build(d) -> bytes:
    out = bytearray(struct.pack('>11I', *d['head']))
    for s in d['sections']:
        out += struct.pack('>I', len(s['records']))
        for k, rec in s['records'].items():
            out += struct.pack('>I', k) + rec
    return bytes(out + d['tail'])


def key_name(k):
    return KEY_NAMES.get(k, f'#{k:08X}')


def name_key(n):
    return int(n[1:], 16) if n.startswith('#') else wad.name_hash(n)


def export(d):
    teams = {}
    for k in d['sections'][1]['records']:
        t = {}
        for group, fields in FIELDS.items():
            g = t.setdefault(group, {})
            for name, (sec, w, n) in fields.items():
                rec = d['sections'][sec]['records'].get(k)
                if rec is None:
                    continue
                vals = [round(v, 6) for v in struct.unpack_from(f'>{n}f', rec, 4 * w)]
                g[name] = vals[0] if n == 1 else vals
        teams[key_name(k)] = t
    return {'teams': teams}


def apply(d, js) -> int:
    changed = 0
    for tname, t in js.get('teams', {}).items():
        k = name_key(tname)
        for group, fields in FIELDS.items():
            for name, (sec, w, n) in fields.items():
                rec = d['sections'][sec]['records'].get(k)
                v = t.get(group, {}).get(name)
                if rec is None or v is None:
                    continue
                vals = [v] if n == 1 else list(v)[:n]
                new = struct.pack(f'>{len(vals)}f', *vals)
                old = rec[4 * w:4 * w + len(new)]
                # unchanged values keep their exact bytes (JSON rounding)
                if [round(x, 6) for x in struct.unpack(f'>{len(vals)}f', old)] != [round(x, 6) for x in vals]:
                    rec[4 * w:4 * w + len(new)] = new
                    changed += 1
    return changed


def main(argv):
    if len(argv) < 2:
        print(__doc__); return 1
    d = parse(Path(argv[1]).read_bytes())
    if argv[0] == 'list':
        for i, s in enumerate(d['sections']):
            print(f'section {i} ({s["size"]:#x} bytes): ' + ' '.join(key_name(k) for k in s['records']))
        print(f'{len(d["tail"])} bytes not decoded after the sections')
    elif argv[0] == 'export' and len(argv) > 2:
        Path(argv[2]).write_text(json.dumps(export(d), indent=1), encoding='utf-8', newline='\n')
    elif argv[0] == 'import' and len(argv) > 3:
        n = apply(d, json.loads(Path(argv[2]).read_text(encoding='utf-8')))
        Path(argv[3]).write_bytes(build(d))
        print(f'{argv[3]}: {n} value(s) changed')
    else:
        print(__doc__); return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
