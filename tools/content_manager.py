"""F1 CE Content Manager: season packs and player helmet packs, on top of tools/season_pack.py.

  python tools/content_manager.py [<launcher folder> [<pack> [<tab> [<team id>]]]]

The launcher folder holds MODS/ (season packs) and PLAYER_HELMETS/ (helmet packs) and f1ce_launcher.ini, whose
[game] path is the default game folder. Settings are kept in content_manager.ini in the launcher folder.
Pack contents (see season_pack.py): edits are saved to the pack's files (DATABASE/entities.json, dynamics.json,
points.txt, the PNGs); Build compiles them into the files the game loads. The original game files are only read.
Each picture says where the game uses it: in game (the race car in races and time trials, from offline.wad), the
garage / menu car (stream.wad), menus, or the podium (season_pack.used_in).
"""
import base64
import configparser
import copy
import json
import math
import os
import queue
import shutil
import struct
import subprocess
import sys
import threading
import tkinter as tk
import zlib
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, ttk

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import gtf  # noqa: E402
import season_pack  # noqa: E402

DRIVER_FIELDS = [('first_name', 'First name'), ('last_name', 'Last name'), ('short_name', 'Short name'),
                 ('display_name', 'Display name'), ('abbreviation', 'Abbreviation'), ('car_number', 'Car number'),
                 ('nationality', 'Nationality'),
                 ('helmet_model', 'Helmet shape (0-2)'), ('helmet_vent', 'Helmet vent (0-5, -1 none)'),
                 ('rating1', 'Rating 1'), ('rating2', 'Rating 2'), ('rating3', 'Rating 3'), ('rating4', 'Rating 4'),
                 ('rating5', 'Rating 5'), ('rating6', 'Rating 6'), ('position_best', 'Expected position: best'),
                 ('position_typical', 'Expected position: typical'), ('position_worst', 'Expected position: worst')]
# the driver profile in the menus (texts: "1 (x 8)" = best result 1st, reached 8 times, "-" = none)
DRIVER_STATS = [('birth_date', 'Birth date (dd/mm/yyyy)'), ('birth_place', 'Birth place'), ('height_m', 'Height (m)'),
                ('debut', 'Debut (year)'), ('first_win', 'First win (year, - none)'), ('titles', 'World titles'),
                ('races', 'Races'), ('points', 'Career points'), ('best_grid', 'Best grid: 1 (x 9) = 9 poles'),
                ('best_finish', 'Best finish: 1 (x 8) = 8 wins')]
TEAM_FIELDS = [('name', 'Name'), ('full_name', 'Full name'), ('name_with_engine', 'Name with engine'),
               ('driver1', 'Driver 1'), ('driver2', 'Driver 2'), ('nationality', 'Nationality'), ('base', 'Base')]
# the classic cars' own drivers stay (Extra Drivers tab)
CLASSIC_FIELDS = [f for f in TEAM_FIELDS if f[0] not in ('driver1', 'driver2')]
PERF_FIELDS = [('aero', 'drag', 'Drag'), ('aero', 'downforce', 'Downforce'), ('engine', 'engine_w18', 'Engine (w18)'),
               ('engine', 'max_speed_rad_s', 'Max engine speed (rad/s)'),
               ('engine', 'failure_rates', 'Failure rates'), ('gearbox', 'ratios', 'Gear ratios')]
EXTRA_DRIVERS = {'playerdriver': 'Player', 'scdriver': 'Safety car', 'ghostdriver': 'Ghost car'}
THUMB = 150
USED_LEGEND = ('Where the game uses each picture: "in game" = the car in races and time trials; "garage" = the car '
               'in the menus (driver select garage). The garage car shows the race car\'s base paint and has its own '
               'copy of the sponsor decals (Build updates both); LOD = low-detail versions for distant cars. Specular '
               '/ reflection map: where the paint shines (most 2006 cars have one on the LOD car only).')


def fmt(v):
    if isinstance(v, list):
        return ', '.join(fmt(x) for x in v)
    if isinstance(v, float):
        return f'{v:.6g}'
    return '' if v is None else str(v)


def parse_like(text, old):
    """The edited text as the type of the old value."""
    text = text.strip()
    if isinstance(old, list):
        return [float(x) for x in text.replace(',', ' ').split()]
    if isinstance(old, bool):
        return text.lower() in ('1', 'true', 'yes')
    if isinstance(old, int):
        return int(float(text))
    if isinstance(old, float):
        return float(text)
    return text if text or old is not None else None


def load_photo(path, size=THUMB):
    try:
        img = tk.PhotoImage(file=str(path))
    except (tk.TclError, OSError):
        return None
    k = max(1, -(-max(img.width(), img.height()) // size))
    return img.subsample(k, k) if k > 1 else img


def open_path(p):
    if sys.platform == 'win32':
        os.startfile(str(p))
    else:
        subprocess.Popen(['xdg-open', str(p)])


class ImageSlot(ttk.Frame):
    """A preview with Replace / Open buttons for one editable PNG of a pack."""

    def __init__(self, master, app, title, size=THUMB):
        super().__init__(master)
        self.app, self.path, self.photo, self.note, self.size = app, None, None, '', size
        ttk.Label(self, text=title, font=('Segoe UI', 9, 'bold')).pack(anchor='w')
        self.pic = ttk.Label(self, relief='sunken', anchor='center')
        self.pic.pack(fill='both', expand=True)
        self.info = ttk.Label(self, foreground='#555', wraplength=max(190, size + 40), justify='left')
        self.info.pack(anchor='w')
        row = ttk.Frame(self)
        row.pack(fill='x')
        ttk.Button(row, text='Replace...', command=self.replace).pack(side='left')
        ttk.Button(row, text='Export...', command=self.export).pack(side='left')
        ttk.Button(row, text='Open', command=lambda: self.path and open_path(self.path)).pack(side='left')

    def show(self, path, note=''):
        self.path, self.note = Path(path) if path else None, note
        exists = bool(self.path and self.path.exists())
        self.photo = load_photo(self.path, self.size) if exists else None
        self.pic.configure(image=self.photo or '', text='' if self.photo else '(none)')
        lines = []
        if self.path:
            lines.append(self.path.name if exists else f'{self.path.name}: not in this pack')
            used = self.app.used_in(self.path)
            if used:
                lines.append('Used: ' + ', '.join(season_pack.USED_IN_TEXT[u] for u in used))
        elif not note:
            lines.append('this car has none')
        if note:
            lines.append(note)
        self.info.configure(text='\n'.join(lines))

    def replace(self):
        if not self.path:
            return
        src = filedialog.askopenfilename(title=f'Replace {self.path.name}',
                                         filetypes=[('Images', '*.png *.dds'), ('All files', '*.*')])
        if not src:
            return
        src = Path(src)
        if src.suffix.lower() == '.dds' and self.app.file_info(self.path).get('alpha_of'):
            messagebox.showerror('Content Manager', f'{self.path.name} is a greyscale mask: replace it with a PNG')
            return
        if src.suffix.lower() == '.dds':      # used as it is (upside down, as the game stores it)
            shutil.copyfile(src, self.path.with_suffix('.dds'))
        else:
            shutil.copyfile(src, self.path)
            self.path.with_suffix('.dds').unlink(missing_ok=True)
        self.app.log(f'{self.path.relative_to(self.app.pack)}: replaced by {src.name} (Build to apply)')
        self.show(self.path, self.note)

    def export(self):
        """A copy of the picture (the DDS when one replaced the PNG) to edit elsewhere."""
        if not self.path:
            return
        src = self.path.with_suffix('.dds') if self.path.with_suffix('.dds').exists() else self.path
        if not src.exists():
            return
        dst = filedialog.asksaveasfilename(title=f'Export {src.name}', initialfile=src.name,
                                           defaultextension=src.suffix, filetypes=[('Images', '*' + src.suffix)])
        if dst:
            shutil.copyfile(src, dst)
            self.app.log(f'{src.relative_to(self.app.pack)}: exported to {dst}')


class Form(ttk.Frame):
    """Label + entry rows bound to a dict (the keys in choices get a drop-down list, see set_choices)."""

    def __init__(self, master, fields, choices=(), columns=1, width=44):
        super().__init__(master)
        self.fields, self.vars, self.data, self.combos, self.names = fields, {}, None, {}, {}
        rows = -(-len(fields) // columns)
        for i, (key, label) in enumerate(fields):
            r, c = i % rows, i // rows * 2
            ttk.Label(self, text=label).grid(row=r, column=c, sticky='w', padx=(16 if c else 0, 8), pady=1)
            v = tk.StringVar()
            if key in choices:
                w = self.combos[key] = ttk.Combobox(self, textvariable=v, width=width - 2, state='readonly')
            else:
                w = ttk.Entry(self, textvariable=v, width=width)
            w.grid(row=r, column=c + 1, sticky='we', pady=1)
            self.vars[key] = v
        for c in range(columns):
            self.columnconfigure(c * 2 + 1, weight=1)

    def set_choices(self, key, values, names=None):
        """names: value -> the text shown for it."""
        self.names[key] = names or {}
        self.combos[key].configure(values=[self.names[key].get(v, v) for v in values])

    def load(self, data, get=lambda d, k: d.get(k)):
        self.data = data
        for key, _ in self.fields:
            v = get(data, key) if data is not None else ''
            self.vars[key].set(self.names[key].get(v) or fmt(v) if key in self.names else fmt(v))

    def text(self, key):
        """The edited text (the value, for a name of set_choices)."""
        shown = self.vars[key].get()
        return next((v for v, name in self.names.get(key, {}).items() if name == shown), shown)

    def store(self, get=lambda d, k: d.get(k), put=lambda d, k, v: d.__setitem__(k, v)):
        if self.data is None:
            return 0
        n = 0
        for key, label in self.fields:
            old = get(self.data, key)
            if old is None and not self.text(key).strip():
                continue
            try:
                new = parse_like(self.text(key), old)
            except ValueError:
                raise ValueError(f'{label}: "{self.text(key)}" is not a valid value')
            if new != old and fmt(new) != fmt(old):
                put(self.data, key, new)
                n += 1
        return n


class Pane:
    """A tab with a list of database rows (teams or drivers) and the forms and pictures of the shown one."""

    def __init__(self, tab, lst):
        self.tab, self.list, self.rows, self.shown = tab, lst, [], None
        self.form = self.form2 = None       # form2: a team's dynamics.bin values, a driver's statistics
        self.imgs, self.ref = [], None


# Driver preview editor: the driver's menu pictures put together from layers (bottom first). The game shows these
# PNGs as they are (one texture each), so the layers are merged into DRIVERS/<id>/ui/<kind>.png on Save.
PREVIEW_KINDS = {'PreRace': ('Pre-race photo', (512, 512), [('background', 'Background'), ('driver', 'Driver'),
                                                            ('car', 'Car')]),
                 'DriverSelect': ('Driver select picture', (512, 256), [('flag', 'National flag'),
                                                                         ('driver', 'Driver')])}


def png_bytes(w, h, rgba):
    rows = b''.join(b'\0' + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))

    def chunk(t, d):
        return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress(rows, 1)) + chunk(b'IEND', b''))


def layer_rect(src, layer, z, w, h):
    """The part of the w x h picture (shown z times its size) a layer covers: (x0, y0, x1, y1), or None."""
    s = layer['scale'] * z
    x0, y0 = max(0, math.floor(layer['x'] * z)), max(0, math.floor(layer['y'] * z))
    x1 = min(round(w * z), math.ceil((layer['x'] + src[0] * layer['scale']) * z))
    y1 = min(round(h * z), math.ceil((layer['y'] + src[1] * layer['scale']) * z))
    return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 and s > 0 else None


