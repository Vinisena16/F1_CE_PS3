"""F1 Championship Edition entities.bin: the drivers, teams, cars, tracks and countries database.

  python -I tools/entities.py list <entities.bin>                       sections and record ids
  python -I tools/entities.py export <entities.bin> <out.json>          editable JSON (names, texts, numbers)
  python -I tools/entities.py import <entities.bin> <edited.json> <out.bin>

entities.bin is in offline.wad and, with a few changed numbers, in update.wad (the game's patch).

Format (big endian): sections, each {u32 version, u32 record size, u32 count} then count records:
  record   <record size> bytes of fields, u32 pool size, pool (NUL-terminated strings), u32 string count, u32 0
  fields   u32 id hash, u32 0, u32 nationality hash (both: tools/wad.py name_hash of the lower-case text), then the
           string fields (byte offsets into the pool, -1 = none) and the numbers; the pool's first string is the id
  sections drivers (33 records, 168 bytes), teams (22, 148), cars (19, 312), tracks (19, 272), countries (27, 20)
String fields are recognised per section: the word positions that hold a pool string start (or -1) in every record.
Pool texts are UTF-8 (the game draws accented names such as Räikkönen from them).
Import rewrites the strings and numbers given in the JSON; the record ids (id, key) stay.
"""
import json
import struct
import sys
import zlib
from pathlib import Path

SECTION_NAMES = ['drivers', 'teams', 'cars', 'tracks', 'countries']

# names of known fields, by word index in the record
DRIVER_FIELDS = {
    2: ('c', 'nationality'),    # a countries record id (Spain, Britain...); the record stores its name hash
    3: 'first_name', 4: 'last_name', 5: 'short_name', 6: 'abbreviation', 7: 'display_name', 8: 'abbreviation2',
    9: 'birth_date', 10: 'birth_place', 11: 'debut', 12: 'first_win', 13: 'titles', 14: 'races', 15: 'best_grid',
    16: 'best_finish', 17: 'points', 18: 'folder', 19: 'key',
    25: ('i', 'helmet_model'), 26: ('i', 'helmet_vent'),   # Animations/Driver/Helmet/Helmet<n>.sg, Vent<n>.sg (-1 none)
    27: ('f', 'height_m'), 28: ('i', 'car_number'), 29: ('i', 'order'),
    # 0-100; the race copies them x 0.01 into the AI driver's skill block (0x104AD4: order 1 2 3 6 4 5, clamped to
    # 0-1 by 0x1072A4, which also forms six pair terms 1 - (a + b) x k; rating 4 is in four of them)
    30: ('f', 'rating1'), 31: ('f', 'rating2'), 32: ('f', 'rating3'), 33: ('f', 'rating4'), 34: ('f', 'rating5'),
    35: ('f', 'rating6'),
    # finishing positions the driver is expected to reach, best / typical / worst (Alonso 1 / 2.75 / 6, Monteiro
    # 14 / 17 / 22; read from the values, use not traced)
    36: ('f', 'position_best'), 37: ('f', 'position_typical'), 38: ('f', 'position_worst'),
    21: ('f', 'unknown21'), 22: ('f', 'unknown22'), 23: ('i', 'unknown23'), 24: ('i', 'unknown24'),
}
TEAM_FIELDS = {
    3: 'full_name', 4: 'name', 5: 'name_with_engine', 6: 'era', 7: 'driver1', 8: 'driver2', 9: 'test_drivers',
    10: 'base', 11: 'first_race', 12: 'first_win', 13: 'drivers_titles', 14: 'constructors_titles', 15: 'stat_a',
    16: 'stat_b', 17: 'points', 18: 'note', 19: 'folder', 20: 'key',
}
KNOWN = {'drivers': DRIVER_FIELDS, 'teams': TEAM_FIELDS}


def decode_text(b: bytes) -> str:
    return b.decode('utf-8')


def encode_text(s: str) -> bytes:
    return s.encode('utf-8')


class Record:
    def __init__(self, fields: bytes, pool: bytes, nstrings: int):
        self.words = list(struct.unpack(f'>{len(fields) // 4}I', fields))
        self.pool = pool
        self.nstrings = nstrings
        self.starts = {}            # pool offset -> string
        p = 0
        while p < len(pool):
            e = pool.index(b'\0', p)
            self.starts[p] = pool[p:e]
            p = e + 1

    @property
    def id(self):
        return decode_text(self.starts.get(0, b''))


class Section:
    def __init__(self, version, size, records):
        self.version, self.size, self.records = version, size, records
        n = size // 4
        # string fields: positions after the 3 header words where every record has -1 or a string start (and at
        # least one has a string)
        self.string_fields = [i for i in range(3, n)
                              if all(r.words[i] == 0xFFFFFFFF or (r.words[i] in r.starts and r.words[i] != 0)
                                     for r in records)
                              and any(r.words[i] != 0xFFFFFFFF for r in records)]


def parse(data: bytes):
    sections = []
    p = 0
    while p + 12 <= len(data):
        version, size, count = struct.unpack_from('>III', data, p)
        p += 12
        recs = []
        for _ in range(count):
            fields = data[p:p + size]
            n = struct.unpack_from('>I', data, p + size)[0]
            pool = data[p + size + 4:p + size + 4 + n]
            p += size + 4 + n
            nstr, zero = struct.unpack_from('>II', data, p)
            p += 8
            if zero != 0 or len(pool) != n or (n and pool[-1] != 0):
                raise ValueError(f'unexpected record layout at {p:#x}')
            recs.append(Record(fields, pool, nstr))
        sections.append(Section(version, size, recs))
    if p != len(data):
        raise ValueError(f'{len(data) - p} bytes left after the last section')
    return sections


