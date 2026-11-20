"""GTF textures (PS3) <-> DDS, for editing the game's textures in GIMP / Paint.NET / Photoshop.

  python -I tools/gtf.py info <file.gtf>...
  python -I tools/gtf.py export <file.gtf> <out.dds>
  python -I tools/gtf.py import <original.gtf> <edited.dds> <out.gtf>
  python -I tools/gtf.py build <any.dds> <out.gtf> [like.gtf]    new GTF of the DDS's own size/format/mips
  python -I tools/gtf.py png <file.gtf> <out.png> [mip level]
  python -I tools/gtf.py frompng <in.png> <out.gtf> [like.gtf]    any size; format and mipmaps of like.gtf (DXT5)

GTF: u32 version (0x00090701), u32 data size, u32 texture count (always 1 in this game), then per texture
{u32 id, u32 data offset, u32 data size, CellGcmTexture: u8 format, u8 mip count, u8 dimension, u8 cubemap,
u32 remap, u16 width, u16 height, u16 depth, u8 location, u8 pad, u32 pitch, u32 offset}, big endian.
Formats used by the game: 0x86 DXT1, 0x87 DXT3, 0x88 DXT5 (block data as on PC), 0x85 A8R8G8B8 swizzled
(Z-order per mip level), 0xA5 = 0x85 | 0x20 linear. Mip levels follow each other; cube maps hold 6 faces, each
128-byte aligned.
Import keeps the original's format, size and mip count: the DDS must match them (save DXT1/3/5 with mipmaps as the
original had). Build makes a new header from the DDS instead (DXT1/3/5 or 32-bit BGRA, any size, any mip count),
taking the channel remap from like.gtf when given. Embedded copies (textures inside .sg models) start the data at
0x80; the header's size field is the data size rounded up to 128 bytes.
"""
import array
import struct
import sys
import zlib

LN = 0x20
FORMATS = {0x86: ('DXT1', 8), 0x87: ('DXT3', 16), 0x88: ('DXT5', 16), 0x85: ('A8R8G8B8', 4)}