def render_nearest(src, layer, z, w, h):
    """A layer's visible part for the preview, nearest pixel: (x0, y0, width, height, RGBA) or None."""
    r = layer_rect(src, layer, z, w, h)
    if not r:
        return None
    sw, sh, px = src
    x0, y0, x1, y1 = r
    s = layer['scale'] * z
    xs = [min(sw - 1, max(0, int((u + 0.5 - layer['x'] * z) / s))) * 4 for u in range(x0, x1)]
    rows, last, seg = [], None, b''
    for v in range(y0, y1):
        sy = min(sh - 1, max(0, int((v + 0.5 - layer['y'] * z) / s)))
        if sy != last:
            row = px[sy * sw * 4:(sy + 1) * sw * 4]
            seg, last = b''.join([row[x:x + 4] for x in xs]), sy
        rows.append(seg)
    return x0, y0, x1 - x0, y1 - y0, b''.join(rows)


def blend_layer(dst, src, layer, w, h):
    """A layer drawn over dst (w x h RGBA bytearray): bilinear with premultiplied alpha, from the mip level nearest
    above the layer's size (a smaller picture is not just sampled at a few pixels)."""
    r = layer_rect(src, layer, 1, w, h)
    if not r:
        return
    scale = layer['scale']
    if scale < 1:
        levels = gtf.mip_chain(*src)
        lv = min(len(levels) - 1, int(math.log2(1 / scale) + 1e-9))
        sw, sh, px = levels[lv]
        scale *= src[0] / sw
    else:
        sw, sh, px = src
    x0, y0, x1, y1 = r

    def taps(o, n, size, at):   # source pixel pairs and weights (0..256) of the output pixels o .. o + n
        out = []
        for u in range(o, o + n):
            f = min(size - 1.0, max(0.0, (u + 0.5 - at) / scale - 0.5))
            i = int(f)
            out.append((i, min(size - 1, i + 1), int((f - i) * 256)))
        return out
    cols, rows = taps(x0, x1 - x0, sw, layer['x']), taps(y0, y1 - y0, sh, layer['y'])
    for v, (ya, yb, wy) in zip(range(y0, y1), rows):
        ra, rb = ya * sw * 4, yb * sw * 4
        for u, (xa, xb, wx) in zip(range(x0, x1), cols):
            p00, p01, p10, p11 = ra + xa * 4, ra + xb * 4, rb + xa * 4, rb + xb * 4
            # weights x alpha of the four source pixels
            w00 = (256 - wx) * (256 - wy) * px[p00 + 3]
            w01 = wx * (256 - wy) * px[p01 + 3]
            w10 = (256 - wx) * wy * px[p10 + 3]
            w11 = wx * wy * px[p11 + 3]
            tw = w00 + w01 + w10 + w11
            if not tw:
                continue
            a = tw >> 16                                      # 0..255
            c = [(w00 * px[p00 + k] + w01 * px[p01 + k] + w10 * px[p10 + k] + w11 * px[p11 + k]) // tw
                 for k in range(3)]
            d = (v * w + u) * 4
            if a >= 255 or not dst[d + 3]:
                dst[d:d + 4] = bytes((c[0], c[1], c[2], min(255, a)))
                continue
            da, inv = dst[d + 3], 255 - a
            oa = a * 255 + da * inv
            for k in range(3):
                dst[d + k] = (c[k] * a * 255 + dst[d + k] * da * inv) // oa
            dst[d + 3] = oa // 255


# The pre-race photos share one backdrop: white fading into the team colour, a dot pattern and a checkered flag,
# opaque above a straight bottom edge. Per pixel it is S - U * (255 - colour) / 255 (S: brightness, U: how much
# team colour), found by fitting every original photo with the colour of its top right corner; the median over the
# photos drops the drivers and cars, and the place every photo covers (behind the driver) is filled smoothly.
BACKDROP_CACHE = 'content_manager_backdrop'     # .png (S, U, known, alpha) and .json (original colours), launcher folder


def corner_colour(w, px):
    """Average colour of the top right 24 x 24 pixels (only backdrop in every original photo)."""
    cs = [0, 0, 0]
    for y in range(24):
        for x in range(w - 24, w):
            for k in range(3):
                cs[k] += px[(y * w + x) * 4 + k]
    return tuple(round(v / 576) for v in cs)