def build(sections) -> bytes:
    out = bytearray()
    for s in sections:
        out += struct.pack('>III', s.version, s.size, len(s.records))
        for r in s.records:
            out += struct.pack(f'>{len(r.words)}I', *r.words)
            out += struct.pack('>I', len(r.pool)) + r.pool + struct.pack('>II', r.nstrings, 0)
    return bytes(out)


def section_name(i):
    return SECTION_NAMES[i] if i < len(SECTION_NAMES) else f'section{i}'


def field_name(sec_name, i):
    k = KNOWN.get(sec_name, {}).get(i)
    if isinstance(k, tuple):
        return k[1]
    return k or f'field{i}'


def name_hash(text):
    """tools/wad.py name_hash of a country id ('#xxxxxxxx' = a hash no country id matches)."""
    if len(text) == 9 and text[0] == '#':
        return int(text[1:], 16)
    return zlib.crc32(text.lower().encode('utf-8'), 0xFFFFFFFF)


def countries(sections):
    """Country name hash -> country id."""
    i = SECTION_NAMES.index('countries')
    return {name_hash(r.id): r.id for r in sections[i].records} if i < len(sections) else {}


def export(sections):
    out = {}
    cmap = countries(sections)
    for si, s in enumerate(sections):
        name = section_name(si)
        known = KNOWN.get(name, {})
        rows = []
        for r in s.records:
            row = {'id': r.id}
            for i in s.string_fields:
                w = r.words[i]
                row[field_name(name, i)] = None if w == 0xFFFFFFFF else decode_text(r.starts[w])
            for i, k in known.items():
                if isinstance(k, tuple):
                    v = r.words[i]
                    if k[0] == 'c':
                        row[k[1]] = cmap.get(v, f'#{v:08x}')
                        continue
                    row[k[1]] = round(struct.unpack('>f', struct.pack('>I', v))[0], 4) if k[0] == 'f' else \
                        (v - (1 << 32) if v & 0x80000000 else v)
            rows.append(row)
        out[name] = rows
    return out


def apply(sections, edits):
    """Set the string and number fields named in edits (export's layout; rows matched by id)."""
    changed = 0
    cmap = countries(sections)
    for si, s in enumerate(sections):
        name = section_name(si)
        rows = {row['id']: row for row in edits.get(name, [])}
        known = KNOWN.get(name, {})
        for r in s.records:
            row = rows.get(r.id)
            if row is None:
                continue
            strings = {}            # field index -> bytes or None
            for i in s.string_fields:
                w = r.words[i]
                strings[i] = None if w == 0xFFFFFFFF else r.starts[w]
            new_strings = dict(strings)
            for i in s.string_fields:
                fname = field_name(name, i)
                if fname in row and fname not in ('id', 'key'):
                    v = row[fname]
                    new_strings[i] = None if v is None else encode_text(v)
            if new_strings != strings:
                # rebuild the pool: the id first, then each string field in order (each field has its own string)
                pool = bytearray(r.starts[0] + b'\0')
                n = 1
                for i in s.string_fields:
                    v = new_strings[i]
                    if v is None:
                        r.words[i] = 0xFFFFFFFF
                    else:
                        r.words[i] = len(pool)
                        pool += v + b'\0'
                        n += 1
                r.pool, r.nstrings = bytes(pool), n
                changed += 1
            for i, k in known.items():
                if isinstance(k, tuple) and k[1] in row:
                    v = row[k[1]]
                    if k[0] == 'c':
                        nw = name_hash(v)
                        if nw not in cmap and not v.startswith('#'):
                            raise ValueError(f'{r.id}: {k[1]} "{v}" is not a country ({", ".join(cmap.values())})')
                        if nw != r.words[i]:
                            r.words[i] = nw
                            changed += 1
                        continue
                    nw = struct.unpack('>I', struct.pack('>f', float(v)))[0] if k[0] == 'f' else int(v) & 0xFFFFFFFF
                    if nw != r.words[i]:
                        r.words[i] = nw
                        changed += 1
    return changed


def main(argv):
    if len(argv) < 2:
        print(__doc__); return 1
    data = Path(argv[1]).read_bytes()
    sections = parse(data)
    if build(sections) != data:
        raise ValueError('rebuild check failed')
    if argv[0] == 'list':
        for si, s in enumerate(sections):
            print(f'{section_name(si)}: version {s.version}, {len(s.records)} records of {s.size} bytes, '
                  f'string fields {s.string_fields}')
            print('  ' + ' '.join(r.id for r in s.records))
    elif argv[0] == 'export' and len(argv) > 2:
        Path(argv[2]).write_text(json.dumps(export(sections), indent=1, ensure_ascii=False), encoding='utf-8')
    elif argv[0] == 'import' and len(argv) > 3:
        n = apply(sections, json.loads(Path(argv[2]).read_text(encoding='utf-8')))
        new = build(sections)
        parse(new)
        Path(argv[3]).write_bytes(new)
        print(f'{argv[3]}: {n} change(s)')
    else:
        print(__doc__); return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