class Gtf:
    def __init__(self, data: bytes):
        ver, size, n = struct.unpack_from('>III', data, 0)
        if ver >> 8 != 0x000907 or n < 1:
            raise ValueError('not a GTF texture')
        self.raw = bytearray(data)
        self.id, self.off, self.size = struct.unpack_from('>III', data, 12)
        (self.fmt, self.mips, self.dim, self.cube, self.remap, self.w, self.h, self.depth, self.loc, _pad,
         self.pitch, _o) = struct.unpack_from('>BBBBIHHHBBII', data, 24)
        self.base = self.fmt & ~(LN | 0x40)
        self.linear = bool(self.fmt & LN)
        if self.base not in FORMATS:
            raise ValueError(f'format {self.fmt:#x} not handled')

    @property
    def kind(self):
        return FORMATS[self.base][0]

    def level_sizes(self):
        name, bpb = FORMATS[self.base]
        w, h, out = self.w, self.h, []
        for _ in range(max(1, self.mips)):
            if name.startswith('DXT'):
                out.append((w, h, max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * bpb))
            else:
                out.append((w, h, w * h * 4))
            w, h = max(1, w // 2), max(1, h // 2)
        return out

    def faces(self):
        return 6 if self.cube else 1

    def face_size(self):
        return sum(s for _, _, s in self.level_sizes())

    def face_stride(self):
        n = self.face_size()
        return (n + 127) & ~127 if self.cube else n

    def data(self):
        return bytes(self.raw[self.off:self.off + self.size])


def morton_index(x, y, w, h):
    """Offset (in texels) of (x, y) in a Z-order swizzled w x h level (rectangles: the square part is swizzled,
    the longer side's high bits come on top, as on the RSX)."""
    i, bit, sx, sy = 0, 0, 1, 1
    while sx < w or sy < h:
        if sx < w:
            i |= (x & sx) and (1 << bit); bit += 1; sx <<= 1
        if sy < h:
            i |= (y & sy) and (1 << bit); bit += 1; sy <<= 1
    return i


def argb_level(src: bytes, w, h, swizzled, to_dds: bool):
    """A8R8G8B8 level, GTF byte order (A R G B, Z-order if swizzled) <-> DDS (B G R A, linear rows)."""
    out = bytearray(len(src))
    for y in range(h):
        for x in range(w):
            g = (morton_index(x, y, w, h) if swizzled else y * w + x) * 4
            d = (y * w + x) * 4
            if to_dds:
                a, r, gg, b = src[g:g + 4]
                out[d:d + 4] = bytes((b, gg, r, a))
            else:
                b, gg, r, a = src[d:d + 4]
                out[g:g + 4] = bytes((a, r, gg, b))
    return bytes(out)


DDSD_FLAGS = 0x1 | 0x2 | 0x4 | 0x1000      # caps, height, width, pixelformat
DDSD_MIPMAPCOUNT, DDSD_LINEARSIZE, DDSD_PITCH = 0x20000, 0x80000, 0x8


def dds_header(t: Gtf):
    flags = DDSD_FLAGS | (DDSD_MIPMAPCOUNT if t.mips > 1 else 0)
    caps, caps2 = 0x1000, 0
    if t.mips > 1:
        caps |= 0x400008
    if t.cube:
        caps |= 0x8
        caps2 = 0x200 | 0xFC00
    if t.kind.startswith('DXT'):
        flags |= DDSD_LINEARSIZE
        pf = struct.pack('<II4sIIIII', 32, 0x4, t.kind.encode(), 0, 0, 0, 0, 0)
        pitch = t.level_sizes()[0][2]
    else:
        flags |= DDSD_PITCH
        pf = struct.pack('<II4sIIIII', 32, 0x41, b'\0\0\0\0', 32, 0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
        pitch = t.w * 4
    hdr = struct.pack('<4sIIIIIII44s', b'DDS ', 124, flags, t.h, t.w, pitch, 0, max(1, t.mips), b'\0' * 44)
    return hdr + pf + struct.pack('<IIIII', caps, caps2, 0, 0, 0)


def to_dds(t: Gtf) -> bytes:
    src = t.data()
    out = bytearray(dds_header(t))
    for f in range(t.faces()):
        p = f * t.face_stride()
        for w, h, n in t.level_sizes():
            lvl = src[p:p + n]
            out += lvl if t.kind.startswith('DXT') else argb_level(lvl, w, h, not t.linear, True)
            p += n
    return bytes(out)


def from_dds(t: Gtf, dds: bytes) -> bytes:
    if dds[:4] != b'DDS ':
        raise ValueError('not a DDS file')
    h, w = struct.unpack_from('<II', dds, 12)
    mips = max(1, struct.unpack_from('<I', dds, 28)[0])
    fourcc = dds[84:88]
    want = t.kind.encode() if t.kind.startswith('DXT') else b'\0\0\0\0'
    problems = []
    if (w, h) != (t.w, t.h): problems.append(f'size {w}x{h}, original {t.w}x{t.h}')
    if mips != max(1, t.mips): problems.append(f'{mips} mip levels, original {max(1, t.mips)}')
    if fourcc != want: problems.append(f'format {fourcc!r}, original {t.kind}')
    if problems:
        raise ValueError('DDS does not match the original: ' + '; '.join(problems))
    p = 128
    data = bytearray(t.size)
    for f in range(t.faces()):
        q = f * t.face_stride()
        for lw, lh, n in t.level_sizes():
            lvl = dds[p:p + n]
            if len(lvl) != n:
                raise ValueError('DDS is truncated')
            data[q:q + n] = lvl if t.kind.startswith('DXT') else argb_level(lvl, lw, lh, not t.linear, False)
            p += n
            q += n
    out = bytearray(t.raw)
    out[t.off:t.off + t.size] = data
    return bytes(out)


def build_from_dds(dds: bytes, remap=0xAAE4) -> bytes:
    """A new GTF from any DXT1/DXT3/DXT5 or 32-bit A8R8G8B8 DDS (2D, any size and mip count): the free-size
    import. A8R8G8B8 is stored swizzled (0x85) as the game's own textures are. remap: the original's channel remap."""
    if dds[:4] != b'DDS ':
        raise ValueError('not a DDS file')
    h, w = struct.unpack_from('<II', dds, 12)
    mips = max(1, struct.unpack_from('<I', dds, 28)[0])
    if not struct.unpack_from('<I', dds, 8)[0] & DDSD_MIPMAPCOUNT:
        mips = 1
    pf_flags, fourcc, bits = struct.unpack_from('<I4sI', dds, 80)
    if struct.unpack_from('<I', dds, 112)[0] & 0x200:
        raise ValueError('cube map DDS not handled')
    if fourcc in (b'DXT1', b'DXT3', b'DXT5') and pf_flags & 0x4:
        fmt = {b'DXT1': 0x86, b'DXT3': 0x87, b'DXT5': 0x88}[fourcc]
    elif pf_flags & 0x40 and bits == 32 and struct.unpack_from('<4I', dds, 92) == \
            (0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000):
        fmt = 0x85
    else:
        raise ValueError('DDS must be DXT1, DXT3, DXT5 or 32-bit A8R8G8B8 (BGRA)')
    hdr = bytearray(b'P' * 0x80)       # the game's converter pads the header with 'P', the data with 0xEE
    struct.pack_into('>BBBBIHHHBBII', hdr, 24, fmt, mips, 2, 0, remap, w, h, 1, 0, 0, 0, 0)
    t = Gtf(struct.pack('>IIIIII', 0x00090701, 0, 1, 0, 0x80, 0) + bytes(hdr[24:]))
    levels = t.level_sizes()
    size = sum(n for _, _, n in levels)
    data = bytearray()
    p = 128
    for lw, lh, n in levels:
        lvl = dds[p:p + n]
        if len(lvl) != n:
            raise ValueError('DDS is truncated')
        data += lvl if fmt != 0x85 else argb_level(lvl, lw, lh, True, False)
        p += n
    padded = (size + 127) & ~127
    struct.pack_into('>IIIIII', hdr, 0, 0x00090701, padded, 1, 0, 0x80, size)
    return bytes(hdr) + bytes(data) + bytes([0xEE]) * (padded - size)


def _rgb565(c):
    r, g, b = c >> 11, (c >> 5) & 63, c & 31
    return ((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2))


def _dxt_colour_block(blk, out, w, x0, y0, h, alpha_opaque):
    c0, c1, bits = struct.unpack_from('<HHI', blk, 0)
    p0, p1 = _rgb565(c0), _rgb565(c1)
    if c0 > c1 or not alpha_opaque:
        pal = [p0 + (255,), p1 + (255,), tuple((2 * a + b) // 3 for a, b in zip(p0, p1)) + (255,),
               tuple((a + 2 * b) // 3 for a, b in zip(p0, p1)) + (255,)]
    else:
        pal = [p0 + (255,), p1 + (255,), tuple((a + b) // 2 for a, b in zip(p0, p1)) + (255,), (0, 0, 0, 0)]
    for i in range(16):
        x, y = x0 + (i & 3), y0 + (i >> 2)
        if x < w and y < h:
            out[(y * w + x) * 4:(y * w + x) * 4 + 4] = bytes(pal[(bits >> (2 * i)) & 3])


def decode_level(t: Gtf, level=0, face=0) -> tuple:
    """(w, h, RGBA bytes) of one mip level."""
    src = t.data()
    p = face * t.face_stride()
    sizes = t.level_sizes()
    for lw, lh, n in sizes[:level]:
        p += n
    w, h, n = sizes[level]
    lvl = src[p:p + n]
    out = bytearray(w * h * 4)
    if t.kind == 'A8R8G8B8':
        bgra = argb_level(lvl, w, h, not t.linear, True)
        for i in range(0, len(bgra), 4):
            out[i:i + 4] = bytes((bgra[i + 2], bgra[i + 1], bgra[i], bgra[i + 3]))
        return w, h, bytes(out)
    bpb = FORMATS[t.base][1]
    q = 0
    for by in range(0, h, 4):
        for bx in range(0, w, 4):
            blk = lvl[q:q + bpb]
            q += bpb
            if t.kind == 'DXT1':
                _dxt_colour_block(blk, out, w, bx, by, h, True)
                continue
            _dxt_colour_block(blk[8:], out, w, bx, by, h, False)
            if t.kind == 'DXT3':
                a = int.from_bytes(blk[:8], 'little')
                alpha = [((a >> (4 * i)) & 15) * 17 for i in range(16)]
            else:
                a0, a1 = blk[0], blk[1]
                if a0 > a1:
                    pal = [a0, a1] + [((7 - k) * a0 + k * a1) // 7 for k in range(1, 7)]
                else:
                    pal = [a0, a1] + [((5 - k) * a0 + k * a1) // 5 for k in range(1, 5)] + [0, 255]
                bits = int.from_bytes(blk[2:8], 'little')
                alpha = [pal[(bits >> (3 * i)) & 7] for i in range(16)]
            for i in range(16):
                x, y = bx + (i & 3), by + (i >> 2)
                if x < w and y < h:
                    out[(y * w + x) * 4 + 3] = alpha[i]
    return w, h, bytes(out)


def flip_rows(w, h, rgba: bytes) -> bytes:
    """Upside down (the game's textures are stored bottom row first)."""
    return b''.join(rgba[y * w * 4:(y + 1) * w * 4] for y in range(h - 1, -1, -1))


def write_png(path, w, h, rgba: bytes):
    rows = b''.join(b'\0' + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))

    def chunk(t, d):
        return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d))
    open(path, 'wb').write(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0)) +
                           chunk(b'IDAT', zlib.compress(rows, 6)) + chunk(b'IEND', b''))


def read_png(path) -> tuple:
    """(w, h, RGBA bytes) of an 8-bit RGB / RGBA / grey PNG (not interlaced, not palette)."""
    d = open(path, 'rb').read()
    if d[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('not a PNG file')
    p, idat = 8, b''
    while p < len(d):
        n, t = struct.unpack_from('>I4s', d, p)
        body = d[p + 8:p + 8 + n]
        if t == b'IHDR':
            w, h, depth, ctype, _, _, interlace = struct.unpack('>IIBBBBB', body)
        elif t == b'IDAT':
            idat += body
        p += 12 + n
    ch = {0: 1, 2: 3, 4: 2, 6: 4}.get(ctype)
    if depth != 8 or ch is None or interlace:
        raise ValueError('PNG must be 8-bit RGB, RGBA or grey, not interlaced')
    raw = zlib.decompress(idat)
    stride = w * ch
    full = (1 << (8 * stride)) - 1
    high = int.from_bytes(b'\x80' * stride, 'little')
    low = full ^ high

    def add(x, y):
        # byte-wise (x + y) & 255 of two rows held as integers: the low 7 bits add, the top bit is a carry-less xor
        return ((x & low) + (y & low)) ^ ((x ^ y) & high)
    prev = bytes(stride)
    rows = []
    for y in range(h):
        o = y * (stride + 1)
        f = raw[o]
        row = raw[o + 1:o + 1 + stride]
        if f == 1:
            # prefix sum per byte lane, ch bytes apart: doubling steps (Hillis-Steele) on the whole row
            s, k = int.from_bytes(row, 'little'), ch
            while k < stride:
                s = add(s, (s << (8 * k)) & full)
                k *= 2
            row = s.to_bytes(stride, 'little')
        elif f == 2:
            row = add(int.from_bytes(row, 'little'), int.from_bytes(prev, 'little')).to_bytes(stride, 'little')
        elif f in (3, 4):
            row = bytearray(row)
            for i in range(ch):
                row[i] = (row[i] + (prev[i] >> 1 if f == 3 else prev[i])) & 255
            if f == 3:
                for i in range(ch, stride):
                    row[i] = (row[i] + ((row[i - ch] + prev[i]) >> 1)) & 255
            else:
                for i in range(ch, stride):
                    a, b, c = row[i - ch], prev[i], prev[i - ch]
                    pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                    row[i] = (row[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
            row = bytes(row)
        rows.append(row)
        prev = row
    data = b''.join(rows)
    if ch == 4:
        return w, h, data
    out = bytearray(w * h * 4)
    if ch == 3:
        out[0::4], out[1::4], out[2::4] = data[0::3], data[1::3], data[2::3]
    else:
        out[0::4] = out[1::4] = out[2::4] = data[0::ch]
    out[3::4] = data[1::2] if ch == 2 else b'\xff' * (w * h)
    return w, h, bytes(out)


def _to565(c):
    return (min(31, (c[0] * 31 + 127) // 255) << 11) | (min(63, (c[1] * 63 + 127) // 255) << 5) | \
        min(31, (c[2] * 31 + 127) // 255)


def _encode_colour_block(px, punch):
    """DXT colour block (8 bytes) for 16 RGBA pixels: endpoints from the pixels' extremes along their main colour
    axis (power iteration on the covariance), indices by nearest palette colour. punch: DXT1 with transparent
    pixels (3-colour mode, index 3 = transparent)."""
    use = [p for p in px if p[3] >= 128] if punch else px
    if not use:
        return struct.pack('<HHI', 0, 0, 0xFFFFFFFF)
    n = len(use)
    mean = [sum(p[i] for p in use) / n for i in range(3)]
    cov = [[sum((p[i] - mean[i]) * (p[j] - mean[j]) for p in use) for j in range(3)] for i in range(3)]
    axis = [1.0, 1.0, 1.0]
    for _ in range(4):
        axis = [sum(cov[i][j] * axis[j] for j in range(3)) for i in range(3)]
        m = max(abs(a) for a in axis)
        if m < 1e-9:
            axis = [1.0, 1.0, 1.0]
            break
        axis = [a / m for a in axis]
    proj = [sum((p[i] - mean[i]) * axis[i] for i in range(3)) for p in use]
    hi, lo = use[proj.index(max(proj))], use[proj.index(min(proj))]
    c0, c1 = _to565(hi), _to565(lo)
    if (c0 < c1) != punch:
        c0, c1 = c1, c0
    if c0 == c1 and not punch:
        return struct.pack('<HHI', c0, c1, 0)
    e0, e1 = _rgb565(c0), _rgb565(c1)
    if punch:
        pal = [e0, e1, tuple((a + b) // 2 for a, b in zip(e0, e1))]
    else:
        pal = [e0, e1, tuple((2 * a + b) // 3 for a, b in zip(e0, e1)), tuple((a + 2 * b) // 3 for a, b in zip(e0, e1))]
    bits = 0
    for i, p in enumerate(px):
        if punch and p[3] < 128:
            k = 3
        else:
            r, g, b = p[0], p[1], p[2]
            d = [(r - q[0]) ** 2 + (g - q[1]) ** 2 + (b - q[2]) ** 2 for q in pal]
            k = d.index(min(d))
        bits |= k << (2 * i)
    return struct.pack('<HHI', c0, c1, bits)


def _encode_alpha_block(alphas):
    a0, a1 = max(alphas), min(alphas)
    if a0 == a1:
        return bytes((a0, a1)) + bytes(6)
    pal = [a0, a1] + [((7 - k) * a0 + k * a1) // 7 for k in range(1, 7)]
    bits = 0
    for i, a in enumerate(alphas):
        d = [abs(a - q) for q in pal]
        bits |= d.index(min(d)) << (3 * i)
    return bytes((a0, a1)) + bits.to_bytes(6, 'little')


def encode_level(w, h, rgba: bytes, kind: str) -> bytes:
    """One level as DXT1 / DXT3 / DXT5 blocks or A8R8G8B8 (swizzled, GTF byte order)."""
    if kind == 'A8R8G8B8':
        bgra = bytearray(len(rgba))
        for i in range(0, len(rgba), 4):
            bgra[i:i + 4] = bytes((rgba[i + 2], rgba[i + 1], rgba[i], rgba[i + 3]))
        return argb_level(bytes(bgra), w, h, True, False)
    out = bytearray()
    memo = {}           # 16 pixels -> their block: flat paint and repeated patterns are encoded once
    s = w * 4
    for by in range(0, h, 4):
        for bx in range(0, w, 4):
            o = (by * w + bx) * 4
            if bx + 4 <= w and by + 4 <= h:
                key = rgba[o:o + 16] + rgba[o + s:o + s + 16] + rgba[o + 2 * s:o + 2 * s + 16] + \
                    rgba[o + 3 * s:o + 3 * s + 16]
            else:
                key = b''.join(rgba[p:p + 4] for p in ((min(h - 1, by + (i >> 2)) * w + min(w - 1, bx + (i & 3))) * 4
                                                       for i in range(16)))
            blk = memo.get(key)
            if blk is not None:
                out += blk
                continue
            px = [tuple(key[i:i + 4]) for i in range(0, 64, 4)]
            start = len(out)
            if kind == 'DXT1':
                out += _encode_colour_block(px, any(p[3] < 128 for p in px))
            elif kind == 'DXT3':
                a = 0
                for i, p in enumerate(px):
                    a |= (p[3] >> 4) << (4 * i)
                out += a.to_bytes(8, 'little') + _encode_colour_block(px, False)
            else:
                out += _encode_alpha_block([p[3] for p in px]) + _encode_colour_block(px, False)
            memo[key] = bytes(out[start:])
    return bytes(out)


def gtf_from_rgba(w, h, rgba: bytes, like: Gtf = None, kind=None) -> bytes:
    """A GTF of any size from RGBA pixels, in the format (DXT1/3/5 or A8R8G8B8) and with the mipmapping of `like`
    (the texture it replaces) unless kind is given; the mip chain is rebuilt down to 1x1 (box filter)."""
    kind = kind or (like.kind if like else 'DXT5')
    levels = mip_chain(w, h, rgba) if (like is None or like.mips > 1) else [(w, h, rgba)]
    fmt = {'DXT1': 0x86, 'DXT3': 0x87, 'DXT5': 0x88, 'A8R8G8B8': 0x85}[kind]
    data = b''.join(encode_level(lw, lh, px, kind) for lw, lh, px in levels)
    size = len(data)
    padded = (size + 127) & ~127
    hdr = bytearray(b'P' * 0x80)
    struct.pack_into('>IIIIII', hdr, 0, 0x00090701, padded, 1, 0, 0x80, size)
    struct.pack_into('>BBBBIHHHBBII', hdr, 24, fmt, len(levels), 2, 0, like.remap if like else 0xAAE4, w, h, 1, 0, 0,
                     0, 0)
    return bytes(hdr) + data + bytes([0xEE]) * (padded - size)


def mip_chain(w, h, rgba: bytes):
    """[(w, h, RGBA)] from the full size down to 1x1, each level a 2x2 box filter of the one before."""
    levels = [(w, h, rgba)]
    while w > 1 or h > 1:
        nw, nh = max(1, w // 2), max(1, h // 2)
        src = array.array('I', levels[-1][2])         # one pixel per item
        rows = []
        for y in range(nh):
            y0, y1 = min(h - 1, 2 * y), min(h - 1, 2 * y + 1)
            r0, r1 = src[y0 * w:(y0 + 1) * w], src[y1 * w:(y1 + 1) * w]
            # even / odd columns (a 1 pixel wide level uses its one column twice)
            ev0, ev1 = r0[0:2 * nw:2].tobytes(), r1[0:2 * nw:2].tobytes()
            od0, od1 = (ev0, ev1) if w == 1 else (r0[1:2 * nw:2].tobytes(), r1[1:2 * nw:2].tobytes())
            rows.append(bytes((a + b + c + d + 2) >> 2 for a, b, c, d in zip(ev0, od0, ev1, od1)))
        w, h = nw, nh
        levels.append((w, h, b''.join(rows)))
    return levels


def dds_from_rgba(w, h, rgba: bytes, mips=True) -> bytes:
    """32-bit BGRA DDS with a box-filtered mip chain down to 1x1 (input for build_from_dds)."""
    levels = [(w, h, rgba)]
    while mips and (w > 1 or h > 1):
        nw, nh = max(1, w // 2), max(1, h // 2)
        src = levels[-1][2]
        dst = bytearray(nw * nh * 4)
        for y in range(nh):
            for x in range(nw):
                for c in range(4):
                    s = 0
                    for dy in (0, 1):
                        for dx in (0, 1):
                            sx, sy = min(w - 1, 2 * x + dx), min(h - 1, 2 * y + dy)
                            s += src[(sy * w + sx) * 4 + c]
                    dst[(y * nw + x) * 4 + c] = (s + 2) // 4
        w, h = nw, nh
        levels.append((w, h, bytes(dst)))
    w0, h0 = levels[0][0], levels[0][1]
    flags = DDSD_FLAGS | DDSD_PITCH | (DDSD_MIPMAPCOUNT if len(levels) > 1 else 0)
    hdr = struct.pack('<4sIIIIIII44s', b'DDS ', 124, flags, h0, w0, w0 * 4, 0, len(levels), b'\0' * 44)
    pf = struct.pack('<II4sIIIII', 32, 0x41, b'\0\0\0\0', 32, 0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
    caps = 0x1000 | (0x400008 if len(levels) > 1 else 0)
    out = bytearray(hdr + pf + struct.pack('<IIIII', caps, 0, 0, 0, 0))
    for lw, lh, px in levels:
        for i in range(0, len(px), 4):
            out += bytes((px[i + 2], px[i + 1], px[i], px[i + 3]))
    return bytes(out)


def main(argv):
    if not argv:
        print(__doc__); return 1
    cmd = argv[0]
    if cmd == 'info':
        for p in argv[1:]:
            t = Gtf(open(p, 'rb').read())
            print(f'{p}: {t.kind}{"" if t.linear or t.kind != "A8R8G8B8" else " swizzled"} {t.w}x{t.h} '
                  f'mips {t.mips}{" cube" if t.cube else ""} remap {t.remap:#x}')
    elif cmd == 'export':
        t = Gtf(open(argv[1], 'rb').read())
        open(argv[2], 'wb').write(to_dds(t))
    elif cmd == 'import':
        t = Gtf(open(argv[1], 'rb').read())
        open(argv[3], 'wb').write(from_dds(t, open(argv[2], 'rb').read()))
    elif cmd == 'png':
        t = Gtf(open(argv[1], 'rb').read())
        write_png(argv[2], *decode_level(t, int(argv[3]) if len(argv) > 3 else 0))
    elif cmd == 'frompng':
        like = Gtf(open(argv[3], 'rb').read()) if len(argv) > 3 else None
        open(argv[2], 'wb').write(gtf_from_rgba(*read_png(argv[1]), like))
    elif cmd == 'build':
        remap = Gtf(open(argv[3], 'rb').read()).remap if len(argv) > 3 else 0xAAE4
        open(argv[2], 'wb').write(build_from_dds(open(argv[1], 'rb').read(), remap))
    else:
        print(__doc__); return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