def push_pull(vals, known, w, h):
    """vals with the unknown pixels (known 0) filled from coarser averages of the known ones."""
    if w <= 1 and h <= 1:
        return vals
    nw, nh = (w + 1) // 2, (h + 1) // 2
    cv, ck = [0.0] * (nw * nh), [0.0] * (nw * nh)
    for y in range(h):
        for x in range(w):
            k = known[y * w + x]
            if k:
                j = (y // 2) * nw + x // 2
                cv[j] += vals[y * w + x] * k
                ck[j] += k
    for j in range(nw * nh):
        if ck[j]:
            cv[j] /= ck[j]
            ck[j] = min(1.0, ck[j])
    cv = push_pull(cv, ck, nw, nh)
    out = list(vals)
    for y in range(h):
        fy = min(nh - 1.0, max(0.0, (y - 0.5) / 2))
        y0 = int(fy)
        y1, ay = min(nh - 1, y0 + 1), fy - y0
        for x in range(w):
            k = known[y * w + x]
            if k < 1:
                fx = min(nw - 1.0, max(0.0, (x - 0.5) / 2))
                x0 = int(fx)
                x1, ax = min(nw - 1, x0 + 1), fx - x0
                c = ((cv[y0 * nw + x0] * (1 - ax) + cv[y0 * nw + x1] * ax) * (1 - ay) +
                     (cv[y1 * nw + x0] * (1 - ax) + cv[y1 * nw + x1] * ax) * ay)
                out[y * w + x] = vals[y * w + x] * k + c * (1 - k)
    return out


def make_backdrop(pictures, w, h):
    """(S, U, known, alpha) bytes of the shared backdrop from the original photos [(RGBA, corner colour)]."""
    good = [(px, [255 - x for x in c]) for px, c in pictures if sum((255 - x) ** 2 for x in c) > 900]   # not white
    if len(good) < 4:
        raise ValueError('too few pre-race photos with a team colour')
    sv, uv, known = [0.0] * (w * h), [0.0] * (w * h), [0.0] * (w * h)
    for p in range(w * h):
        i = p * 4
        ss, us = [], []
        for px, d in good:
            # P_k = s*255 - s*t*255 * d_k/255 for the 3 channels: least squares for a = s*255, b = s*t
            sd, dd = d[0] + d[1] + d[2], d[0] * d[0] + d[1] * d[1] + d[2] * d[2]
            sp, spd = px[i] + px[i + 1] + px[i + 2], px[i] * d[0] + px[i + 1] * d[1] + px[i + 2] * d[2]
            det = 3 * dd - sd * sd
            if det < 1:
                continue
            b = (sp * sd - 3 * spd) / det
            ss.append(min(255.0, max(0.0, (sp + b * sd) / 3)))
            us.append(min(255.0, max(0.0, b * 255)))
        n = len(ss)
        if not n:
            continue
        ss.sort()
        us.sort()
        sv[p], uv[p] = ss[n // 2], us[n // 2]
        # the same in most photos and light (the tyres are black in every one)
        known[p] = 1.0 if max(ss[3 * n // 4] - ss[n // 4], us[3 * n // 4] - us[n // 4]) < 20 and sv[p] >= 150 else 0.0
    for _ in range(2):          # not the drivers' edges
        grown = list(known)
        for p in range(w * h):
            if not known[p]:
                x, y = p % w, p // w
                for q in (p - 1 if x else p, p + 1 if x < w - 1 else p, p - w if y else p, p + w if y < h - 1 else p):
                    grown[q] = 0.0
        known = grown
    sv, uv = push_pull(sv, known, w, h), push_pull(uv, known, w, h)
    # the left edge is never covered: its alpha per row is the backdrop's straight bottom edge
    rows = [sorted(px[(y * w + 40) * 4 + 3] for px, _ in pictures)[len(pictures) // 2] for y in range(h)]
    return (bytes(round(v) for v in sv), bytes(round(v) for v in uv), bytes(255 if k else 0 for k in known),
            b''.join(bytes((a,)) * w for a in rows))


def backdrop_rgba(backdrop, colour):
    """The backdrop in a colour: RGBA bytes."""
    s, u, a = backdrop
    out = bytearray(len(s) * 4)
    for k in range(3):
        table = bytes(v * (255 - colour[k]) // 255 for v in range(256))
        out[k::4] = bytes(map(int.__sub__, s, u.translate(table)))
    out[3::4] = a
    return bytes(out)


def hex_colour(c):
    return '#%02x%02x%02x' % tuple(c)


def parse_colour(text):
    text = str(text or '').lstrip('#')
    return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4)) if len(text) == 6 else None


class PreviewEditor(ttk.Frame):
    """The Driver preview editor tab: a driver's pre-race photo and driver select picture made of PNG layers. The
    layer files and their places are kept in DRIVERS/<id>/ui/preview/ (the build ignores that folder), so a
    picture can be edited again later."""

    def __init__(self, master, app):
        super().__init__(master, padding=6)
        self.app = app
        self.rows, self.shown, self.layers, self.cache, self.photos, self.items = [], None, {}, {}, [], {}
        self.current_photo = None
        self.backdrop, self.orig_colours, self.backdrop_note = None, {}, ''   # see load_backdrop
        self.kind = tk.StringVar(value='PreRace')
        self.sel = tk.StringVar()
        self.view = (1.0, 0, 0)          # preview zoom and the picture's corner on the canvas
        self.drag, self.render_due, self.quiet = None, False, False

        left = ttk.Frame(self)
        left.pack(side='left', fill='y')
        ttk.Label(left, text='Drivers with menu pictures', foreground='#555').pack(anchor='w')
        self.list = tk.Listbox(left, width=26, exportselection=False)
        self.list.pack(fill='both', expand=True)
        self.list.bind('<<ListboxSelect>>', lambda e: self.show())

        right = ttk.Frame(self, padding=(10, 0))
        right.pack(side='left', fill='both', expand=True)
        top = ttk.Frame(right)
        top.pack(fill='x')
        for kind, (title, size, _) in PREVIEW_KINDS.items():
            ttk.Radiobutton(top, text=f'{title} ({kind}.png, {size[0]} x {size[1]})', value=kind, variable=self.kind,
                            command=self.show).pack(side='left', padx=(0, 12))
        ttk.Label(right, wraplength=760, justify='left', foreground='#555',
                  text='Layers from the bottom up; PNGs with transparency (cut-outs) for the layers above the first. '
                       'Drag in the preview to move the selected layer, mouse wheel to resize it. Save merges the '
                       'layers into the driver\'s picture (Build to apply). The checked squares are transparent.'
                  ).pack(anchor='w', pady=(4, 0))

        body = ttk.Frame(right)
        body.pack(fill='both', expand=True, pady=4)
        side = ttk.Frame(body)
        side.pack(side='right', fill='y', padx=(10, 0))
        self.layer_box = ttk.LabelFrame(side, text='Layers (top first)', padding=6)
        self.layer_box.pack(fill='x')
        box = ttk.LabelFrame(side, text='Selected layer', padding=6)
        box.pack(fill='x', pady=(6, 0))
        self.vars = {}
        for r, (key, label) in enumerate((('x', 'X (pixels)'), ('y', 'Y (pixels)'), ('scale', 'Size (%)'))):
            ttk.Label(box, text=label).grid(row=r, column=0, sticky='w')
            v = self.vars[key] = tk.StringVar()
            sp = ttk.Spinbox(box, textvariable=v, width=8, from_=-4000 if key != 'scale' else 1, to=4000,
                             increment=1 if key != 'scale' else 0.5, command=self.typed)
            sp.grid(row=r, column=1, sticky='w', pady=1)
            sp.bind('<Return>', lambda e: self.typed())
            sp.bind('<FocusOut>', lambda e: self.typed())
        row = ttk.Frame(box)
        row.grid(row=3, column=0, columnspan=2, sticky='w', pady=(4, 0))
        for text, cmd in (('Fit height', lambda: self.fit('h')), ('Fit width', lambda: self.fit('w')),
                          ('Centre', lambda: self.fit('c')), ('100%', lambda: self.fit('1'))):
            ttk.Button(row, text=text, width=9, command=cmd).pack(side='left')
        row = ttk.Frame(side)
        row.pack(fill='x', pady=(6, 0))
        cur = ttk.LabelFrame(row, text='In the pack now', padding=4)
        cur.pack(side='left')
        self.current = ttk.Label(cur, anchor='center')
        self.current.pack()
        btns = ttk.Frame(row)
        btns.pack(side='left', anchor='n', padx=(8, 0), pady=(8, 0))
        ttk.Button(btns, text='Save picture', command=self.save).pack(fill='x')
        ttk.Button(btns, text='Export...', command=self.export).pack(fill='x')
        ttk.Button(btns, text='Open folder', command=self.open_folder).pack(fill='x')

        self.canvas = tk.Canvas(body, background='#9a9a9a', highlightthickness=0)
        self.canvas.pack(side='left', fill='both', expand=True)
        self.canvas.bind('<Configure>', lambda e: self.redraw())
        self.canvas.bind('<ButtonPress-1>', self.press)
        self.canvas.bind('<B1-Motion>', self.motion)
        self.canvas.bind('<ButtonRelease-1>', lambda e: self.release())
        self.canvas.bind('<MouseWheel>', self.wheel)
        self.canvas.bind('<Button-4>', lambda e: self.wheel(e, 1))     # X11
        self.canvas.bind('<Button-5>', lambda e: self.wheel(e, -1))
        self.bind('<Map>', lambda e: self.show())       # a nationality changed in the Drivers tab: its flag

    # ------------------------------------------------------------------ drivers and layer files
    def fill(self):
        """The drivers of the pack that have menu pictures (keeps the shown one)."""
        app = self.app
        names = {d['id']: f'{d.get("display_name") or d["id"]}' for d in app.drivers}
        shown = self.shown['id'] if self.shown else None
        self.rows = [d for d in app.drivers if (app.pack / 'DRIVERS' / d['id'] / 'ui').is_dir()]
        self.list.delete(0, 'end')
        for n, d in enumerate(self.rows):
            self.list.insert('end', f'{names[d["id"]]}  ({d["id"]})')
            if d['id'] == shown:
                self.list.selection_set(n)
        if self.rows and not self.list.curselection():
            self.list.selection_set(0)
        self.shown = None
        self.show()

    def ui_dir(self):
        return self.app.pack / 'DRIVERS' / self.shown['id'] / 'ui'

    def project_path(self):
        return self.ui_dir() / 'preview' / f'{self.kind.get()}.json'

    def flag_file(self):
        """The game's faded flag behind the driver select picture, of the driver's nationality."""
        try:
            self.app.store_forms()          # a nationality just chosen in the Drivers tab
        except ValueError:
            pass
        nat = self.shown.get('nationality')
        f = self.app.pack / 'COUNTRIES' / str(nat) / 'DriverSelect.png'
        return f.relative_to(self.app.pack).as_posix() if nat and f.exists() else None

    def show(self):
        s = self.list.curselection()
        self.shown = self.rows[s[0]] if s and s[0] < len(self.rows) and self.app.pack else None
        self.layers = {}
        if self.shown:
            p = self.project_path()
            try:
                saved = json.loads(p.read_text(encoding='utf-8')).get('layers', {}) if p.exists() else {}
            except (OSError, ValueError):
                saved = {}
            for name, _ in PREVIEW_KINDS[self.kind.get()][2]:
                layer = self.layers[name] = {'file': None, 'x': 0.0, 'y': 0.0, 'scale': 1.0, 'visible': True,
                                             **saved.get(name, {})}
                # the game's own flag / backdrop until another file is chosen
                layer['auto'] = name in ('flag', 'background') and bool(layer.get('auto') or not layer['file'])
            flag = self.layers.get('flag')
            if flag and flag['auto']:       # follows the nationality
                flag['file'] = self.flag_file()
            if self.layers.get('background', {}).get('auto'):
                self.load_backdrop()
        if self.sel.get() not in self.layers:
            names = list(self.layers)
            self.sel.set(names[-1] if names else '')
        self.layer_rows()
        self.show_current()
        self.selected_to_vars()
        self.redraw()

    def layer_rows(self):
        for w in self.layer_box.winfo_children():
            w.destroy()
        if not self.shown:
            ttk.Label(self.layer_box, text='No driver selected').pack(anchor='w')
            return
        for r, (name, title) in enumerate(reversed(PREVIEW_KINDS[self.kind.get()][2])):
            layer = self.layers[name]
            ttk.Radiobutton(self.layer_box, text=title, value=name, variable=self.sel,
                            command=self.selected_to_vars).grid(row=r * 2, column=0, sticky='w')
            vis = tk.BooleanVar(value=layer['visible'])
            ttk.Checkbutton(self.layer_box, text='Show', variable=vis,
                            command=lambda n=name, v=vis: self.set_visible(n, v.get())).grid(row=r * 2, column=1)
            ttk.Button(self.layer_box, text='Choose...', width=9,
                       command=lambda n=name: self.choose(n)).grid(row=r * 2, column=2)
            ttk.Button(self.layer_box, text='Clear', width=6,
                       command=lambda n=name: self.clear(n)).grid(row=r * 2, column=3)
            text = Path(layer['file']).name if layer['file'] else '(none)'
            if name == 'flag' and layer['auto']:
                text = (f'{text}: the game\'s flag of {self.shown.get("nationality")}' if layer['file'] else
                        f'(no driver select flag for {self.shown.get("nationality") or "no nationality"})')
            info = ttk.Frame(self.layer_box)
            info.grid(row=r * 2 + 1, column=0, columnspan=4, sticky='w', padx=(20, 0), pady=(0, 4))
            if name == 'background' and layer['auto']:
                self.backdrop_row(info)
            else:
                ttk.Label(info, text=text, foreground='#555', wraplength=260).pack(anchor='w')

    def backdrop_row(self, info):
        """The game's backdrop in the team colour: the colour and who else uses it."""
        team = self.team_of()
        if not self.backdrop:
            ttk.Label(info, text=self.backdrop_note or 'The game\'s backdrop: loading...', foreground='#555',
                      wraplength=260).pack(anchor='w')
            return
        if not team:
            ttk.Label(info, text='The game\'s backdrop (this driver has no team: white)', foreground='#555',
                      wraplength=260).pack(anchor='w')
            return
        colour, custom = self.team_colour(team)
        mates = [d.get('display_name') or d['id'] for d in self.app.drivers
                 if d['id'] in (team.get('driver1'), team.get('driver2')) and d is not self.shown]
        ttk.Label(info, foreground='#555', wraplength=260, justify='left',
                  text=f'The game\'s backdrop in the colour of {team.get("name") or team["id"]} '
                       f'({hex_colour(colour)}, {"changed" if custom else "original"}). The team colour is also used '
                       f'for {", ".join(mates) or "the team mate"}: Save each picture.').pack(anchor='w')
        row = ttk.Frame(info)
        row.pack(anchor='w', pady=(2, 0))
        tk.Label(row, background=hex_colour(colour), width=4, relief='sunken').pack(side='left', fill='y')
        ttk.Button(row, text='Team colour...', command=self.pick_colour).pack(side='left', padx=(4, 0))
        ttk.Button(row, text='Original colour', command=lambda: self.set_team_colour(None)).pack(side='left')

    def show_current(self):
        p = self.ui_dir() / f'{self.kind.get()}.png' if self.shown else None
        self.current_photo = load_photo(p, 100) if p and p.exists() else None
        self.current.configure(image=self.current_photo or '', text='' if self.current_photo else '(none)')

    def image(self, rel):
        """(w, h, RGBA) of a pack file, read once (again when the file changes)."""
        p = self.app.pack / rel
        try:
            key = (p, p.stat().st_mtime_ns)
        except OSError:
            return None
        if key not in self.cache:
            try:
                self.cache[key] = gtf.read_png(p)
            except (OSError, ValueError, zlib.error) as e:
                self.app.log(f'{rel}: {e}')
                self.cache[key] = None
        return self.cache[key]

    # ------------------------------------------------------------------ the team colour backdrop
    def team_of(self):
        """The pack's team the shown driver drives for (None: no seat, e.g. the safety car driver)."""
        return next((t for t in (self.app.ents or {}).get('teams', [])
                     if self.shown and self.shown['id'] in (t.get('driver1'), t.get('driver2'))), None)

    def colour_path(self, team):
        return self.app.pack / 'CARS' / team['id'] / 'ui' / 'preview' / 'backdrop.json'

    def team_colour(self, team):
        """(colour, changed): the team's colour from the pack, else the colour of its original photos."""
        try:
            c = parse_colour(json.loads(self.colour_path(team).read_text(encoding='utf-8')).get('colour'))
        except (OSError, ValueError, AttributeError):
            c = None
        if c:
            return c, True
        orig = next((t for t in (self.app.original or {}).get('teams', []) if t['id'] == team['id']), team)
        for d in (orig.get('driver1'), orig.get('driver2'), self.shown['id']):
            if str(d).lower() in self.orig_colours:
                return self.orig_colours[str(d).lower()], False
        return (255, 255, 255), False

    def pick_colour(self):
        team = self.team_of()
        if not team:
            return
        c = colorchooser.askcolor(hex_colour(self.team_colour(team)[0]), parent=self,
                                  title=f'Pre-race backdrop colour of {team.get("name") or team["id"]}')[0]
        if c:
            self.set_team_colour(tuple(int(v) for v in c))

    def set_team_colour(self, colour):
        team = self.team_of()
        if not team:
            return
        p = self.colour_path(team)
        if colour:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({'colour': hex_colour(colour)}), encoding='utf-8', newline='\n')
        else:
            p.unlink(missing_ok=True)
        self.app.log(f'{team.get("name") or team["id"]}: pre-race backdrop colour '
                     f'{hex_colour(colour) if colour else "back to the original"} (Save the team\'s pictures, '
                     f'then Build)')
        self.layer_rows()
        self.redraw()

    def load_backdrop(self):
        """The backdrop from the cache in the launcher folder, or made from the game's photos (about 10 s, in the
        background; drawn when ready)."""
        if self.backdrop or self.backdrop_note.startswith('Making'):
            return
        base = self.app.cfg_path.with_name(BACKDROP_CACHE)
        try:
            _, _, px = gtf.read_png(base.with_suffix('.png'))
            cols = json.loads(base.with_suffix('.json').read_text(encoding='utf-8'))
            self.orig_colours = {k: tuple(v) for k, v in cols.items()}
            self.backdrop = (px[0::4], px[1::4], px[3::4])
            return
        except (OSError, ValueError, zlib.error):
            pass
        usr = self.app.usrdir()
        if not usr:
            self.backdrop_note = 'The game\'s backdrop needs the game folder (above)'
            return
        self.backdrop_note = 'Making the team colour backdrop from the game\'s pre-race photos (once, ~10 s)...'
        result = []

        def work():         # no Tk calls here
            try:
                game = season_pack.Game(usr)
                pics, cols = [], {}
                for n in game.names:
                    lo = n.lower().replace('\\', '/')
                    if not (lo.startswith('frontend/textures/prerace/drivers/') and lo.endswith('.gtf')):
                        continue
                    raw = next((game.get(a, n) for a in game.where(n)), None)
                    w, h, px = gtf.decode_level(gtf.Gtf(raw), 0) if raw else (0, 0, b'')
                    if (w, h) == (512, 512):
                        px = gtf.flip_rows(w, h, px)
                        cols[Path(lo).stem] = corner_colour(w, px)
                        pics.append((px, cols[Path(lo).stem]))
                s, u, known, a = make_backdrop(pics, 512, 512)
                out = bytearray(512 * 512 * 4)
                out[0::4], out[1::4], out[2::4], out[3::4] = s, u, known, a
                base.with_suffix('.png').write_bytes(png_bytes(512, 512, bytes(out)))
                base.with_suffix('.json').write_text(json.dumps(cols), encoding='utf-8', newline='\n')
                result.append(((s, u, a), cols))
            except Exception as e:          # noqa: BLE001 - shown in the layer list
                result.append(e)

        def poll():
            if not result:
                self.after(300, poll)
                return
            if isinstance(result[0], Exception):
                self.backdrop_note = f'The game\'s backdrop could not be made: {result[0]}'
            else:
                self.backdrop, self.orig_colours = result[0]
                self.backdrop_note = ''
                self.app.log(f'pre-race backdrop made from the game\'s photos ({base.name}.png in the launcher '
                             f'folder)')
            self.layer_rows()
            self.redraw()
        threading.Thread(target=work, daemon=True).start()
        self.after(300, poll)

    def src_of(self, name):
        """(w, h, RGBA) of a layer (None: nothing to draw)."""
        layer = self.layers.get(name)
        if not layer or not layer['visible']:
            return None
        if name == 'background' and layer['auto']:
            if not self.backdrop:
                return None
            team = self.team_of()
            key = ('backdrop', self.team_colour(team)[0] if team else (255, 255, 255))
            if key not in self.cache:
                self.cache[key] = (512, 512, backdrop_rgba(self.backdrop, key[1]))
            return self.cache[key]
        return self.image(layer['file']) if layer['file'] else None

    def save_project(self):
        p = self.project_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({'layers': self.layers}, indent=1), encoding='utf-8', newline='\n')

    def choose(self, name):
        if not self.shown:
            return
        src = filedialog.askopenfilename(title=f'{name.capitalize()} layer (PNG)',
                                         filetypes=[('PNG pictures', '*.png'), ('All files', '*.*')])
        if not src:
            return
        try:
            w, h, _ = gtf.read_png(src)
        except (OSError, ValueError, zlib.error) as e:
            messagebox.showerror('Content Manager', f'{Path(src).name}: {e}\n\nSave it again as an 8-bit RGBA PNG.')
            return
        dst = self.ui_dir() / 'preview' / f'{self.kind.get()}_{name}.png'
        dst.parent.mkdir(parents=True, exist_ok=True)
        if Path(src).resolve() != dst.resolve():
            shutil.copyfile(src, dst)
        layer = self.layers[name]
        layer.update(file=dst.relative_to(self.app.pack).as_posix(), auto=False, visible=True)
        if not layer.get('placed'):        # a new layer: fitted to the picture's height, at the right edge
            pw, ph = PREVIEW_KINDS[self.kind.get()][1]
            layer['scale'] = ph / h if name != 'background' else max(pw / w, ph / h)
            layer['x'], layer['y'], layer['placed'] = pw - w * layer['scale'], 0.0, True
            if name == 'background':
                layer['x'] = 0.0
        self.sel.set(name)
        self.changed()
        self.layer_rows()

    def clear(self, name):
        layer = self.layers.get(name)
        if not layer:
            return
        layer.update(file=None, auto=name in ('flag', 'background'), placed=False, x=0.0, y=0.0, scale=1.0)
        if name == 'flag':
            layer['file'] = self.flag_file()
        elif layer['auto']:
            self.load_backdrop()
        self.changed()
        self.layer_rows()

    def set_visible(self, name, on):
        self.layers[name]['visible'] = on
        self.changed()

    # ------------------------------------------------------------------ the selected layer's place
    def selected(self):
        return self.layers.get(self.sel.get())

    def selected_to_vars(self):
        layer = self.selected()
        self.quiet = True
        for key in self.vars:
            v = layer[key] * 100 if layer and key == 'scale' else layer[key] if layer else ''
            self.vars[key].set(f'{v:.4g}' if layer else '')
        self.quiet = False

    def typed(self):
        layer = self.selected()
        if self.quiet or not layer:
            return
        try:
            x, y, s = (float(self.vars[k].get()) for k in ('x', 'y', 'scale'))
        except ValueError:
            self.selected_to_vars()
            return
        if (x, y, s / 100) != (layer['x'], layer['y'], layer['scale']) and s > 0:
            layer.update(x=x, y=y, scale=s / 100, placed=True)
            self.changed()

    def fit(self, how):
        layer = self.selected()
        src = self.src_of(self.sel.get())
        if not src:
            return
        pw, ph = PREVIEW_KINDS[self.kind.get()][1]
        if how in 'hw':
            layer['scale'] = ph / src[1] if how == 'h' else pw / src[0]
        elif how == '1':
            layer['scale'] = 1.0
        layer['x'] = (pw - src[0] * layer['scale']) / 2
        layer['y'] = (ph - src[1] * layer['scale']) / 2 if how != 'h' else 0.0
        layer['placed'] = True
        self.changed()

    def changed(self):
        if self.shown:
            self.save_project()
        self.selected_to_vars()
        self.redraw()

    # ------------------------------------------------------------------ preview
    def redraw(self):
        """Draws again when Tk is idle (several changes in a row draw once)."""
        if not self.render_due:
            self.render_due = True
            self.after_idle(self.draw)

    def draw(self):
        self.render_due = False
        c = self.canvas
        c.delete('all')
        self.photos, self.items = [], {}
        pw, ph = PREVIEW_KINDS[self.kind.get()][1]
        cw, ch = max(1, c.winfo_width()), max(1, c.winfo_height())
        z = min(1.0, (cw - 16) / pw, (ch - 16) / ph)
        if z <= 0:
            return
        ox, oy = (cw - round(pw * z)) // 2, (ch - round(ph * z)) // 2
        self.view = (z, ox, oy)
        sq = max(4, round(16 * z))
        for y in range(0, round(ph * z), sq):
            for x in range(0, round(pw * z), sq):
                c.create_rectangle(ox + x, oy + y, ox + min(x + sq, round(pw * z)), oy + min(y + sq, round(ph * z)),
                                   fill='#ffffff' if (x // sq + y // sq) % 2 else '#dcdcdc', width=0)
        for name, _ in PREVIEW_KINDS[self.kind.get()][2]:
            layer = self.layers.get(name)
            src = self.src_of(name)
            part = render_nearest(src, layer, z, pw, ph) if src else None
            if not part:
                continue
            x0, y0, w, h, rgba = part
            photo = tk.PhotoImage(data=base64.b64encode(png_bytes(w, h, rgba)).decode('ascii'), format='png')
            self.photos.append(photo)
            self.items[name] = c.create_image(ox + x0, oy + y0, image=photo, anchor='nw')
        c.create_rectangle(ox - 1, oy - 1, ox + round(pw * z), oy + round(ph * z), outline='#333')

    def press(self, e):
        layer = self.selected()
        self.drag = (e.x, e.y, layer['x'], layer['y']) if self.src_of(self.sel.get()) else None

    def motion(self, e):
        layer = self.selected()
        if not self.drag or not layer:
            return
        z = self.view[0]
        x, y = self.drag[2] + (e.x - self.drag[0]) / z, self.drag[3] + (e.y - self.drag[1]) / z
        item = self.items.get(self.sel.get())
        if item:     # moved as it is until the button is released (then drawn again, cut to the picture)
            self.canvas.move(item, (x - layer['x']) * z, (y - layer['y']) * z)
        layer.update(x=round(x), y=round(y), placed=True)
        self.selected_to_vars()

    def release(self):
        if self.drag:
            self.drag = None
            self.changed()

    def wheel(self, e, step=None):
        layer = self.selected()
        if not self.src_of(self.sel.get()):
            return
        k = 1.04 if (step or e.delta) > 0 else 1 / 1.04
        z, ox, oy = self.view
        px, py = (e.x - ox) / z, (e.y - oy) / z         # this point of the picture stays under the mouse
        layer['x'], layer['y'] = px - (px - layer['x']) * k, py - (py - layer['y']) * k
        layer['scale'] *= k
        layer['placed'] = True
        self.changed()

    # ------------------------------------------------------------------ saving
    def merged(self):
        """(w, h, RGBA) of the layers merged at full size."""
        pw, ph = PREVIEW_KINDS[self.kind.get()][1]
        out = bytearray(pw * ph * 4)
        for name, _ in PREVIEW_KINDS[self.kind.get()][2]:
            src = self.src_of(name)
            if src:
                blend_layer(out, src, self.layers[name], pw, ph)
        return pw, ph, bytes(out)

    def write(self, path):
        self.app.status_var.set('Merging the layers...')
        self.configure(cursor='watch')
        self.update_idletasks()
        try:
            w, h, rgba = self.merged()
            Path(path).write_bytes(png_bytes(w, h, rgba))
        finally:
            self.configure(cursor='')
            self.app.status_var.set('Ready')

    def save(self):
        if not self.shown or not any(self.src_of(n) for n in self.layers):
            return
        dst = self.ui_dir() / f'{self.kind.get()}.png'
        if not messagebox.askyesno('Content Manager', f'Replace {dst.relative_to(self.app.pack)} with the merged '
                                   'layers?'):
            return
        self.write(dst)
        dst.with_suffix('.dds').unlink(missing_ok=True)     # a DDS replacing the PNG would win at Build
        self.app.log(f'{dst.relative_to(self.app.pack)}: merged from the preview layers (Build to apply)')
        self.show_current()

    def export(self):
        if not self.shown:
            return
        dst = filedialog.asksaveasfilename(title='Export the merged picture', defaultextension='.png',
                                           initialfile=f'{self.shown["id"]}_{self.kind.get()}.png',
                                           filetypes=[('PNG pictures', '*.png')])
        if dst:
            self.write(dst)
            self.app.log(f'{self.shown["id"]} {self.kind.get()}: merged picture exported to {dst}')

    def open_folder(self):
        if self.shown:
            d = self.ui_dir() / 'preview'
            open_path(d if d.is_dir() else self.ui_dir())


class App(tk.Tk):
    def __init__(self, launcher_dir):
        super().__init__()
        self.title('F1 CE Content Manager')
        self.geometry('1280x860')
        self.minsize(1000, 700)
        self.q = queue.Queue()
        self.busy = False
        self.pack, self.kind = None, None
        self.ents = self.dyn = None
        self.manifest, self.roles = {}, {}
        self.cfg_path = None
        self.launcher_var, self.game_var = tk.StringVar(), tk.StringVar()
        self._build_ui()
        self.set_launcher_dir(launcher_dir)
        self.after(100, self._poll)

    # ------------------------------------------------------------------ layout
    def _build_ui(self):
        top = ttk.Frame(self, padding=6)
        top.pack(fill='x')
        for r, (label, var, cmd) in enumerate([('Launcher folder (MODS, PLAYER_HELMETS)', self.launcher_var,
                                                 self.browse_launcher),
                                                ('Game folder (PS3_GAME)', self.game_var, self.browse_game)]):
            ttk.Label(top, text=label).grid(row=r, column=0, sticky='w')
            ttk.Entry(top, textvariable=var).grid(row=r, column=1, sticky='we', padx=6)
            ttk.Button(top, text='Browse...', command=cmd).grid(row=r, column=2)
        top.columnconfigure(1, weight=1)

        # bottom first, so the log and the progress bar keep their place when the window is small
        self.logbox = tk.Text(self, height=8, state='disabled', wrap='none')
        self.logbox.pack(side='bottom', fill='x', padx=6, pady=6)
        status = ttk.Frame(self)
        status.pack(side='bottom', fill='x', padx=6, pady=(6, 0))
        self.bar = ttk.Progressbar(status, mode='determinate', maximum=1000)
        self.bar.pack(side='left', fill='x', expand=True)
        self.status_var = tk.StringVar(value='Ready')
        ttk.Label(status, textvariable=self.status_var, width=70).pack(side='left', padx=8)

        body = ttk.PanedWindow(self, orient='horizontal')
        body.pack(fill='both', expand=True, padx=6)
        left = ttk.Frame(body)
        body.add(left, weight=1)
        self.tree = ttk.Treeview(left, show='tree', selectmode='browse')
        self.tree.pack(fill='both', expand=True)
        self.tree.bind('<<TreeviewSelect>>', lambda e: self.select_pack())
        for text, cmd in [('New season pack...', self.new_season_pack), ('New helmet pack...', self.new_helmet_pack),
                          ('Save', self.save), ('Build', self.build), ('Open folder', self.open_pack),
                          ('Refresh', self.refresh)]:
            ttk.Button(left, text=text, command=cmd).pack(fill='x')
        self.models_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(left, text='New packs with 3D models (OBJ + glTF for Blender)', variable=self.models_var).pack(anchor='w')

        right = ttk.Frame(body)
        body.add(right, weight=4)
        self.header = ttk.Label(right, text='Select or create a pack', font=('Segoe UI', 13, 'bold'))
        self.header.pack(anchor='w', pady=4)
        self.nb = ttk.Notebook(right)
        self.nb.pack(fill='both', expand=True)
        # Teams tab driver edits show up in the Drivers tab's reference line
        self.nb.bind('<<NotebookTabChanged>>', lambda e: self.show_driver_refs())
        self.teams_pane = self._team_pane(
            'Teams', TEAM_FIELDS, [('Move up', lambda: self.move_team(-1)), ('Move down', lambda: self.move_team(1))],
            'Order of the teams in the menus and the pit lane. Save and Build to apply.')
        for key in ('driver1', 'driver2'):
            self.teams_pane.form.combos[key].bind('<<ComboboxSelected>>', lambda e, k=key: self.change_seat(k))
        self.drivers_pane = self._driver_pane(
            'Drivers', [('Move up', lambda: self.move_driver(-1)), ('Move down', lambda: self.move_driver(1)),
                        ('Arrange by team', self.arrange_drivers)],
            'Order of the 22 race drivers on the entry list, and in the standings before the first race. Save and '
            'Build to apply.')
        self._rims_tab()
        self._steering_tab()
        self.classic_pane = self._team_pane(
            'Classic Cars', CLASSIC_FIELDS, note='The classic cars: their order is fixed, their drivers are in the '
                                                 'Extra Drivers tab.')
        self.extra_pane = self._driver_pane(
            'Extra Drivers', note='The drivers who are not on the entry list: the player, the classic cars\' drivers, '
                                  'the safety car and the ghost car. Their order is fixed.')
        self.extra_pane.list.configure(width=30)       # car and driver
        self.team_panes, self.driver_panes = (self.teams_pane, self.classic_pane), (self.drivers_pane, self.extra_pane)
        self.preview = PreviewEditor(self.nb, self)
        self.nb.add(self.preview, text='Driver preview editor')
        self._points_tab()
        self._helmets_tab()



    def _list_and_detail(self, title, buttons=(), note=''):
        """A tab with a list and the selected row's detail; buttons [(text, command)] and a note below the list."""
        tab = ttk.Frame(self.nb, padding=6)
        self.nb.add(tab, text=title)
        left = ttk.Frame(tab)
        left.pack(side='left', fill='y')
        # bottom first, so the buttons keep their place when the window is small
        if note:
            ttk.Label(left, text=note, foreground='#555', wraplength=165, justify='left').pack(side='bottom', anchor='w')
        for text, cmd in reversed(buttons):
            ttk.Button(left, text=text, command=cmd).pack(side='bottom', fill='x')
        lst = tk.Listbox(left, width=24, exportselection=False)
        lst.pack(fill='both', expand=True)
        detail = ttk.Frame(tab, padding=(10, 0))
        detail.pack(side='left', fill='both', expand=True)
        return tab, lst, detail

    def _team_pane(self, title, fields, buttons=(), note=''):
        tab, lst, d = self._list_and_detail(title, buttons, note)
        pane = Pane(tab, lst)
        lst.bind('<<ListboxSelect>>', lambda e: self.show_team(pane))
        cols = ttk.Frame(d)
        cols.pack(fill='x')
        pane.form = Form(cols, fields, choices=('driver1', 'driver2', 'nationality'))
        pane.form.pack(side='left', fill='x', expand=True, anchor='n')
        perf = ttk.LabelFrame(cols, text='dynamics.bin values (the AI uses drag, engine and gears only)', padding=6)
        perf.pack(side='left', fill='x', expand=True, anchor='n', padx=(10, 0))
        pane.form2 = Form(perf, [(f'{g}.{k}', label) for g, k, label in PERF_FIELDS])
        pane.form2.pack(fill='x')
        ttk.Label(d, text=USED_LEGEND, wraplength=780, justify='left', foreground='#555').pack(anchor='w', pady=(6, 0))
        row = ttk.Frame(d)
        row.pack(side='bottom', fill='x')
        ttk.Button(row, text='Open livery folder', command=lambda: self.open_sub(pane, 'livery')).pack(side='left')
        ttk.Button(row, text='Open 3D model folder', command=lambda: self.open_sub(pane, 'model')).pack(side='left')
        imgs = self._scrolled(d, fit_width=True)
        pane.imgs = [ImageSlot(imgs, self, t, 110) for t in
                          ('Base paint', 'Sponsor decals', 'Specular / reflection map', 'Team logo (menus)',
                           'LOD base paint', 'LOD sponsor decals', 'LOD specular / reflection map',
                           'Driver overalls (in the car)')]
        for i, slot in enumerate(pane.imgs):
            slot.grid(row=i // 4, column=i % 4, sticky='nsew', padx=4, pady=2)
        for c in range(4):
            imgs.columnconfigure(c, weight=1)
        return pane

    def _rims_tab(self):
        self.rims_tab, self.rim_list, d = self._list_and_detail('Rims and tyres')
        self.rim_list.bind('<<ListboxSelect>>', lambda e: self.show_rims())
        ttk.Label(d, wraplength=900, justify='left', foreground='#555',
                  text='The rims and tyres a car uses are named in the database; several cars share some files (the '
                       '"Shared with" line), so a change there changes every car listed. "Blurred" versions are drawn '
                       'when the wheels spin fast. The garage car uses the same wheel textures.').pack(anchor='w')
        row = ttk.Frame(d)
        row.pack(fill='x', pady=(6, 0))
        ttk.Label(row, text='Tyre maker (sidewall)').pack(side='left')
        self.maker_var = tk.StringVar()
        self.maker_box = ttk.Combobox(row, textvariable=self.maker_var, state='readonly', width=24)
        self.maker_box.pack(side='left', padx=6)
        self.maker_box.bind('<<ComboboxSelected>>', lambda e: self.set_maker())
        ttk.Button(row, text='New tyre maker...', command=self.new_maker).pack(side='left')
        ttk.Button(row, text='Open 3D tyre model folder', command=self.open_tyre_model).pack(side='left', padx=6)
        imgs = ttk.Frame(d)
        imgs.pack(fill='both', expand=True, pady=4)
        self.rim_imgs = [ImageSlot(imgs, self, t) for t in
                         ('Rim', 'Rim, spinning (blurred)', 'Rim normal map', 'Tyre sidewall', 'Tyre tread (dry)',
                          'Tyre tread (wet)')]
        for i, slot in enumerate(self.rim_imgs):
            slot.grid(row=i // 3, column=i % 3, sticky='nsew', padx=4, pady=2)
        for c in range(3):
            imgs.columnconfigure(c, weight=1)

    def _steering_tab(self):
        self.steering_tab, self.steer_list, d = self._list_and_detail('Steering wheels')
        self.steer_list.bind('<<ListboxSelect>>', lambda e: self.show_steering())
        ttk.Label(d, wraplength=900, justify='left', foreground='#555',
                  text='The steering wheel each driver holds in the cockpit (Generic: the far / low detail drivers; '
                       'McLaren and the Williams FW18 show their display on a separate dash model). The display is '
                       'drawn live by the game: the digits and symbols come from "Display digits" (shared by every '
                       'car; keep them white, the game colours them), and the display mesh of the 3D model places '
                       'them through its UVs on the layout guide.').pack(anchor='w')
        row = ttk.Frame(d)
        row.pack(fill='x', pady=(6, 0))
        ttk.Button(row, text='Open folder', command=lambda: self.open_steering('')).pack(side='left')
        ttk.Button(row, text='Open 3D model folder', command=lambda: self.open_steering('model')).pack(side='left',
                                                                                                    padx=6)
        self.steer_grid = ttk.Frame(d)
        self.steer_grid.pack(fill='both', expand=True, pady=4)
        self.steer_slots = []

    def steering_teams(self):
        return [t for t in season_pack.STEERING_MODELS if (self.pack / 'STEERING' / t).is_dir()] if self.pack else []

    def show_steering(self):
        for w in self.steer_grid.winfo_children():
            w.destroy()
        teams = self.steering_teams()
        if not teams:
            ttk.Label(self.steer_grid, text='This pack was made before the steering wheels: use "Open folder" to '
                                            'add them.').grid(sticky='w')
            return
        s = self.steer_list.curselection()
        team = teams[s[0]] if s and s[0] < len(teams) else teams[0]
        folder = self.pack / 'STEERING' / team
        order = lambda p: ({'c': 0, 'ca': 0, 's': 1, 'n': 2}.get(p.stem.lower().rsplit('_', 1)[-1], 3), p.name)  # noqa
        pics = [(p.stem, p, '') for p in sorted(folder.glob('*.png'), key=order) if not p.stem.endswith('_reflection')]
        pics.append(('Display digits (all cars)', self.pack / 'STEERING' / 'LCDDisplaySource.png',
                     'white shapes: the game colours each part'))
        guide = next(iter(sorted((folder / 'model').glob('*_textures/Textures/SWDisplay.png'))), None)
        if guide:
            pics.append(('Display layout (guide only)', guide, 'not used by the game: the display mesh UVs on it '
                                                              'say where each part of the live display shows'))
        for i, (title, path, note) in enumerate(pics):
            slot = ImageSlot(self.steer_grid, self, title)
            slot.grid(row=i // 3, column=i % 3, sticky='nsew', padx=4, pady=2)
            slot.show(path, note)
        for c in range(3):
            self.steer_grid.columnconfigure(c, weight=1)

    def open_steering(self, sub):
        if not self.pack or self.kind != 'season':
            return
        teams = self.steering_teams()
        if not teams:
            if messagebox.askyesno('Content Manager', 'This pack has no steering wheels yet. Add them now (textures, '
                                   'and the 3D models if "New packs with 3D models" is ticked)?'):
                self.run_tool(['steering', str(self.pack)] + (['--models'] if self.models_var.get() else []),
                              self.select_pack)
            return
        s = self.steer_list.curselection()
        folder = self.pack / 'STEERING' / teams[s[0] if s else 0] / sub
        if folder.is_dir():
            open_path(folder)
        elif sub == 'model' and messagebox.askyesno('Content Manager', 'This pack has no 3D steering wheel models '
                                                    'yet. Export them now (OBJ + glTF for Blender)?'):
            self.run_tool(['steering', str(self.pack), '--models'], self.select_pack)

    def _driver_pane(self, title, buttons=(), note=''):
        tab, lst, d = self._list_and_detail(title, buttons, note)
        pane = Pane(tab, lst)
        lst.bind('<<ListboxSelect>>', lambda e: self.show_driver(pane))
        # small windows: the statistics box goes below the bottom, the pictures past the right edge
        d = self._scrolled(d, fit_width=True, min_width=790)
        pane.ref = ttk.Label(d, foreground='#555', justify='left')
        pane.ref.pack(anchor='w', pady=(0, 6))
        cols = ttk.Frame(d)
        cols.pack(fill='x')
        imgs = ttk.Frame(cols)
        imgs.pack(side='right', fill='both', padx=(10, 0))     # first: the form gets what is left
        pane.form = Form(cols, DRIVER_FIELDS, choices=('nationality',))
        pane.form.pack(side='left', fill='x', expand=True, anchor='n')
        box = ttk.LabelFrame(d, text='Profile and career statistics (driver profile in the menus)', padding=4)
        box.pack(fill='x', pady=(6, 0))
        pane.form2 = Form(box, DRIVER_STATS, columns=3, width=16)
        pane.form2.pack(fill='x')
        pane.imgs = [ImageSlot(imgs, self, t, 90) for t in ('Helmet paint (in the car)', 'Helmet specular',
                                                               'Pre-race photo (menus)',
                                                               'Overalls (garage and podium)',
                                                               'Face (garage and podium)')]
        for i, s in enumerate(pane.imgs):
            s.grid(row=i // 2, column=i % 2, sticky='nsew', padx=4, pady=2)
        row = ttk.Frame(imgs)
        row.grid(row=2, column=1, sticky='nw', padx=4, pady=(20, 2))
        ttk.Label(row, text='Garage and podium figures: face, hair,\ncap, eyes, teeth and overalls (PNG); the\n'
                            '3D figures are a reference for painting\n(not imported)', foreground='#555',
                  justify='left').pack(anchor='w')
        ttk.Button(row, text='Open figure folder', command=lambda: self.open_figure(pane, 'figure')).pack(anchor='w',
                                                                                                 pady=(6, 0))
        ttk.Button(row, text='Open 3D figure folder', command=lambda: self.open_figure(pane, 'model')).pack(anchor='w',
                                                                                                   pady=(4, 0))
        return pane

    def _points_tab(self):
        self.points_tab = ttk.Frame(self.nb, padding=6)
        self.nb.add(self.points_tab, text='Points')
        ttk.Label(self.points_tab, justify='left',
                  text='Points of 1st, 2nd, 3rd ... separated by spaces (up to 22 places). The original system is '
                       '10 8 6 5 4 3 2 1.\nThe game reads this when the season pack is selected; no Build needed.'
                  ).pack(anchor='w')
        self.points_var = tk.StringVar()
        ttk.Entry(self.points_tab, textvariable=self.points_var, font=('Consolas', 13)).pack(fill='x', pady=8)
        row = ttk.Frame(self.points_tab)
        row.pack(anchor='w')
        for label, pts in [('2006 (original)', '10 8 6 5 4 3 2 1'), ('2003-2009', '10 8 6 5 4 3 2 1'),
                           ('1991-2002', '10 6 4 3 2 1'), ('2010+', '25 18 15 12 10 8 6 4 2 1')]:
            ttk.Button(row, text=label, command=lambda p=pts: self.points_var.set(p)).pack(side='left')

    def _helmets_tab(self):
        self.helmets_tab = ttk.Frame(self.nb, padding=6)
        self.nb.add(self.helmets_tab, text='Player helmets')
        self.helmet_grid = self._scrolled(self.helmets_tab)
        self.helmet_slots = []

    def _scrolled(self, master, fit_width=False, min_width=0):
        """A frame in a vertically scrolling area (fit_width: as wide as the area, but not narrower than min_width:
        a horizontal scroll bar appears instead)."""
        outer = ttk.Frame(master)
        outer.pack(fill='both', expand=True, pady=4)
        canvas = tk.Canvas(outer, highlightthickness=0)
        sb = ttk.Scrollbar(outer, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        hsb = ttk.Scrollbar(outer, orient='horizontal', command=canvas.xview)
        canvas.configure(xscrollcommand=hsb.set)
        canvas.pack(fill='both', expand=True)
        inner = ttk.Frame(canvas)
        win = canvas.create_window((0, 0), window=inner, anchor='nw')
        inner.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        if fit_width:
            def fit(e):
                canvas.itemconfigure(win, width=max(e.width, min_width))
                if e.width < min_width and not hsb.winfo_ismapped():
                    hsb.pack(side='bottom', fill='x', before=canvas)
                elif e.width >= min_width and hsb.winfo_ismapped():
                    hsb.pack_forget()
            canvas.bind('<Configure>', fit)
        wheel = lambda e: canvas.yview_scroll(-1 if e.delta > 0 else 1, 'units')  # noqa: E731
        outer.bind('<Enter>', lambda e: canvas.bind_all('<MouseWheel>', wheel))
        outer.bind('<Leave>', lambda e: canvas.unbind_all('<MouseWheel>'))
        return inner

    # ------------------------------------------------------------------ folders and settings
    def set_launcher_dir(self, d):
        d = Path(d).resolve()
        self.launcher_var.set(str(d))
        self.cfg_path = d / 'content_manager.ini'
        cfg = configparser.ConfigParser()
        cfg.read(self.cfg_path, encoding='utf-8')
        game = cfg.get('paths', 'game', fallback='')
        if not game:
            li = configparser.ConfigParser(strict=False, interpolation=None)
            try:
                li.read(d / 'f1ce_launcher.ini', encoding='utf-8')
                game = li.get('game', 'path', fallback='')
            except configparser.Error:
                game = ''
        self.game_var.set(game)
        self.refresh()

    def save_settings(self):
        cfg = configparser.ConfigParser()
        cfg['paths'] = {'game': self.game_var.get()}
        try:
            with open(self.cfg_path, 'w', encoding='utf-8') as f:
                cfg.write(f)
        except OSError:
            pass

    def browse_launcher(self):
        d = filedialog.askdirectory(title='Launcher folder', initialdir=self.launcher_var.get())
        if d:
            self.set_launcher_dir(d)

    def browse_game(self):
        d = filedialog.askdirectory(title='Game folder (PS3_GAME)', initialdir=self.game_var.get())
        if d:
            self.game_var.set(d)
            self.save_settings()

    def usrdir(self):
        g = Path(self.game_var.get())
        for c in (g / 'USRDIR', g, g / 'PS3_GAME' / 'USRDIR'):
            if (c / 'offline.wad').exists():
                return c
        return None

    def folders(self):
        root = Path(self.launcher_var.get())
        return {'season': root / 'MODS', 'helmet': root / 'PLAYER_HELMETS'}

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for kind, title in (('season', 'Season packs (MODS)'), ('helmet', 'Player helmet packs (PLAYER_HELMETS)')):
            node = self.tree.insert('', 'end', text=title, open=True, values=())
            d = self.folders()[kind]
            if d.is_dir():
                for p in sorted(x for x in d.iterdir() if x.is_dir() and not x.name.startswith('.')):
                    self.tree.insert(node, 'end', iid=f'{kind}|{p}', text=p.name)

    # ------------------------------------------------------------------ pack selection
    def select_pack(self):
        sel = self.tree.selection()
        if not sel or '|' not in sel[0]:
            return
        kind, path = sel[0].split('|', 1)
        self.pack, self.kind = Path(path), kind
        built = (self.pack / 'template.json').exists()
        self.header.configure(text=f'{self.pack.name}  ({"season pack" if kind == "season" else "helmet pack"}'
                                   f'{"" if built else ", no template: files only"})')
        for tab in self.nb.tabs():
            if tab != str(self.helmets_tab):
                self.nb.tab(tab, state='normal' if kind == 'season' else 'hidden')
        self.nb.tab(self.helmets_tab, state='normal' if kind == 'helmet' else 'hidden')
        self.nb.select(self.teams_pane.tab if kind == 'season' else self.helmets_tab)
        if kind == 'season':
            self.load_season()
        else:
            self.load_helmets()

    def load_json(self, rel):
        p = self.pack / rel
        return json.loads(p.read_text(encoding='utf-8')) if p.exists() else None

    def load_manifest(self):
        self.manifest = self.load_json('template.json') or {}
        self.roles = self.manifest.get('teams')
        if self.roles is None and self.manifest:
            # packs made before 2026-11-14: find the livery roles in the game's models now
            self.roles = {}
            usr = self.usrdir()
            if usr:
                self.status_var.set('Reading the car models for this older pack...')
                self.update_idletasks()
                game = season_pack.Game(usr)
                for team, model in season_pack.TEAMS.items():
                    self.roles[team] = season_pack.livery_roles(game, team, model)
                    self.roles[team].update(season_pack.wheel_roles(self.ents or {'cars': []}, team,
                                                                    self.manifest.get('files', {})))
                self.status_var.set('Ready')
            self.log(f'{self.pack.name}: made by an older version (no WHEELS folder, no "used in" data): create a '
                     f'new pack to get every feature')

    def pack_rel(self, path):
        try:
            rel = Path(path).relative_to(self.pack).as_posix()
        except ValueError:
            return None
        return rel[:-4] + '.png' if rel.lower().endswith('.dds') else rel

    def file_info(self, path):
        """The template.json entry of a pack picture ({} when it has none)."""
        return self.manifest.get('files', {}).get(self.pack_rel(path)) or {}

    def used_in(self, path):
        rel, info = self.pack_rel(path), self.file_info(path)
        if not info:
            return []
        team = Path(rel).parts[1] if rel.startswith('CARS/') else None
        return info.get('used_in') or season_pack.used_in(rel, info['targets'], (self.roles or {}).get(team))

    def load_season(self):
        for pane in self.team_panes + self.driver_panes:
            pane.shown = None     # the pack shown before
        self.ents = self.load_json('DATABASE/entities.json')
        self.load_manifest()
        self.dyn = self.load_json('DATABASE/dynamics.json')
        self.load_original()
        self.check_lineup()
        self.drivers = (self.ents or {}).get('drivers', [])
        # packs made before 2026-11-21 (drivers) and 2026-11-22 (teams) have no nationality: the game's
        for section in ('drivers', 'teams'):
            original = {r['id']: r for r in (self.original or {}).get(section, [])}
            for r in (self.ents or {}).get(section, []):
                if 'nationality' not in r and original.get(r['id'], {}).get('nationality'):
                    r['nationality'] = original[r['id']]['nationality']
        for pane in self.team_panes + self.driver_panes:
            pane.form.set_choices('nationality', sorted(c['id'] for c in (self.ents or {}).get('countries', [])))
        self.fill_lists()
        self.steer_list.delete(0, 'end')
        names = {t['id']: t.get('name') or t['id'] for t in self.teams}
        for t in self.steering_teams():
            self.steer_list.insert('end', names.get(t, t))
        if self.steering_teams():
            self.steer_list.selection_set(0)
        self.show_steering()
        pts = self.pack / 'DATABASE' / 'points.txt'
        self.points_var.set(' '.join(x for line in (pts.read_text(encoding='utf-8').splitlines() if pts.exists()
                                                      else []) for x in line.split('#')[0].split()))
        for panes, show in ((self.team_panes, self.show_team), (self.driver_panes, self.show_driver)):
            for pane in panes:
                pane.form.load(None)
                pane.form2.load(None)
                if pane.rows:
                    pane.list.selection_set(0)
                    show(pane)
        if self.teams:
            self.rim_list.selection_set(0)
            self.show_rims()

    def load_original(self):
        """The game's own database (None without the game folder), and the ids of the season's teams and drivers."""
        usr = self.usrdir()
        self.original = season_pack.entities.export(season_pack.entities.parse(
            season_pack.Game(usr).get('offline.wad', 'entities.bin'))) if usr else None
        self.orig_drivers = {d['id']: d for d in (self.original or {}).get('drivers', [])}
        if self.original:
            self.race_teams, self.race_drivers = season_pack.entities.race_ids(self.original)
        else:       # the 2006 cars (models <Team>06) and the drivers in their seats
            self.race_teams = [t for t, model in season_pack.TEAMS.items() if model.endswith('06')]
            self.race_drivers = [t.get(k) for t, k in season_pack.entities.seats((self.ents or {}).get('teams', []),
                                                                                self.race_teams)]

    def check_lineup(self):
        """A database whose rows are not a valid season (edited by hand) makes cars overlap in the game: offer to
        repair it."""
        if not self.ents or not self.original:
            return
        problems = season_pack.entities.lineup_problems(self.ents, self.original)
        if not problems:
            return
        for p in problems:
            self.log(f'{self.pack.name}: {p}')
        fixed = copy.deepcopy(self.ents)
        done = season_pack.entities.repair(fixed, self.original)
        if messagebox.askyesno('Content Manager', 'The driver line-up of this pack is not valid: each of the 22 race '
                               'drivers needs one row and exactly one seat.\n\n' + '\n'.join(problems) +
                               '\n\nRepair it like this? (Save to keep it)\n\n' + '\n'.join(done)):
            self.ents = fixed
            for d in done:
                self.log(f'{self.pack.name}: {d}')

    def fill_lists(self):
        """The team, rim and driver lists, in the order of the database (keeps the selected rows)."""
        self.teams = [t for t in (self.ents or {}).get('teams', []) if t['id'] in season_pack.TEAMS]
        self.teams_pane.rows = [t for t in self.teams if t['id'] in self.race_teams]
        self.classic_pane.rows = [t for t in self.teams if t['id'] not in self.race_teams]
        self.drivers_pane.rows = [d for d in self.drivers if d['id'] in self.race_drivers]
        self.extra_pane.rows = [d for d in self.drivers if d['id'] not in self.race_drivers]
        team = lambda t: t.get('name') or t['id']  # noqa: E731
        cars = {t.get('driver1'): team(t) for t in self.classic_pane.rows}
        for lst, rows, shown, text in (
                [(p.list, p.rows, p.shown, team) for p in self.team_panes] +
                [(self.rim_list, self.teams, getattr(self, 'shown_rims', None), team),
                 (self.drivers_pane.list, self.drivers_pane.rows, self.drivers_pane.shown,
                  lambda d: f'{d.get("car_number", "")}  {d.get("display_name") or d["id"]}'),
                 (self.extra_pane.list, self.extra_pane.rows, self.extra_pane.shown,      # many have the same name
                  lambda d: (EXTRA_DRIVERS.get(d['id']) or cars.get(d['id'], d['id'])) +
                  f': {d.get("display_name") or ""}')]):
            lst.delete(0, 'end')
            for n, r in enumerate(rows):
                lst.insert('end', text(r))
                if r is shown:
                    lst.selection_set(n)
                    lst.see(n)
        race = [d for d in self.drivers if d['id'] in self.race_drivers]
        names = {d['id']: f'{d.get("last_name") or d.get("display_name") or ""} ({d["id"]})' for d in race}
        for key in ('driver1', 'driver2'):
            self.teams_pane.form.set_choices(key, [d['id'] for d in race], names)
        self.preview.fill()

    # ------------------------------------------------------------------ order and seats
    def move_row(self, section, ids, row, step):
        """Swaps a row with the next / previous one of the season's rows (ids)."""
        rows = self.ents[section]
        pos = [n for n, r in enumerate(rows) if r['id'] in ids]
        i = next((k for k, n in enumerate(pos) if rows[n] is row), None)
        if i is None or not 0 <= i + step < len(pos):
            return False
        a, b = pos[i], pos[i + step]
        rows[a], rows[b] = rows[b], rows[a]
        return True

    def number_drivers(self):
        for n, d in enumerate([d for d in self.drivers if d['id'] in self.race_drivers]):
            d['order'] = n

    def stored(self):
        """The open forms into the loaded data; False (with a message) when a value is not valid."""
        try:
            self.store_forms()
        except ValueError as e:
            messagebox.showerror('Content Manager', str(e))
            return False
        return True

    def move_team(self, step):
        t = self.cur(self.teams_pane.list, self.teams_pane.rows) if self.kind == 'season' else None
        if t is not None and self.stored() and self.move_row('teams', self.race_teams, t, step):
            self.fill_lists()

    def move_driver(self, step):
        d = self.cur(self.drivers_pane.list, self.drivers_pane.rows) if self.kind == 'season' else None
        if d is not None and self.stored() and self.move_row('drivers', self.race_drivers, d, step):
            self.number_drivers()
            self.fill_lists()

    def arrange_drivers(self):
        """The race drivers in the order of the teams: driver 1 and driver 2 of the first team, ..."""
        if self.kind != 'season' or not self.ents or not self.original or not self.stored():
            return
        order = [t.get(k) for t, k in season_pack.entities.seats(self.ents['teams'], self.race_teams)]
        place = [n for n, d in enumerate(self.drivers) if d['id'] in self.race_drivers]
        race = sorted((self.drivers[n] for n in place),
                      key=lambda d: order.index(d['id']) if d['id'] in order else len(order))
        for n, d in zip(place, race):
            self.drivers[n] = d
        self.number_drivers()
        self.fill_lists()
        self.log('drivers arranged in the order of the teams (Save and Build to apply)')

    def change_seat(self, key):
        """Another driver chosen for a seat: the driver who had it takes the chosen one's seat (one seat each)."""
        t = self.teams_pane.shown
        if not t:
            return
        old = t.get(key)
        if not self.stored():
            return
        new = t.get(key)
        for other, k in season_pack.entities.seats(self.ents['teams'], self.race_teams):
            if other.get(k) == new and not (other is t and k == key):
                other[k] = old
                names = self.teams_pane.form.names[key]
                self.log(f'{names.get(new, new)}: {t.get("name") or t["id"]}; {names.get(old, old)}: '
                         f'{other.get("name") or other["id"]}')
        self.teams_pane.form.load(t)

    def show_driver_refs(self):
        for pane in self.driver_panes:
            self.show_driver_ref(pane)

    def show_driver_ref(self, pane):
        """The driver's database id (the name Teams > Driver 1 / Driver 2 use), original name and current teams."""
        dr = pane.shown
        if self.kind != 'season' or not dr:
            pane.ref.configure(text='')
            return
        for p in self.team_panes:
            if p.shown is not None:
                p.form.store()
        o = getattr(self, 'orig_drivers', {}).get(dr['id'])
        orig = f'{o.get("first_name") or ""} {o.get("last_name") or ""}'.strip() if o else ''
        teams = [f'{t.get("name") or t["id"]} ({"Driver 1" if t.get("driver1") == dr["id"] else "Driver 2"})'
                 for t in (self.ents or {}).get('teams', []) if dr['id'] in (t.get('driver1'), t.get('driver2'))]
        if pane is self.extra_pane:
            cars = dict.fromkeys(t.rsplit(' (', 1)[0] for t in teams)
            pane.ref.configure(text=f'Database id: {dr["id"]}' +
                               (f'      originally {orig} in the game' if orig else '') +
                               f'\nDrives: {", ".join(cars) or "no car of its own"}')
            return
        pane.ref.configure(text=f'Teams tab name: {dr["id"]}' +
                           (f'      originally {orig} in the game' if orig else '') +
                           f'\nIn the Teams tab: {", ".join(teams) or "no team"}   (to move this driver, '
                           f'choose it as Driver 1 or Driver 2 of the new team: the two drivers swap seats)')

    def cur(self, lst, items):
        s = lst.curselection()
        return items[s[0]] if s and s[0] < len(items) else None

    def perf_of(self, team):
        return (self.dyn or {}).get('teams', {}).get(team['id'].lower()) if team else None

    def show_team(self, pane):
        self.store_forms()
        t = pane.shown = self.cur(pane.list, pane.rows)
        pane.form.load(t)
        pane.form2.load(self.perf_of(t), get=lambda d, k: d.get(k.split('.')[0], {}).get(k.split('.')[1]))
        if not t:
            return
        car = self.pack / 'CARS' / t['id']
        roles = (self.roles or {}).get(t['id'], {})
        role = lambda k: self.pack / roles[k] if roles.get(k) else None  # noqa: E731
        pane.imgs[0].show(role('paint'))
        pane.imgs[1].show(role('decals'), 'no specular map of its own: shines as the paint under it'
                               if roles.get('decals') else 'one-layer livery: no decal sheet')
        pane.imgs[3].show(car / 'ui' / 'TeamLogo_Big.png')
        same = roles.get('paint_lod') and roles.get('paint_lod') == roles.get('paint')
        pane.imgs[4].show(role('paint_lod'), 'the same file as the base paint' if same else '')
        pane.imgs[5].show(role('decals_lod'))
        for slot, key in ((pane.imgs[2], 'specular'), (pane.imgs[6], 'specular_lod')):
            mask = role(key + '_mask')
            if mask and mask.exists():
                slot.show(mask, f'white = reflective; Build puts it into the alpha of {Path(roles[key]).name}')
            elif roles.get(key):
                slot.show(role(key), 'reflection mask in the alpha channel' if roles.get(key + '_mask') else
                          'specular intensity (greyscale)')
            elif 'specular_maps' in self.manifest or key in roles:
                slot.show(None, 'none: the car has the same shine all over')
            else:
                slot.show(None, 'Build once to add the specular maps to this older pack')
        drv = car / 'driver'
        pane.imgs[7].show(next(iter(sorted(drv.glob('InCarOveralls*.png'))), None) if drv.is_dir() else None)

    def show_rims(self):
        t = self.shown_rims = self.cur(self.rim_list, self.teams)
        if not t:
            return
        roles = (self.roles or {}).get(t['id'], {})

        def shared_rim(key, rel):
            names = [x.get('name') or x['id'] for x in self.teams if x is not t and rel and
                     ((self.roles or {}).get(x['id'], {}).get(key) == rel)]
            return 'Shared with: ' + ', '.join(names) if names else 'Used by this car only'
        for slot, key in zip(self.rim_imgs[:3], ('rim', 'rim_blurred', 'rim_normal')):
            rel = roles.get(key)
            slot.show(self.pack / rel if rel else None, shared_rim(key, rel) if rel else '')
        makers = season_pack.tyre_makers(self.pack)
        car = season_pack.car_of(self.ents or {'cars': []}, t['id'])
        # the classic cars' wheels are part of the car model: no tyre maker to choose
        maker = season_pack.tyre_maker_of(car, makers) if car and car.get('field65') else None
        self.maker_box['values'] = list(makers)
        self.maker_box.configure(state='readonly' if maker else 'disabled')
        self.maker_var.set(maker or '')
        # the sidewall set the car names now (the tyre maker can change after the template)
        side = [Path(f).relative_to(self.pack).as_posix() for f in season_pack.sidewall_files(self.pack, makers[maker])] \
            if maker in makers else roles.get('sidewall') or []
        users = [x.get('name') or x['id'] for x in self.teams if x is not t and maker and
                 season_pack.tyre_maker_of(season_pack.car_of(self.ents, x['id']) or {}, makers) == maker]
        tread = roles.get('tread') or []
        pick = lambda files, word: next((f for f in files if word in f.lower()), files[0] if files else None)  # noqa
        s0 = pick(side, 'blur0')
        self.rim_imgs[3].show(self.pack / s0 if s0 else None,
                              f'{len(side)} versions (Blur0-3); ' +
                              (f'Shared with: {", ".join(users)}' if users else 'Used by this car only') if side else '')
        for slot, word in ((self.rim_imgs[4], 'dry_blur0_ca'), (self.rim_imgs[5], 'wet_blur0_ca')):
            f = next((x for x in tread if word in x.lower()), None)
            slot.show(self.pack / f if f else None, 'shared by every car' if f else '')

    def set_maker(self):
        t = self.cur(self.rim_list, self.teams)
        car = season_pack.car_of(self.ents or {'cars': []}, t['id']) if t else None
        if car is None:
            return
        makers = season_pack.tyre_makers(self.pack)
        if self.maker_var.get() in makers and season_pack.tyre_maker_of(car, makers) != self.maker_var.get():
            season_pack.set_tyre_maker(car, makers, self.maker_var.get())
            self.log(f'{t.get("name") or t["id"]}: tyre maker {self.maker_var.get()} (Save and Build to apply)')
        self.show_rims()

    def open_tyre_model(self):
        if not self.pack or self.kind != 'season':
            return
        mdir = self.pack / 'WHEELS' / 'model'
        if any(mdir.glob('Wheels*.*')):
            open_path(mdir)
        elif messagebox.askyesno('Content Manager', 'This pack has no 3D tyre model yet. Export it now (OBJ + glTF '
                                 'for Blender: the race wheels and the garage wheels)?'):
            self.run_tool(['wheelmodels', str(self.pack)], lambda: mdir.is_dir() and open_path(mdir))

    def new_maker(self):
        if not self.pack or self.kind != 'season':
            return
        name = self.ask_name('New tyre maker', 'Tyre maker name:')
        if not name:
            return
        like = self.maker_var.get() if self.maker_var.get() in season_pack.TYRE_MAKERS else 'Bridgestone'
        try:
            made = season_pack.new_tyre_maker(self.pack, name, like)
        except ValueError as e:
            messagebox.showerror('Content Manager', str(e))
            return
        self.log(f'tyre maker {name}: {len(made)} sidewall pictures copied from {like} into WHEELS/SideWalls '
                 f'({made[0].name} ... ; Blur1-3 are drawn as the wheels spin faster). Paint them, choose the maker '
                 f'for the teams, then Save and Build')
        self.maker_var.set(name)
        self.set_maker()

    def has_figures(self):
        return any(rel.startswith('DRIVERS/') and '/figure/' in rel for rel in self.manifest.get('files', {}))

    def show_driver(self, pane):
        self.store_forms()
        dr = pane.shown = self.cur(pane.list, pane.rows)
        pane.form.load(dr)
        pane.form2.load(dr)
        self.show_driver_ref(pane)
        if dr:
            base = self.pack / 'DRIVERS' / dr['id']
            pane.imgs[0].show(base / 'Helmet' / 'Colour.png')
            pane.imgs[1].show(base / 'Helmet' / 'Specular.png')
            pane.imgs[2].show(base / 'ui' / 'PreRace.png')
            missing = 'This pack was made before the figures: "Open figure folder" adds them'
            for slot, part in ((pane.imgs[3], '_Body.sg'), (pane.imgs[4], '_Eyes.sg')):
                # the podium body model has only the overalls, the eyes model only the face texture (and normals)
                f = next((self.pack / rel for rel, info in sorted(self.manifest.get('files', {}).items())
                          if rel.startswith(f'DRIVERS/{dr["id"]}/figure/') and not rel.endswith('_n.png')
                          and any(t[1].endswith(part) for t in info['targets'])), None)
                slot.show(f, '' if f else missing if pane is self.drivers_pane and not self.has_figures() else 'no figure')

    def open_figure(self, pane, sub):
        dr = pane.shown
        if not dr:
            return
        folder = self.pack / 'DRIVERS' / dr['id'] / sub
        if folder.is_dir():
            open_path(folder)
        elif pane is self.extra_pane:
            self.log(f'{dr["id"]}: no garage / podium figure')
        elif messagebox.askyesno('Content Manager', 'This pack has no garage / podium figures yet. Add them now '
                                 '(textures' + (' and 3D models' if sub == 'model' else '') + ', every driver)?'):
            self.run_tool(['figures', str(self.pack)] + (['--models'] if sub == 'model' else []),
                          lambda: (self.select_pack(), folder.is_dir() and open_path(folder)))

    def open_sub(self, pane, sub):
        t = pane.shown
        if not t:
            return
        folder = self.pack / 'CARS' / t['id'] / sub
        # the 2006 cars (models <Team>06) have lod1 / lod2; the classics only the close-up model
        lods = (sub == 'model' and season_pack.TEAMS.get(t['id'], '').endswith('06')
                and not any(folder.glob(f'{t["id"]}_lod1.*')))
        if lods and messagebox.askyesno(
                'Content Manager', ('This pack has no 3D models of this car yet.' if not folder.is_dir() else
                                    'This pack has only the close-up (lod0) car models.') +
                ' Export the missing ones now (lod1 / lod2: the cars seen from further away), for every team?'):
            self.run_tool(['carlods', str(self.pack)], lambda: folder.is_dir() and open_path(folder))
        elif folder.is_dir():
            open_path(folder)

    def load_helmets(self):
        self.manifest, self.roles = self.load_json('template.json') or {}, {}
        for w in self.helmet_grid.winfo_children():
            w.destroy()
        self.helmet_slots = []
        dirs = sorted(p for p in self.pack.iterdir() if p.is_dir() and (p / 'Colour.png').exists())
        for i, d in enumerate(dirs):
            s = ImageSlot(self.helmet_grid, self, d.name)
            s.grid(row=i // 6, column=i % 6, padx=4, pady=4)
            s.show(d / 'Colour.png')
            self.helmet_slots.append(s)
        if not dirs:
            ttk.Label(self.helmet_grid, text='No helmet designs in this folder.').grid()

    # ------------------------------------------------------------------ saving
    def store_forms(self):
        """Form edits back into the loaded data (not to disk)."""
        if self.kind != 'season':
            return 0
        n = 0
        for pane in self.team_panes:
            if pane.shown is not None:
                n += pane.form.store()
                n += pane.form2.store(get=lambda d, k: d.get(k.split('.')[0], {}).get(k.split('.')[1]),
                                      put=lambda d, k, v: d.setdefault(k.split('.')[0], {}).__setitem__(
                                          k.split('.')[1], v))
        for pane in self.driver_panes:
            if pane.shown is not None:
                n += pane.form.store()
                n += pane.form2.store()
        return n

    def save(self):
        if self.kind != 'season' or not self.pack:
            return True
        try:
            self.store_forms()
        except ValueError as e:
            messagebox.showerror('Content Manager', str(e))
            return False
        db = self.pack / 'DATABASE'
        if self.ents is not None:
            (db / 'entities.json').write_text(json.dumps(self.ents, indent=1, ensure_ascii=False), encoding='utf-8',
                                              newline='\n')
        if self.dyn is not None:
            (db / 'dynamics.json').write_text(json.dumps(self.dyn, indent=1), encoding='utf-8', newline='\n')
        pts = self.points_var.get().replace(',', ' ').split()
        if pts:
            if not all(p.isdigit() for p in pts):
                messagebox.showerror('Content Manager', 'Points: whole numbers separated by spaces')
                return False
            db.mkdir(parents=True, exist_ok=True)
            (db / 'points.txt').write_text(season_pack.POINTS_TXT.rsplit('\n', 2)[0] + '\n' + ' '.join(pts) + '\n',
                                           encoding='utf-8', newline='\n')
        self.log(f'{self.pack.name}: saved')
        return True

    # ------------------------------------------------------------------ template / build (season_pack.py)
    def run_tool(self, args, done=None):
        if self.busy:
            messagebox.showinfo('Content Manager', 'Still working on the previous task.')
            return
        usr = self.usrdir()
        if not usr:
            messagebox.showerror('Content Manager', 'Choose the game folder (PS3_GAME, with USRDIR/offline.wad).')
            return
        self.save_settings()
        self.busy = True
        cmd = [sys.executable, '-I', '-u', str(TOOLS / 'season_pack.py'), args[0], str(usr)] + args[1:]
        self.bar.configure(mode='indeterminate')
        self.bar.start(15)
        self.status_var.set('Starting...')
        self.log('> ' + ' '.join(args))

        def work():
            try:
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     encoding='utf-8', errors='replace',
                                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                for line in p.stdout:
                    self.q.put(('log', line.rstrip()))
                rc = p.wait()
            except OSError as e:
                self.q.put(('log', str(e)))
                rc = -1
            self.q.put(('done', (rc, done)))
        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        try:
            while True:
                what, val = self.q.get_nowait()
                if what == 'log' and val.startswith('PROGRESS '):
                    frac, _, text = val[9:].partition(' ')
                    a, _, b = frac.partition('/')
                    if self.bar['mode'] != 'determinate':
                        self.bar.stop()
                        self.bar.configure(mode='determinate')
                    self.bar['value'] = 1000 * int(a) / max(1, int(b))
                    self.status_var.set(f'{text} ({a}/{b})')
                elif what == 'log':
                    self.log(val)
                else:
                    rc, done = val
                    self.busy = False
                    self.bar.stop()
                    self.bar.configure(mode='determinate')
                    self.bar['value'] = 1000 if rc == 0 else 0
                    self.status_var.set('Done' if rc == 0 else f'Failed (exit code {rc})')
                    self.log('done' if rc == 0 else f'failed (exit code {rc})')
                    if rc == 0 and done:
                        done()
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def ask_name(self, title, label='Folder name:'):
        win = tk.Toplevel(self)
        win.title(title)
        win.transient(self)
        v = tk.StringVar()
        ttk.Label(win, text=label, padding=8).pack(side='left')
        e = ttk.Entry(win, textvariable=v, width=30)
        e.pack(side='left', padx=4)
        out = []
        ok = lambda *_: (out.append(v.get().strip()), win.destroy())  # noqa: E731
        ttk.Button(win, text='Create', command=ok).pack(side='left', padx=8)
        e.bind('<Return>', ok)
        e.focus_set()
        win.grab_set()
        self.wait_window(win)
        name = out[0] if out else ''
        if name and any(c in name for c in '\\/:*?"<>|'):
            messagebox.showerror('Content Manager', 'Not a valid folder name')
            return ''
        return name

    def new_pack(self, kind, cmd, extra):
        name = self.ask_name('New season pack' if kind == 'season' else 'New player helmet pack')
        if not name:
            return
        dest = self.folders()[kind] / name
        if (dest / 'template.json').exists():
            messagebox.showerror('Content Manager', f'{dest} already exists')
            return
        dest.parent.mkdir(parents=True, exist_ok=True)

        def done():
            self.refresh()
            iid = f'{kind}|{dest}'
            if self.tree.exists(iid):
                self.tree.selection_set(iid)
        self.run_tool([cmd, str(dest)] + extra, done)

    def new_season_pack(self):
        self.new_pack('season', 'template', ['--models'] if self.models_var.get() else [])

    def new_helmet_pack(self):
        self.new_pack('helmet', 'helmets', [])

    def build(self):
        if not self.pack:
            return
        if not (self.pack / 'template.json').exists():
            messagebox.showerror('Content Manager', 'This pack has no template.json (not made by the Content '
                                                    'Manager / season_pack.py): nothing to build.')
            return
        if self.save():
            # reload: a build adds the specular map files to packs made before them
            self.run_tool(['build', str(self.pack)], self.select_pack)

    def open_pack(self):
        if self.pack:
            open_path(self.pack)

    def log(self, text):
        self.logbox.configure(state='normal')
        self.logbox.insert('end', text + '\n')
        self.logbox.see('end')
        self.logbox.configure(state='disabled')


def main(argv):
    app = App(argv[0] if argv else Path.cwd())
    if len(argv) > 1:   # a pack to open: its folder name (season packs first)
        for kind, d in app.folders().items():
            iid = f'{kind}|{d / argv[1]}'
            if app.tree.exists(iid):
                app.tree.selection_set(iid)
                break
    if len(argv) > 2:   # a tab, and a team of it (for tests and shortcuts)
        app.update()
        for tab in app.nb.tabs():
            if app.nb.tab(tab, 'text') == argv[2]:
                app.nb.select(tab)
        if len(argv) > 3:
            for lst, rows, show in [(p.list, p.rows, lambda p=p: app.show_team(p)) for p in app.team_panes] + [
                    (app.rim_list, getattr(app, 'teams', []), app.show_rims)]:
                ids = [t['id'] for t in rows]
                if argv[3] in ids:
                    lst.selection_clear(0, 'end')
                    lst.selection_set(ids.index(argv[3]))
                    show()
    app.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
