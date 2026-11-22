"""Spanwyse: physically aligned spanning wallpapers for monitors with different pixel densities.

Images and monitors are laid out in real millimetres (like LittleBigMouse does for the cursor);
each monitor gets whatever physically sits in front of it, resampled to its own resolution. A
wallpaper holds one or more images (e.g. a pair, one per monitor). The result is a single PNG set
in Windows' "Span" mode.

    python spanwyse.py          # the app
    python spanwyse.py --tray   # start hidden in the tray (used by "Start with Windows")
    python spanwyse.py --test   # checks the alignment math
    python make_icon.py         # regenerates spanwyse.ico
"""
import ctypes
import ctypes.wintypes as w
import functools
import hashlib
import json
import math
import os
import random
import re
import sys
import time
import uuid
import winreg
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk
from tkinter import font as tkfont

import subprocess
import threading

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps, ImageTk

VERSION = '1.0.1'
FROZEN = getattr(sys, 'frozen', False)
# Next to the .exe when packaged (PyInstaller unpacks __file__ into a temp folder).
HERE = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
RES = getattr(sys, '_MEIPASS', HERE)  # bundled read-only files (the icon)
CONFIG = os.path.join(HERE, 'spanwyse.json')
APPLIED = os.path.join(HERE, 'applied')  # the per-monitor images Windows is showing
THUMBS = os.path.join(HERE, 'thumbs')
ICON = os.path.join(RES, 'spanwyse.ico')
THUMB = (176, 100)
IMAGE_EXT = ('.png', '.jpg', '.jpeg', '.bmp', '.webp', '.tif', '.tiff')
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
MODES = ('In order', 'Random', 'Time of day')
FILLS = ('Black fill', 'Blurred fill', 'Custom color…')

# Palette shared with the icon: deep purple ink, cyan -> magenta accents.
INK, PANEL, RAISED, HOVER, BORDER = '#120822', '#1a0f2e', '#261841', '#33214f', '#3a2a5a'
TEXT, MUTED, SELECTED = '#f1ebff', '#9d8fbd', '#3b1f63'
CYAN, PINK, YELLOW, GREEN = '#19e3ff', '#ff2bd6', '#ffd400', '#2ee68a'
PAPER, PAPER_MINOR, PAPER_MAJOR = (20, 10, 38), (32, 20, 54), (48, 32, 80)
FONT = 'Segoe UI Variable Text'

user32 = ctypes.windll.user32
user32.GetParent.argtypes, user32.GetParent.restype = [w.HWND], w.HWND
user32.LoadImageW.argtypes = [w.HINSTANCE, w.LPCWSTR, w.UINT, ctypes.c_int, ctypes.c_int, w.UINT]
user32.LoadImageW.restype = w.HANDLE
user32.MonitorFromPoint.argtypes, user32.MonitorFromPoint.restype = [w.POINT, w.DWORD], w.HMONITOR


class MONITORINFOEX(ctypes.Structure):
    _fields_ = [('cb', w.DWORD), ('rc', w.RECT), ('work', w.RECT), ('flags', w.DWORD), ('dev', w.WCHAR * 32)]


class DISPLAY_DEVICE(ctypes.Structure):
    _fields_ = [('cb', w.DWORD), ('name', w.WCHAR * 32), ('desc', w.WCHAR * 128),
                ('flags', w.DWORD), ('id', w.WCHAR * 128), ('key', w.WCHAR * 128)]


class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [('cbSize', w.DWORD), ('hWnd', w.HWND), ('uID', w.UINT), ('uFlags', w.UINT),
                ('uCallbackMessage', w.UINT), ('hIcon', w.HICON), ('szTip', w.WCHAR * 128), ('dwState', w.DWORD),
                ('dwStateMask', w.DWORD), ('szInfo', w.WCHAR * 256), ('uVersion', w.UINT),
                ('szInfoTitle', w.WCHAR * 64), ('dwInfoFlags', w.DWORD), ('guidItem', ctypes.c_byte * 16),
                ('hBalloonIcon', w.HICON)]


def edid(dev):
    """(stable monitor id, (width, height) in mm from EDID, or None)."""
    dd = DISPLAY_DEVICE(cb=ctypes.sizeof(DISPLAY_DEVICE))
    if not user32.EnumDisplayDevicesW(dev, 0, ctypes.byref(dd), 1):  # 1 = EDD_GET_DEVICE_INTERFACE_NAME
        return dev, None
    parts = dd.id.split('#')  # \\?\DISPLAY#AOC3202#7&2db114b8&0&UID768#{guid}
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             rf'SYSTEM\CurrentControlSet\Enum\DISPLAY\{parts[1]}\{parts[2]}\Device Parameters')
        e = winreg.QueryValueEx(key, 'EDID')[0]
    except (OSError, IndexError):
        return dd.id, None
    mm = (e[66] | (e[68] >> 4) << 8, e[67] | (e[68] & 15) << 8)  # first detailed timing descriptor
    if not all(mm):
        mm = (e[21] * 10, e[22] * 10)  # some monitors only report centimetres
    return dd.id, mm if all(mm) else None


def detect_monitors():
    found = []
    proc = ctypes.WINFUNCTYPE(w.BOOL, w.HMONITOR, w.HDC, ctypes.POINTER(w.RECT), w.LPARAM)

    def cb(h, *_):
        mi = MONITORINFOEX(cb=ctypes.sizeof(MONITORINFOEX))
        user32.GetMonitorInfoW(h, ctypes.byref(mi))
        r = mi.rc
        found.append((mi.dev, [r.left, r.top, r.right - r.left, r.bottom - r.top]))
        return True

    user32.EnumDisplayMonitors(None, None, proc(cb), 0)
    mons = []
    for dev, px in found:
        mid, mm = edid(dev)
        mm = mm or (px[2] * 25.4 / 96, px[3] * 25.4 / 96)  # no EDID: assume 96 dpi
        if (px[2] > px[3]) != (mm[0] > mm[1]):
            mm = mm[::-1]  # rotated to portrait
        mons.append(dict(id=mid, name=dev.lstrip('\\.'), px=px, mm=[0, 0, *mm]))
    # Initial physical position: the Windows layout, scaled by the main monitor's density.
    main = next((m for m in mons if m['px'][:2] == [0, 0]), mons[0])
    k = main['mm'][2] / main['px'][2]
    for m in mons:
        m['mm'][:2] = [m['px'][0] * k, m['px'][1] * k]
    return mons


def signature(mons):
    """Identifies a display layout: which monitors, at which resolution and position."""
    return '|'.join(sorted(f"{m['id']}@{m['px']}" for m in mons))


# --- geometry: rects are (x, y, w, h) in mm; place = (x, y, mm per pixel) of an image ---

def union(rects):
    x0, y0 = min(r[0] for r in rects), min(r[1] for r in rects)
    return x0, y0, max(r[0] + r[2] for r in rects) - x0, max(r[1] + r[3] for r in rects) - y0


def fit_view(rects, size, margin=1.15):
    """(origin x, origin y, pixels per mm) so all rects fit in `size`."""
    x, y, rw, rh = union(rects)
    k = min(size[0] / (rw * margin), size[1] / (rh * margin))
    return x + rw / 2 - size[0] / 2 / k, y + rh / 2 - size[1] / 2 / k, k


def box(r, view):
    ox, oy, k = view
    return [round((r[0] - ox) * k), round((r[1] - oy) * k), round((r[0] + r[2] - ox) * k), round((r[1] + r[3] - oy) * k)]


# --- layers: the original file, then flip/rotate (xf), then an optional crop (pieces of a split image) ---

ROT = {1: Image.ROTATE_270, 2: Image.ROTATE_180, 3: Image.ROTATE_90}  # quarter turns clockwise


_OPENED = {}  # (path, modified time): decoded image; an 8K JPEG takes ~0.3 s to decode, and pairs often reuse one


def open_image(path):
    key = (path, os.path.getmtime(path))
    im = _OPENED.pop(key, None)
    if im is None:
        im = Image.open(path)
        im.load()
        if im.mode not in ('RGB', 'RGBA'):  # palette, greyscale, CMYK...: keep transparency if there is any
            im = im.convert('RGBA' if 'A' in im.getbands() or 'transparency' in im.info else 'RGB')
    _OPENED[key] = im
    while len(_OPENED) > 4:
        _OPENED.pop(next(iter(_OPENED)))
    return im


def rotated_size(w, h, deg):
    """The size Pillow's rotate(-deg, expand=True) gives (same arithmetic), without rotating anything."""
    a = -math.radians(-deg % 360.0)
    m = [round(math.cos(a), 15), round(math.sin(a), 15), 0.0, round(-math.sin(a), 15), round(math.cos(a), 15), 0.0]

    def tf(x, y):
        return m[0] * x + m[1] * y + m[2], m[3] * x + m[4] * y + m[5]

    m[2], m[5] = tf(-w / 2.0, -h / 2.0)
    m[2] += w / 2.0
    m[5] += h / 2.0
    xs, ys = zip(*(tf(x, y) for x, y in ((0, 0), (w, 0), (w, h), (0, h))))
    return math.ceil(max(xs)) - math.floor(min(xs)), math.ceil(max(ys)) - math.floor(min(ys))


def set_xf(L, xf, crop=None, angle=None):
    """The layer's pixels: original -> flip/quarter turns (xf) -> crop (the `base`), then a free rotation in
    degrees (clockwise, transparent corners; None keeps the current angle). The base and its preview are
    rebuilt only when xf or crop change."""
    rot, flip = xf
    crop = crop and list(crop)
    if 'base' not in L or L['xf'] != [rot, flip] or L['crop'] != crop:
        img = L['orig'].transpose(Image.FLIP_LEFT_RIGHT) if flip else L['orig']
        img = img.transpose(ROT[rot]) if rot else img
        L['xf'], L['crop'], L['full'] = [rot, flip], crop, img.size
        L['base'] = img.crop(crop) if crop else img
        L['base_preview'] = L['base'].copy()
        L['base_preview'].thumbnail((1600, 1600))
    set_rotation(L, L.get('angle', 0) if angle is None else angle)


def set_rotation(L, angle, fast=False):
    """Only the 1600 px preview is rotated here; source() rotates the full image when it is actually drawn
    at full size, since an 8K rotation takes seconds. `fast` (while a slider is dragged) turns the preview
    with the nearest pixel, 5 ms instead of 100; it is redone smoothly when the drag settles."""
    L['angle'] = angle
    L.pop('rotated', None)
    L.pop('rotating', None)
    L['size'] = rotated_size(*L['base'].size, angle) if angle else L['base'].size
    bp = L['base_preview']
    L['rough'] = bool(angle and fast)
    L['preview'] = (bp.convert('RGBA').rotate(-angle, Image.NEAREST if fast else Image.BICUBIC, expand=True)
                    if angle else bp)
    L['pratio'] = L['size'][0] / L['preview'].width


def source(L):
    """The layer's full-resolution pixels, rotated on first use and kept until the angle changes."""
    if not L['angle']:
        return L['base']
    if L.get('rotated', (None,))[0] != L['angle']:
        L['rotated'] = (L['angle'], L['base'].convert('RGBA').rotate(-L['angle'], Image.BICUBIC, expand=True))
    return L['rotated'][1]


def source_soon(L):
    """For the editor: the full-resolution pixels if they are ready, else None while a thread rotates them
    (Pillow lets go of the GIL while it works, so the window stays responsive meanwhile)."""
    if not L['angle']:
        return L['base']
    angle = L['angle']
    if L.get('rotated', (None,))[0] == angle:
        return L['rotated'][1]
    if L.get('rotating') != angle:
        L['rotating'] = angle

        def work(base=L['base']):
            img = base.convert('RGBA').rotate(-angle, Image.BICUBIC, expand=True)
            if L['angle'] == angle and L['base'] is base:  # still wanted
                L['rotated'] = (angle, img)
        threading.Thread(target=work, daemon=True).start()
    return None


def load_layer(path, place=None, xf=(0, False), crop=None, adj=None, angle=0):
    L = dict(path=path, place=place, orig=open_image(path), adj=dict(adj or {}))
    set_xf(L, xf, crop, angle)
    return L


def turn(L, op):
    """Rotate a quarter turn clockwise ('rot') or mirror ('flip') the layer's full image, keeping any crop
    on the same pixels and the layer's centre where it was."""
    rot, flip = L['xf']
    W, H = L['full']
    c = L['crop']
    if op == 'rot':
        xf, crop = [(rot + 1) % 4, flip], c and [H - c[3], c[0], H - c[1], c[2]]
    else:  # mirroring after rotating r == mirroring first, then rotating -r
        xf, crop = [(-rot) % 4, not flip], c and [W - c[2], c[1], W - c[0], c[3]]
    x, y, rw, rh = layer_rect(L)
    set_xf(L, xf, crop)
    s = L['place'][2]
    L['place'] = [x + rw / 2 - L['size'][0] * s / 2, y + rh / 2 - L['size'][1] * s / 2, s]


def seams(mons, a):
    """Middle of every seam on axis a: 0 = between side-by-side monitors, 1 = between stacked ones.
    Only neighbours that overlap on the other axis count, so a third monitor further away adds no cut."""
    b, out = 1 - a, set()
    for m in mons:
        r = m['mm']
        beyond = [n['mm'] for n in mons if n['mm'][a] >= r[a] + r[a + 2] - 1
                  and n['mm'][b] < r[b] + r[b + 2] and r[b] < n['mm'][b] + n['mm'][b + 2]]
        if beyond:
            out.add((r[a] + r[a + 2] + min(q[a] for q in beyond)) / 2)
    return out


def set_angle(L, angle, fast=False):
    """Free rotation around the image's centre, keeping its scale."""
    x, y, rw, rh = layer_rect(L)
    set_rotation(L, angle, fast)
    s = L['place'][2]
    L['place'] = [x + rw / 2 - L['size'][0] * s / 2, y + rh / 2 - L['size'][1] * s / 2, s]


def split(L, mons):
    """Cut a layer at the seams between monitors (side by side and stacked); each piece keeps its share
    of the image (beyond the cut too) and can then be moved on its own. Tilted images aren't split."""
    if L.get('angle'):
        return [L]
    x, y, s = L['place']
    w_, h_ = L['size']
    cols = sorted({0, w_, *(round((c - x) / s) for c in seams(mons, 0) if 0 < (c - x) / s < w_)})
    rows = sorted({0, h_, *(round((c - y) / s) for c in seams(mons, 1) if 0 < (c - y) / s < h_)})
    if len(cols) + len(rows) < 5:
        return [L]
    ox, oy = (L['crop'] or (0, 0))[:2]
    pieces = []
    for r0, r1 in zip(rows, rows[1:]):
        for c0, c1 in zip(cols, cols[1:]):
            P = dict(path=L.get('path'), orig=L['orig'], place=[x + c0 * s, y + r0 * s, s], adj=dict(L.get('adj', {})))
            set_xf(P, L['xf'], [ox + c0, oy + r0, ox + c1, oy + r1])
            pieces.append(P)
    return pieces


def layer_rect(L):
    x, y, s = L['place']
    return x, y, L['size'][0] * s, L['size'][1] * s


def cover(L, rect):
    """Scale and centre the image so it fully covers `rect`."""
    x, y, rw, rh = rect
    iw, ih = L['size']
    s = max(rw / iw, rh / ih)
    L['place'] = [x + (rw - iw * s) / 2, y + (rh - ih * s) / 2, s]


def render(src, place, rect, size, resample):
    """The part of `src` that falls on `rect` (drawn at `size` pixels), as (image, (x, y) to paste it at),
    or None if none of it does. Only the visible part is resampled and returned, so the cost follows what
    is on screen, not the size of the source or of the canvas."""
    ix, iy, s = place
    x, y, rw, rh = rect
    kx, ky = size[0] / rw, size[1] / rh  # output pixels per mm
    c = (max((x - ix) / s, 0), max((y - iy) / s, 0),
         min((x + rw - ix) / s, src.width), min((y + rh - iy) / s, src.height))  # in source pixels
    if c[0] >= c[2] or c[1] >= c[3]:
        return None
    d = [round((c[0] * s + ix - x) * kx), round((c[1] * s + iy - y) * ky),
         round((c[2] * s + ix - x) * kx), round((c[3] * s + iy - y) * ky)]
    # Crop to whole pixels around the box first (cheap), then resample with the sub-pixel box inside that:
    # same result, without the resampler walking a whole 8K source when only a corner is on screen.
    x0, y0 = max(math.floor(c[0]) - 2, 0), max(math.floor(c[1]) - 2, 0)
    part = src.crop((x0, y0, min(math.ceil(c[2]) + 2, src.width), min(math.ceil(c[3]) + 2, src.height)))
    d[0], d[1] = max(d[0], 0), max(d[1], 0)
    return part.resize((max(min(d[2], size[0]) - d[0], 1), max(min(d[3], size[1]) - d[1], 1)), resample,
                       box=(c[0] - x0, c[1] - y0, c[2] - x0, c[3] - y0)), (d[0], d[1])


ADJUSTMENTS = (('brightness', 'Brightness', ImageEnhance.Brightness), ('contrast', 'Contrast', ImageEnhance.Contrast),
               ('saturation', 'Saturation', ImageEnhance.Color))


def adjust(img, adj):
    """Brightness / contrast / saturation, each -100..100 (0 = unchanged), on an RGBA image; alpha untouched.
    Applied to the already-resampled output, so it costs the same at any zoom."""
    if not adj or not any(adj.values()):
        return img
    rgb = img.convert('RGB') if img.mode != 'RGB' else img
    for key, _, enhancer in ADJUSTMENTS:
        if adj.get(key):
            rgb = enhancer(rgb).enhance(1 + adj[key] / 100)
    if img.mode == 'RGBA':
        rgb.putalpha(img.getchannel('A'))
    return rgb


def paint(layers, rect, size, preview=False, base=None, fast=False):
    """All the wallpaper's images (last on top) over the physical `rect`, on `base` (default black).
    `fast` resamples with the nearest pixel (about 6x quicker) for frames drawn while something moves."""
    out = base.copy() if base else Image.new('RGBA', size, (0, 0, 0, 255))
    quick = Image.NEAREST if fast else Image.BILINEAR
    for L in layers:
        x, y, s = L['place']
        if preview and size[0] / rect[2] * s * L['pratio'] <= 1:  # preview is sharp enough; at high zoom use source
            part = render(L['preview'], (x, y, s * L['pratio']), rect, size, quick)
        else:
            part = render(source(L), (x, y, s), rect, size, quick if preview else Image.LANCZOS)
        if part:
            img, at = part
            img = adjust(img, L.get('adj'))
            if img.mode == 'RGBA':  # tilted or transparent: blend
                out.alpha_composite(img, at)
            else:  # opaque: a plain copy, much cheaper than blending
                out.paste(img, at)
    return out


def paint_editor(layers, rect, size, fast=False):
    """The images over `rect` as an RGB picture plus a mask of where they are: the editor's cheap form
    (plain copies instead of blending whole canvases). Full-resolution pixels are used only when zoomed past
    the preview, never while something moves, and a tilted one only once its rotation is ready."""
    rgb, mask = Image.new('RGB', size), Image.new('L', size, 0)
    quick = Image.NEAREST if fast else Image.BILINEAR
    for L in layers:
        x, y, s = L['place']
        src = source_soon(L) if not fast and size[0] / rect[2] * s * L['pratio'] > 1 else None
        if src is not None:
            part = render(src, (x, y, s), rect, size, quick)
        else:
            part = render(L['preview'], (x, y, s * L['pratio']), rect, size, quick)
        if not part:
            continue
        img, at = part
        img = adjust(img, L.get('adj'))
        where = (at[0], at[1], at[0] + img.width, at[1] + img.height)
        if img.mode == 'RGBA':  # tilted or transparent: only where it has pixels
            alpha = img.getchannel('A')
            rgb.paste(img.convert('RGB'), at, alpha)
            mask.paste(255, where, alpha)
        else:
            rgb.paste(img, at)
            mask.paste(255, where)
    return rgb, mask


def backdrop(layers, rect, size, fill, part_of=None):
    """What shows on a monitor where no image covers it: a colour, or the bottom image blurred to fill it.
    `rect` is the area drawn at `size`; `part_of` the whole monitor when only part of it is on screen."""
    if fill == 'blur' and layers:
        L = dict(layers[0])
        cover(L, part_of or rect)
        x, y, s = L['place']
        small = (max(size[0] // 16, 1), max(size[1] // 16, 1))
        img = Image.new('RGBA', small, (0, 0, 0, 255))
        part = render(L['preview'], (x, y, s * L['pratio']), rect, small, Image.BILINEAR)
        if part:
            img.paste(*part)
        return img.filter(ImageFilter.GaussianBlur(4)).resize(size, Image.BILINEAR)
    return Image.new('RGBA', size, fill if fill.startswith('#') else '#000000')


def compose(layers, mons, fill='#000000'):
    """The final wallpaper, sized to the bounding box of the virtual desktop (what Span mode expects)."""
    x0, y0, vw, vh = union([m['px'] for m in mons])
    out = Image.new('RGB', (vw, vh))
    for m in mons:
        size = tuple(m['px'][2:])
        out.paste(paint(layers, m['mm'], size, base=backdrop(layers, m['mm'], size, fill)).convert('RGB'),
                  (m['px'][0] - x0, m['px'][1] - y0))
    return out


@functools.lru_cache(maxsize=4)
def graph_paper(view, size, step=10):
    """Background grid in real millimetres: every `step` and every 10 steps, thinned out when zoomed far out.
    Cached (moving an image doesn't move the grid); callers must not draw on the image it returns."""
    ox, oy, k = view
    img = Image.new('RGB', size, PAPER)
    d = ImageDraw.Draw(img)
    for step, color in ((step, PAPER_MINOR), (step * 10, PAPER_MAJOR)):
        if step * k < 6:
            continue
        for a in (0, 1):
            o = (ox, oy)[a]
            for n in range(math.floor(o / step), math.ceil((o + size[a] / k) / step) + 1):
                p = round((n * step - o) * k)
                d.line((p, 0, p, size[1]) if a == 0 else (0, p, size[0], p), fill=color)
    return img


def preview_image(layers, mons, view, size, paper=True, step=10, fill='#000000', fast=False):
    """Monitors show exactly what they'll display; image parts off-screen are ghosted over the grid."""
    ox, oy, k = view
    lay = paint_editor(layers, (ox, oy, size[0] / k, size[1] / k), size, fast) if layers else None
    if not paper:
        img = Image.new('RGB', size, PANEL)
    elif lay:  # the images at 40 % over the grid, only where there are images (and only that box is blended)
        grid = graph_paper(tuple(view), tuple(size), step)
        img = grid.copy()
        bb = lay[1].getbbox()
        if bb:
            img.paste(Image.blend(grid.crop(bb), lay[0].crop(bb), 0.4), bb[:2], lay[1].crop(bb))
    else:
        img = graph_paper(tuple(view), tuple(size), step).copy()
    for m in mons:
        b = box(m['mm'], view)
        # Only the part of the monitor that is on the canvas: zoomed in, a monitor can be 7000 px wide.
        v = [max(b[0], 0), max(b[1], 0), min(b[2], size[0]), min(b[3], size[1])]
        if v[0] >= v[2] or v[1] >= v[3]:
            continue
        if lay and lay[1].crop(v).getextrema() == (255, 255):  # fully covered: just the pixels, no backdrop
            img.paste(lay[0].crop(v), v[:2])
            continue
        seen = (ox + v[0] / k, oy + v[1] / k, (v[2] - v[0]) / k, (v[3] - v[1]) / k)
        screen = backdrop(layers, seen, (v[2] - v[0], v[3] - v[1]), fill, part_of=m['mm']).convert('RGB')
        if lay:
            screen.paste(lay[0].crop(v), (0, 0), lay[1].crop(v))
        img.paste(screen, v[:2])
    return img


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [('dwSize', w.DWORD), ('cntUsage', w.DWORD), ('th32ProcessID', w.DWORD),
                ('th32DefaultHeapID', ctypes.c_size_t), ('th32ModuleID', w.DWORD), ('cntThreads', w.DWORD),
                ('th32ParentProcessID', w.DWORD), ('pcPriClassBase', ctypes.c_long), ('dwFlags', w.DWORD),
                ('szExeFile', w.WCHAR * 260)]


kernel32 = ctypes.windll.kernel32
kernel32.CreateToolhelp32Snapshot.argtypes, kernel32.CreateToolhelp32Snapshot.restype = [w.DWORD, w.DWORD], w.HANDLE
kernel32.Process32FirstW.argtypes = kernel32.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
kernel32.OpenProcess.argtypes, kernel32.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
kernel32.CloseHandle.argtypes = [w.HANDLE]


def processes():
    """{exe name in lower case: pid} of everything running."""
    snap, out = kernel32.CreateToolhelp32Snapshot(2, 0), {}  # TH32CS_SNAPPROCESS
    e = PROCESSENTRY32(dwSize=ctypes.sizeof(PROCESSENTRY32))
    ok = kernel32.Process32FirstW(snap, ctypes.byref(e))
    while ok:
        out[e.szExeFile.lower()] = e.th32ProcessID
        ok = kernel32.Process32NextW(snap, ctypes.byref(e))
    kernel32.CloseHandle(snap)
    return out


def process_path(pid):
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    buf, size = ctypes.create_unicode_buffer(1024), w.DWORD(1024)
    ok = kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
    kernel32.CloseHandle(h)
    return buf.value if ok else None


def windows_dark():
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize')
        return winreg.QueryValueEx(key, 'AppsUseLightTheme')[0] == 0
    except OSError:
        return False


def stop_wallpaper_engine(procs):
    """Wallpaper Engine draws over the Windows wallpaper; its documented '-control stop' hides it.
    Returns True if it was running."""
    for exe in ('wallpaper64.exe', 'wallpaper32.exe'):
        path = exe in procs and process_path(procs[exe])
        if path:
            subprocess.Popen([path, '-control', 'stop'], creationflags=0x08000000)  # CREATE_NO_WINDOW
            return True
    return False


def context_match(presets, procs, dark):
    """Index of the wallpaper whose rule holds right now: running apps first (list order), then the
    Windows theme. None if no rule holds."""
    for kind in ('app', 'theme'):
        for i, p in enumerate(presets):
            rule = p.get('when') or {}
            if kind == 'app' and rule.get('app', '').lower() in procs:
                return i
            if kind == 'theme' and rule.get('theme') and (rule['theme'] == 'dark') == dark:
                return i
    return None


def pick_by_time(presets, now):
    """The wallpaper whose start time ('HH:MM') was most recently passed; before the first one of the day,
    yesterday's last one. None if no wallpaper has a time."""
    timed = sorted((p['time'], i) for i, p in enumerate(presets) if p.get('time'))
    if not timed:
        return None
    return ([t for t in timed if t[0] <= now] or timed)[-1][1]


class GUID(ctypes.Structure):
    _fields_ = [('a', ctypes.c_ulong), ('b', ctypes.c_ushort), ('c', ctypes.c_ushort), ('d', ctypes.c_ubyte * 8)]


def guid(s):
    g = GUID()
    ctypes.oledll.ole32.CLSIDFromString(s, ctypes.byref(g))
    return g


class DesktopWallpaper:
    """Windows' per-monitor wallpaper API (IDesktopWallpaper), via raw COM. The registry "Span" style is
    ignored once Windows 11 has per-monitor wallpapers (it then fills each monitor with the whole image),
    so every monitor gets its own image at its native resolution instead: Fill at native size is 1:1.
    Monitor ids here are the same device interface paths detect_monitors() uses."""
    FILL = 4

    def __init__(self):
        ole32 = ctypes.oledll.ole32
        ole32.CoInitialize(None)  # S_FALSE if Tk already initialised COM on this thread; that's fine
        self.p = ctypes.c_void_p()
        ole32.CoCreateInstance(ctypes.byref(guid('{C2CF3110-460E-4fc1-B9D0-8A1C0C9CC4BD}')), None, 0x17,
                               ctypes.byref(guid('{B92B56A9-8B55-4E14-9A89-0199BBB6F93B}')), ctypes.byref(self.p))
        self.vt = ctypes.cast(self.p, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]

    def call(self, index, *args):
        """vtable slot `index` (after IUnknown's 3); args are (ctype, value) pairs. Raises OSError on failure."""
        fn = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *(t for t, _ in args))(self.vt[index])
        fn(self.p, *(v for _, v in args))

    def ids(self):
        n = ctypes.c_uint()
        self.call(6, (ctypes.POINTER(ctypes.c_uint), ctypes.byref(n)))
        out = []
        for i in range(n.value):
            mid = ctypes.c_wchar_p()
            self.call(5, (ctypes.c_uint, i), (ctypes.POINTER(ctypes.c_wchar_p), ctypes.byref(mid)))
            out.append(mid.value)
        return out

    def get(self, mid):
        path = ctypes.c_wchar_p()
        self.call(4, (ctypes.c_wchar_p, mid), (ctypes.POINTER(ctypes.c_wchar_p), ctypes.byref(path)))
        return path.value or ''

    def set(self, mid, path):
        self.call(3, (ctypes.c_wchar_p, mid), (ctypes.c_wchar_p, path))

    def position(self, value=None):
        if value is not None:
            self.call(10, (ctypes.c_int, value))
            return value
        pos = ctypes.c_int()
        self.call(11, (ctypes.POINTER(ctypes.c_int), ctypes.byref(pos)))
        return pos.value

    def snapshot(self):
        return self.position(), {mid: self.get(mid) for mid in self.ids()}

    def restore(self, snap):
        pos, paths = snap
        self.position(pos)
        for mid, path in paths.items():
            if path and mid in self.ids():
                self.set(mid, path)

    def show(self, files):
        """files: {monitor id: image at that monitor's native resolution}. Returns ids Windows doesn't know."""
        known = self.ids()
        self.position(self.FILL)
        for mid, path in files.items():
            if mid in known:
                self.set(mid, path)
        return [mid for mid in files if mid not in known]


for _name, _args, _res in (('FindWindowW', [w.LPCWSTR, w.LPCWSTR], w.HWND), ('GetWindow', [w.HWND, w.UINT], w.HWND),
                           ('GetWindowLongW', [w.HWND, ctypes.c_int], ctypes.c_long),
                           ('SetWindowLongW', [w.HWND, ctypes.c_int, ctypes.c_long], ctypes.c_long),
                           ('SetWindowPos', [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                             w.UINT], w.BOOL)):
    getattr(user32, _name).argtypes, getattr(user32, _name).restype = _args, _res


def fade_cover(root, mons, shown):
    """Smooth transitions: one borderless window per monitor showing what it displays now, placed just
    above the desktop (under every app), click-through and never focused. Set the new wallpaper under it,
    then fade_out() it. Returns [] (no fade) if the desktop window can't be found."""
    above = user32.GetWindow(user32.FindWindowW('Progman', None) or 0, 3)  # the window right above the desktop
    if not above:
        return []
    wins = []
    for m in mons:
        x, y, pw, ph = m['px']
        try:
            img = ImageOps.fit(Image.open(shown[m['id']]).convert('RGB'), (pw, ph))  # what Fill shows
        except (KeyError, OSError):
            continue
        t = tk.Toplevel(root, bg='black')
        t.overrideredirect(True)
        t.attributes('-alpha', 0.0)  # invisible until it sits at the right depth
        t.geometry(f'{pw}x{ph}+{x}+{y}')
        t.photo = ImageTk.PhotoImage(img)
        tk.Label(t, image=t.photo, bd=0).pack()
        t.update()
        hwnd = user32.GetParent(t.winfo_id())
        user32.SetWindowLongW(hwnd, -20, user32.GetWindowLongW(hwnd, -20) | 0x08000000 | 0x80 | 0x20)
        user32.SetWindowPos(hwnd, above, 0, 0, 0, 0, 0x13)  # NOACTIVATE | NOMOVE | NOSIZE, below `above`
        t.attributes('-alpha', 1.0)
        wins.append(t)
    root.update()
    return wins


def fade_out(root, wins, wait=700, duration=900, steps=24):
    """Give Windows `wait` ms to draw the new wallpaper, then fade the cover away."""
    def step(i):
        for t in wins:
            if i > steps:
                t.destroy()
            else:
                t.attributes('-alpha', 1 - i / steps)
        if i <= steps:
            root.after(duration // steps, step, i + 1)
    if wins:
        root.after(wait, step, 1)


def write_monitor_images(layers, mons, fill, prefix):
    """One PNG per monitor in APPLIED, with a fresh name each time (Windows caches by path)."""
    os.makedirs(APPLIED, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S') + f'-{uuid.uuid4().hex[:4]}'
    files = {}
    for m in mons:
        size = tuple(m['px'][2:])
        path = os.path.join(APPLIED, f'{prefix}{m["name"]}_{stamp}.png')
        paint(layers, m['mm'], size, base=backdrop(layers, m['mm'], size, fill)).convert('RGB').save(
            path, compress_level=1)
        files[m['id']] = path
    return files


def clean_applied(keep):
    """Delete older generated images, keeping the ones Windows currently shows."""
    keep = {os.path.normcase(p) for p in keep}
    for name in os.listdir(APPLIED):
        path = os.path.join(APPLIED, name)
        if os.path.normcase(path) not in keep:
            try:
                os.remove(path)
            except OSError:  # still open somewhere; next apply tries again
                pass


def native_hooks(root, handler):
    """Subclass the Tk window to receive file drops, global hotkeys, tray clicks and display changes.
    Events are queued and handled from Tk's loop: calling Tk inside the window proc crashes."""
    shell32, comctl = ctypes.windll.shell32, ctypes.windll.comctl32
    shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, w.UINT, w.LPWSTR, w.UINT]
    shell32.DragFinish.argtypes = [ctypes.c_void_p]
    proc_t = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM, ctypes.c_size_t, ctypes.c_size_t)
    comctl.SetWindowSubclass.argtypes = [w.HWND, proc_t, ctypes.c_size_t, ctypes.c_size_t]
    comctl.DefSubclassProc.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
    comctl.DefSubclassProc.restype = ctypes.c_ssize_t
    taskbar_created = user32.RegisterWindowMessageW('TaskbarCreated')  # Explorer restarted: re-add tray icon
    events = []

    def proc(hwnd, msg, wp, lp, *_):
        if msg == 0x233:  # WM_DROPFILES
            paths = []
            for i in range(shell32.DragQueryFileW(wp, 0xFFFFFFFF, None, 0)):
                buf = ctypes.create_unicode_buffer(shell32.DragQueryFileW(wp, i, None, 0) + 1)
                shell32.DragQueryFileW(wp, i, buf, len(buf))
                paths.append(buf.value)
            shell32.DragFinish(wp)
            events.append(('drop', paths))
            return 0
        if msg == 0x312:  # WM_HOTKEY
            events.append(('hotkey', wp))
            return 0
        if msg == TRAY_MSG:
            events.append(('tray', lp & 0xFFFF))
            return 0
        if msg == 0x7E:  # WM_DISPLAYCHANGE
            events.append(('display', None))
        elif msg == taskbar_created:
            events.append(('tray_readd', None))
        return comctl.DefSubclassProc(hwnd, msg, wp, lp)

    def poll():
        while events:
            handler(*events.pop(0))
        root.after(150, poll)

    poll()
    root.update_idletasks()
    hwnd = user32.GetParent(root.winfo_id())  # the native window wrapping Tk
    root._native_proc = proc_t(proc)  # keep a reference for the window's lifetime
    comctl.SetWindowSubclass(hwnd, root._native_proc, 1, 0)
    shell32.DragAcceptFiles(hwnd, True)
    return hwnd


TRAY_MSG = 0x8001  # WM_APP + 1


def dark_titlebar(win):
    """Windows 11 dark title bar tinted to the app background; silently ignored on older Windows."""
    win.update_idletasks()
    hwnd = user32.GetParent(win.winfo_id())
    dwm = ctypes.windll.dwmapi
    for attr, value in ((20, 1), (35, int(INK[5:7] + INK[3:5] + INK[1:3], 16))):  # dark mode, caption COLORREF
        v = ctypes.c_int(value)
        dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), 4)


def apply_theme(root):
    """Flat dark ttk theme; returns the icon glyph font (Fluent on Windows 11, MDL2 on 10)."""
    st = ttk.Style(root)
    st.theme_use('clam')
    root.configure(bg=INK)
    base = (FONT, 10)
    st.configure('.', background=INK, foreground=TEXT, font=base, bordercolor=INK, lightcolor=INK, darkcolor=INK,
                 troughcolor=PANEL, focuscolor=PINK, selectbackground=SELECTED, selectforeground=TEXT,
                 insertcolor=TEXT, fieldbackground=RAISED)
    st.configure('Side.TFrame', background=PANEL)
    st.configure('Side.TLabel', background=PANEL)
    st.configure('Muted.TLabel', background=PANEL, foreground=MUTED, font=(FONT, 9))
    st.configure('Title.TLabel', background=PANEL, font=('Segoe UI Variable Display', 16, 'bold'))
    st.configure('Head.TLabel', background=PANEL, foreground=MUTED, font=(FONT, 9, 'bold'))
    st.configure('Status.TLabel', background=INK, foreground=MUTED, font=(FONT, 9))
    st.configure('Bar.TLabel', background=INK, foreground=MUTED)
    st.configure('TButton', background=RAISED, foreground=TEXT, borderwidth=0, padding=(14, 7), relief='flat')
    st.map('TButton', background=[('pressed', BORDER), ('active', HOVER)])
    st.configure('Accent.TButton', background=PINK, foreground='white', font=(FONT, 10, 'bold'), padding=(18, 7))
    st.map('Accent.TButton', background=[('pressed', '#c81fa6'), ('active', '#ff55e0')])
    glyphs = 'Segoe Fluent Icons' if 'Segoe Fluent Icons' in tkfont.families(root) else 'Segoe MDL2 Assets'
    st.configure('Icon.TButton', background=PANEL, foreground=MUTED, font=(glyphs, 11), padding=(7, 5))
    st.map('Icon.TButton', background=[('pressed', BORDER), ('active', HOVER)], foreground=[('active', TEXT)])
    st.configure('Glyph.TButton', font=(glyphs, 11), padding=(10, 8))
    st.configure('Treeview', background=PANEL, fieldbackground=PANEL, foreground=TEXT, borderwidth=0,
                 rowheight=THUMB[1] + 14, font=base)
    st.map('Treeview', background=[('selected', SELECTED)], foreground=[('selected', 'white')])
    st.layout('Treeview', [('Treeview.treearea', {'sticky': 'nswe'})])  # no border
    st.configure('TCheckbutton', background=PANEL, foreground=TEXT, indicatorbackground=RAISED,
                 indicatorforeground='white', indicatormargin=(0, 0, 6, 0))
    st.map('TCheckbutton', background=[('active', PANEL)], indicatorbackground=[('selected', PINK)])
    st.configure('Bar.TCheckbutton', background=INK)
    st.map('Bar.TCheckbutton', background=[('active', INK)])
    st.configure('TSpinbox', fieldbackground=RAISED, background=RAISED, foreground=TEXT, arrowcolor=MUTED,
                 borderwidth=0, padding=4)
    st.configure('TCombobox', fieldbackground=RAISED, background=RAISED, foreground=TEXT, arrowcolor=MUTED,
                 borderwidth=0, padding=4)
    st.map('TCombobox', fieldbackground=[('readonly', RAISED)], foreground=[('readonly', TEXT)],
           selectbackground=[('readonly', RAISED)], selectforeground=[('readonly', TEXT)])
    root.option_add('*TCombobox*Listbox.background', RAISED)
    root.option_add('*TCombobox*Listbox.foreground', TEXT)
    root.option_add('*TCombobox*Listbox.selectBackground', SELECTED)
    root.option_add('*TCombobox*Listbox.font', base)
    st.configure('Horizontal.TScale', background=MUTED, troughcolor=RAISED, borderwidth=0, sliderlength=14,
                 lightcolor=MUTED, darkcolor=MUTED, bordercolor=MUTED, troughrelief='flat', gripcount=0)
    st.map('Horizontal.TScale', background=[('active', PINK)], lightcolor=[('active', PINK)],
           darkcolor=[('active', PINK)], bordercolor=[('active', PINK)])
    st.configure('TEntry', fieldbackground=RAISED, foreground=TEXT, borderwidth=0, padding=6)
    st.configure('Vertical.TScrollbar', background=RAISED, troughcolor=PANEL, borderwidth=0, arrowcolor=MUTED,
                 gripcount=0)
    st.map('Vertical.TScrollbar', background=[('active', HOVER)])
    st.configure('TPanedwindow', background=PANEL)
    st.configure('Sash', sashthickness=8, background=PANEL, gripcount=0)
    return glyphs


class Popup:
    """App-styled popup menu (Tk menus are native and ignore the theme on Windows). Same calls as tk.Menu:
    add_command / add_separator / tk_popup. 'Label\\tShortcut' puts the shortcut on the right."""

    def __init__(self, root):
        self.root, self.items, self.win = root, [], None

    def add_command(self, label, command, font=None):
        self.items.append((label, command, font))

    def add_separator(self):
        self.items.append(None)

    def tk_popup(self, x, y):
        self.win = m = tk.Toplevel(self.root, bg=BORDER)
        m.overrideredirect(True)
        m.attributes('-topmost', True)
        body = tk.Frame(m, bg=PANEL, padx=6, pady=6)
        body.pack(padx=1, pady=1)
        body.columnconfigure(0, weight=1)
        for r, item in enumerate(self.items):
            if item is None:
                tk.Frame(body, bg=BORDER, height=1).grid(row=r, column=0, columnspan=2, sticky='ew', padx=8, pady=5)
                continue
            label, command, font = item
            text, _, keys = label.partition('\t')
            cells = [tk.Label(body, text=text, bg=PANEL, fg=TEXT, font=font or (FONT, 10), anchor='w', padx=14, pady=7),
                     tk.Label(body, text=keys, bg=PANEL, fg=MUTED, font=(FONT, 9), anchor='e', padx=14, pady=7)]
            cells[0].grid(row=r, column=0, sticky='ew')
            cells[1].grid(row=r, column=1, sticky='ew')
            for c in cells:
                c.bind('<Enter>', lambda e, cs=cells: [x.config(bg=SELECTED) for x in cs])
                c.bind('<Leave>', lambda e, cs=cells: [x.config(bg=PANEL) for x in cs])
                c.bind('<ButtonRelease-1>', lambda e, cmd=command: (self.close(), cmd()))
        m.update_idletasks()
        # Keep it inside the work area of the monitor under the pointer; above the pointer if it would run off
        # the bottom (the tray case).
        mi = MONITORINFOEX(cb=ctypes.sizeof(MONITORINFOEX))
        user32.GetMonitorInfoW(user32.MonitorFromPoint(w.POINT(x, y), 2), ctypes.byref(mi))
        wa, mw, mh = mi.work, m.winfo_reqwidth(), m.winfo_reqheight()
        x = max(wa.left, min(x, wa.right - mw))
        y = y - mh if y + mh > wa.bottom else y
        m.geometry(f'+{x}+{max(wa.top, y)}')
        hwnd = user32.GetParent(m.winfo_id())
        corner = ctypes.c_int(2)  # Windows 11 rounded corners
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner), 4)
        user32.SetForegroundWindow(hwnd)  # needed so clicking elsewhere (even from the tray) closes it
        m.focus_force()
        m.bind('<FocusOut>', lambda e: self.close())
        m.bind('<Escape>', lambda e: self.close())

    def close(self):
        if self.win:
            self.win.destroy()
            self.win = None


class App:
    def __init__(self, root):
        self.root = root
        cfg = json.load(open(CONFIG, encoding='utf-8')) if os.path.exists(CONFIG) else {}
        self.mons = detect_monitors()
        self.sig = signature(self.mons)
        # Calibration per display layout; the single 'monitors' entry of older versions becomes the first profile.
        self.profiles = cfg.get('profiles') or ({self.sig: cfg['monitors']} if cfg.get('monitors') else {})
        self.load_profile()
        self.presets = cfg.get('presets', [])
        if cfg.get('image'):  # config from the very first version: a single image
            self.presets.append(dict(id=uuid.uuid4().hex[:8], name=os.path.basename(cfg['image']),
                                     layers=[dict(path=cfg['image'], place=cfg['place'])]))
        self.applied = cfg.get('applied')
        self.cur = self.active = self.drag = self.sel = None
        self.layers, self.missing, self.thumbs = [], [], {}
        self.view, self.fit_k, self.pan = (0, 0, 1), 1, None
        self.resize = self.hover = self.lib_drag = self.pending = None
        self.undo_stack, self.redo_stack, self.last_step = [], [], 0
        self.guides = []  # (axis 0=x / 1=y, position in mm), shown while dragging
        # Image library; on first run, the images already used by wallpapers.
        self.library = cfg.get('library') or list(dict.fromkeys(L['path'] for p in self.presets for L in p['layers']))
        self.hidden = set(cfg.get('hidden', []))  # removed by hand: watched folders don't bring them back
        self.watch = cfg.get('watch', [])
        self.lib_paths = {}
        saved = cfg.get('rotate', {})
        self.rot_on = tk.BooleanVar(value=saved.get('on', False))
        self.rot_min = tk.StringVar(value=str(saved.get('min', 30)))
        self.rot_mode = tk.StringVar(value=saved.get('mode', MODES[0]))
        self.rot_unit = tk.StringVar(value=saved.get('unit', 'min'))
        self.rot_job = self.disp_job = None
        saved = cfg.get('options', {})
        self.smooth = tk.BooleanVar(value=saved.get('smooth', True))
        self.stop_we = tk.BooleanVar(value=saved.get('stop_we', True))
        self.context = self.context_return = None  # wallpaper forced by a rule, and the one to go back to
        self.adj_vars = {key: tk.DoubleVar(value=0) for key, _, _ in ADJUSTMENTS}
        self.adj_vars['angle'] = tk.DoubleVar(value=0)
        self.redraw_job = self.settle_job = self.frame_key = self.photo = None
        self.fast = False
        self.apply_note = ''
        saved = cfg.get('grid', {})
        self.grid_step = tk.StringVar(value=str(saved.get('step', 10)))
        self.grid_snap = tk.BooleanVar(value=saved.get('snap', False))
        self.fill_label = tk.StringVar(value=FILLS[0])
        self.autostart = tk.BooleanVar(value=self.autostart_cmd() is not None)
        self.tray_ok = self.balloon_shown = False
        os.makedirs(THUMBS, exist_ok=True)
        self.hint = ('Wheel: zoom view  ·  Ctrl+wheel: zoom image (+Shift: fine)  ·  Drag image: move  ·  '
                     'Corners: resize  ·  Hold Ctrl: no snapping  ·  Empty space / middle button: pan  ·  '
                     'Right-drag: move monitor (arrows: 1 mm)  ·  Double-click monitor: physical size')
        self.build_ui(apply_theme(root))
        self.hwnd = native_hooks(root, self.on_native)
        self.tray_add()
        self.hotkeys()

        for path in self.library:
            self.lib_add_item(path)
        for p in self.presets:
            self.add_item(p)
        if self.presets:
            first = next((p for p in self.presets if p['id'] == self.applied), self.presets[0])
            self.tree.selection_set(first['id'])
            self.select()
        self.schedule()
        root.after(2000, self.scan_watched)
        root.after(3000, self.check_context)

    # --- layout ---
    def build_ui(self, glyphs):
        root = self.root
        side = ttk.Frame(root, style='Side.TFrame', width=420)
        side.pack(side='left', fill='y')
        side.pack_propagate(False)

        head = ttk.Frame(side, style='Side.TFrame')
        head.pack(fill='x', padx=18, pady=(16, 8))
        if os.path.exists(ICON):
            self.logo = ImageTk.PhotoImage(Image.open(ICON).convert('RGBA').resize((32, 32), Image.LANCZOS))
            ttk.Label(head, image=self.logo, style='Side.TLabel').pack(side='left', padx=(0, 10))
        ttk.Label(head, text='Spanwyse', style='Title.TLabel').pack(side='left')

        foot = ttk.Frame(side, style='Side.TFrame')
        foot.pack(side='bottom', fill='x', padx=18, pady=(6, 14))
        ttk.Checkbutton(foot, text='Start with Windows (in the tray)', variable=self.autostart,
                        command=self.set_autostart).pack(anchor='w')
        ttk.Checkbutton(foot, text='Smooth transitions', variable=self.smooth, command=self.store).pack(
            anchor='w', pady=(4, 0))
        ttk.Checkbutton(foot, text='Stop Wallpaper Engine when applying', variable=self.stop_we,
                        command=self.store).pack(anchor='w', pady=(4, 0))
        ttk.Label(foot, text='Ctrl+Alt+← / → anywhere: previous / next wallpaper', style='Muted.TLabel').pack(
            anchor='w', pady=(6, 0))
        ttk.Label(foot, text=f'Spanwyse {VERSION}  ·  free and open source (MIT)  ·  protagonistlabs.app',
                  style='Muted.TLabel').pack(anchor='w', pady=(2, 0))

        panes = ttk.PanedWindow(side, orient='vertical')
        panes.pack(fill='both', expand=True)

        # Wallpapers
        wp = ttk.Frame(panes, style='Side.TFrame')
        panes.add(wp, weight=1)
        self.tree = t = self.section(wp, 'WALLPAPERS', 'extended', [
            ('\uE710', self.new, 'New wallpaper'),
            ('\uE8AC', self.rename, 'Rename (F2)'),
            ('\uE74D', self.delete, 'Delete (Del)'),
            ('\uE70E', lambda: self.move(-1), 'Move up (Ctrl+↑)'),
            ('\uE70D', lambda: self.move(1), 'Move down (Ctrl+↓)')])
        t.bind('<<TreeviewSelect>>', self.select)
        t.bind('<Double-1>', lambda e: self.apply())
        t.bind('<Return>', lambda e: self.apply())
        t.bind('<F2>', lambda e: self.rename())
        t.bind('<Delete>', lambda e: self.delete())
        t.bind('<Control-Up>', lambda e: self.move(-1) or 'break')
        t.bind('<Control-Down>', lambda e: self.move(1) or 'break')
        t.bind('<Button-3>', self.wallpaper_menu)
        self.tip(t, 'Double-click or Enter applies  ·  Ctrl/Shift+click to pick several  ·  right-click: '
                    'slideshow, start time, "show when" rules')
        rot = ttk.Frame(wp, style='Side.TFrame')
        rot.pack(fill='x', padx=18, pady=(8, 14))
        ttk.Checkbutton(rot, text='Slideshow', variable=self.rot_on).pack(side='left')
        ttk.Combobox(rot, textvariable=self.rot_mode, values=MODES, state='readonly', width=10).pack(
            side='left', padx=8)
        ttk.Spinbox(rot, from_=1, to=9999, width=4, textvariable=self.rot_min).pack(side='left')
        ttk.Combobox(rot, textvariable=self.rot_unit, values=('sec', 'min', 'h'), state='readonly', width=4).pack(
            side='left', padx=6)
        self.tip(rot, 'Cycles the wallpapers marked ▶ (right-click › Add to slideshow; all of them if none is '
                      'marked), in order or at random, or by time of day. Runs while Spanwyse is open or in the tray.')
        for var in (self.rot_on, self.rot_min, self.rot_mode, self.rot_unit):
            var.trace_add('write', self.schedule)

        # Images
        im = ttk.Frame(panes, style='Side.TFrame')
        panes.add(im, weight=1)
        self.lib = lb = self.section(im, 'IMAGES', 'extended', [
            ('\uE710', self.add_to_library, 'Add images… (or drop files from Explorer)'),
            ('\uE8B7', self.watch_menu, 'Watched folders: new images in them appear here by themselves'),
            ('\uE8E5', self.place_selected, 'Place selected in the layout (2 selected = one per monitor)'),
            ('\uE711', self.lib_remove, 'Remove from list (files stay on disk)')])
        self.watch_button = self.buttons['']
        lb.bind('<Double-1>', self.place_selected)
        lb.bind('<Return>', self.place_selected)
        lb.bind('<Delete>', lambda e: self.lib_remove())
        lb.bind('<ButtonPress-1>', self.lib_press, add='+')
        lb.bind('<B1-Motion>', self.lib_motion, add='+')
        lb.bind('<ButtonRelease-1>', self.lib_release, add='+')
        self.tip(lb, 'Drag onto a monitor to fill it  ·  double-click to place  ·  drop files from Explorer to add')

        main = ttk.Frame(root)
        main.pack(side='left', fill='both', expand=True)
        bar = ttk.Frame(main)
        bar.pack(fill='x', padx=14, pady=(12, 6))
        for glyph, cmd, tip in (('\uE7A7', self.undo, 'Undo (Ctrl+Z)'), ('\uE7A6', self.redo, 'Redo (Ctrl+Y)')):
            b = ttk.Button(bar, text=glyph, style='Glyph.TButton', command=cmd, width=3)
            b.pack(side='left', padx=(0, 4))
            self.tip(b, tip)
        for text, cmd, tip in (
                ('Fit view', self.fit_canvas, 'Show all monitors'),
                ('Fit image', self.fit_active, 'Selected image covers the selected monitor (right-click one), or all'),
                ('Split at seam', self.split_active,
                 'Cut the selected image where the monitors meet, so each piece can be aligned on its own'),
                ('Flip', lambda: self.transform('flip'), 'Mirror the selected image'),
                ('Rotate', lambda: self.transform('rot'), 'Turn the selected image 90° clockwise'),
                ('Remove', self.remove_layer, 'Take the selected image out of this wallpaper (Del)')):
            b = ttk.Button(bar, text=text, command=cmd)
            b.pack(side='left', padx=(8 if text == 'Fit view' else 0, 6))
            self.tip(b, tip)
        apply = ttk.Button(bar, text='Apply wallpaper', style='Accent.TButton', command=self.apply)
        apply.pack(side='right')
        self.tip(apply, 'Set this wallpaper across all monitors')
        b = ttk.Button(bar, text='Preview', command=self.preview_live)
        b.pack(side='right', padx=6)
        self.tip(b, 'Show it on your monitors for 15 seconds, then keep it or go back')
        self.export_button = b = ttk.Button(bar, text='Export ▾', command=self.export_menu)
        b.pack(side='right')
        self.tip(b, 'Save the combined image, or one image per monitor at its native resolution')

        opts = ttk.Frame(main)
        opts.pack(fill='x', padx=14, pady=(0, 8))
        ttk.Label(opts, text='Grid', style='Bar.TLabel').pack(side='left')
        ttk.Spinbox(opts, from_=1, to=500, increment=5, width=5, textvariable=self.grid_step).pack(side='left', padx=6)
        ttk.Label(opts, text='mm', style='Bar.TLabel').pack(side='left')
        ttk.Checkbutton(opts, text='Snap to grid', variable=self.grid_snap, style='Bar.TCheckbutton').pack(
            side='left', padx=(14, 28))
        ttk.Label(opts, text='Behind images', style='Bar.TLabel').pack(side='left')
        fill = ttk.Combobox(opts, textvariable=self.fill_label, values=FILLS, state='readonly', width=16)
        fill.pack(side='left', padx=6)
        fill.bind('<<ComboboxSelected>>', self.set_fill)
        self.tip(opts, 'Grid spacing in real mm  ·  what shows on a monitor where no image covers it')
        adj = ttk.Frame(opts)
        adj.pack(side='left', padx=(28, 0))
        for key, name, limit in (('angle', 'Angle', 180), *((k, n, 100) for k, n, _ in ADJUSTMENTS)):
            ttk.Label(adj, text=name, style='Bar.TLabel').pack(side='left', padx=(10, 4))
            sc = ttk.Scale(adj, from_=-limit, to=limit, length=110, variable=self.adj_vars[key],
                           command=lambda v, k=key: self.set_adj(k, v))
            sc.pack(side='left')
            sc.bind('<ButtonRelease-1>', lambda e: (self.settle(), self.store()), add='+')
            sc.bind('<Double-Button-1>', lambda e, k=key: self.set_adj(k, 0))
        b = ttk.Button(adj, text='Reset', command=self.reset_adj)
        b.pack(side='left', padx=(10, 0))
        self.tip(adj, 'Selected image: free rotation in degrees, and brightness / contrast / saturation (each '
                      'piece of a split image on its own)  ·  double-click a slider to zero it')
        self.grid_step.trace_add('write', lambda *_: self.redraw())

        self.canvas = c = tk.Canvas(main, bg='#%02x%02x%02x' % PAPER, highlightthickness=0)
        c.pack(fill='both', expand=True, padx=14)
        self.status = ttk.Label(main, text=self.hint, style='Status.TLabel', anchor='w')
        self.status.pack(fill='x', padx=16, pady=(6, 10))

        c.bind('<Configure>', self.fit_canvas)
        c.bind('<ButtonPress-1>', self.press_image)
        c.bind('<ButtonPress-3>', self.press_monitor)
        c.bind('<B1-Motion>', self.motion)
        c.bind('<B3-Motion>', self.motion)
        c.bind('<ButtonPress-2>', self.start_pan)
        c.bind('<B2-Motion>', self.motion)
        c.bind('<MouseWheel>', self.wheel)
        c.bind('<Motion>', self.hover_cursor)
        c.bind('<ButtonRelease-1>', self.release)
        c.bind('<ButtonRelease-3>', self.release)
        c.bind('<Double-Button-1>', self.edit_size)
        c.bind('<Delete>', lambda e: self.remove_layer())
        for key, d in (('Left', (-1, 0)), ('Right', (1, 0)), ('Up', (0, -1)), ('Down', (0, 1))):
            c.bind(f'<{key}>', lambda e, d=d: self.nudge(*d))
        root.bind('<Control-z>', lambda e: self.undo())
        root.bind('<Control-y>', lambda e: self.redo())
        root.bind('<Control-Z>', lambda e: self.redo())  # Ctrl+Shift+Z
        root.protocol('WM_DELETE_WINDOW', self.hide)

    def section(self, parent, title, selectmode, buttons):
        """Header with icon buttons, then a thumbnail list with a scrollbar."""
        head = ttk.Frame(parent, style='Side.TFrame')
        head.pack(fill='x', padx=18, pady=(14, 6))
        ttk.Label(head, text=title, style='Head.TLabel').pack(side='left')
        self.buttons = {}
        for glyph, cmd, tip in reversed(buttons):
            b = ttk.Button(head, text=glyph, style='Icon.TButton', command=cmd, takefocus=False, width=3)
            b.pack(side='right', padx=1)
            self.tip(b, tip)
            self.buttons[glyph] = b
        body = ttk.Frame(parent, style='Side.TFrame')
        body.pack(fill='both', expand=True, padx=(10, 6))
        tree = ttk.Treeview(body, show='tree', selectmode=selectmode)
        sb = ttk.Scrollbar(body, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        tree.pack(side='left', fill='both', expand=True)
        return tree

    def tip(self, widget, text):
        widget.bind('<Enter>', lambda e: self.status.config(text=text), add='+')
        widget.bind('<Leave>', lambda e: self.status.config(text=self.hint), add='+')

    def menu(self):
        return Popup(self.root)

    def dialog(self, title):
        d = tk.Toplevel(self.root, bg=INK)
        d.title(title)
        d.transient(self.root)
        d.resizable(False, False)
        if os.path.exists(ICON):
            d.iconbitmap(ICON)
        return d

    def show_dialog(self, d, focus):
        dark_titlebar(d)
        r = self.root
        if r.state() == 'withdrawn':  # opened from the tray: centre on the screen instead
            d.geometry(f'+{(d.winfo_screenwidth() - d.winfo_width()) // 2}+{(d.winfo_screenheight() - d.winfo_height()) // 3}')
        else:
            d.geometry(f'+{r.winfo_rootx() + (r.winfo_width() - d.winfo_width()) // 2}'
                       f'+{r.winfo_rooty() + (r.winfo_height() - d.winfo_height()) // 3}')
        d.lift()
        focus.focus_set()
        d.grab_set()

    def ask(self, title, prompt, initial=''):
        """Small dark text prompt; returns the stripped text, '' if left empty, or None if cancelled."""
        d = self.dialog(title)
        ttk.Label(d, text=prompt).pack(anchor='w', padx=22, pady=(20, 8))
        var = tk.StringVar(value=initial)
        entry = ttk.Entry(d, textvariable=var, width=40, font=(FONT, 11))
        entry.pack(fill='x', padx=22)
        entry.select_range(0, 'end')
        result = []

        def ok(_=None):
            result.append(var.get().strip())
            d.destroy()

        row = ttk.Frame(d)
        row.pack(fill='x', padx=22, pady=20)
        ttk.Button(row, text='OK', style='Accent.TButton', command=ok).pack(side='right')
        ttk.Button(row, text='Cancel', command=d.destroy).pack(side='right', padx=8)
        d.bind('<Return>', ok)
        d.bind('<Escape>', lambda _: d.destroy())
        self.show_dialog(d, entry)
        d.wait_window()
        return result[0] if result else None

    # --- window, tray, hotkeys, autostart ---
    def on_native(self, kind, data):
        if kind == 'drop':
            self.add_to_library(data)
        elif kind == 'hotkey':
            self.step_wallpaper(1 if data == 1 else -1)
        elif kind == 'tray' and data == 0x202:  # WM_LBUTTONUP
            self.show()
        elif kind == 'tray' and data == 0x205:  # WM_RBUTTONUP
            self.tray_menu()
        elif kind == 'display':  # coalesce the burst of messages a layout change sends
            if self.disp_job:
                self.root.after_cancel(self.disp_job)
            self.disp_job = self.root.after(1500, self.display_changed)
        elif kind == 'tray_readd':
            self.tray_add()

    def tray_data(self):
        nid = NOTIFYICONDATA(cbSize=ctypes.sizeof(NOTIFYICONDATA), hWnd=self.hwnd, uID=1)
        return nid

    def tray_add(self):
        nid = self.tray_data()
        nid.uFlags = 1 | 2 | 4  # NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = TRAY_MSG
        nid.hIcon = user32.LoadImageW(None, ICON, 1, 0, 0, 0x10 | 0x40) if os.path.exists(ICON) else None
        nid.szTip = 'Spanwyse'
        self.tray_ok = bool(ctypes.windll.shell32.Shell_NotifyIconW(0, ctypes.byref(nid)))  # NIM_ADD

    def tray_balloon(self, text):
        nid = self.tray_data()
        nid.uFlags = 0x10  # NIF_INFO
        nid.szInfo, nid.szInfoTitle = text, 'Spanwyse'
        ctypes.windll.shell32.Shell_NotifyIconW(1, ctypes.byref(nid))  # NIM_MODIFY

    def tray_menu(self):
        m = self.menu()
        m.add_command(label='Open Spanwyse', command=self.show, font=(FONT, 10, 'bold'))
        m.add_command(label='Next wallpaper\tCtrl+Alt+→', command=lambda: self.step_wallpaper(1))
        m.add_command(label='Previous wallpaper\tCtrl+Alt+←', command=lambda: self.step_wallpaper(-1))
        m.add_command(label=f'Slideshow\t{"on" if self.rot_on.get() else "off"}',
                      command=lambda: self.rot_on.set(not self.rot_on.get()))
        if self.presets:
            m.add_separator()
            for i, p in enumerate(self.presets):
                m.add_command(label=('● ' if p['id'] == self.applied else '    ') + p['name'] +
                              ('\t▶' if p.get('show') else ''), command=lambda i=i: self.go(i))
        m.add_separator()
        m.add_command(label='Quit', command=self.quit)
        pt = w.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        m.tk_popup(pt.x, pt.y)

    def hotkeys(self):
        failed = [name for hid, vk, name in ((1, 0x27, '→'), (2, 0x25, '←'))
                  if not user32.RegisterHotKey(self.hwnd, hid, 0x1 | 0x2 | 0x4000, vk)]  # Alt|Ctrl|NoRepeat
        if failed:
            self.status['text'] = f'Ctrl+Alt+{"/".join(failed)} is taken by another app; those hotkeys are off.'

    def autostart_cmd(self):
        try:
            return winreg.QueryValueEx(winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY), 'Spanwyse')[0]
        except OSError:
            return None

    def set_autostart(self):
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
        if not self.autostart.get():
            if self.autostart_cmd() is not None:
                winreg.DeleteValue(key, 'Spanwyse')
            return
        if FROZEN:
            cmd = f'"{sys.executable}" --tray'
        else:  # pythonw: no console window
            pyw = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe')
            cmd = f'"{pyw if os.path.exists(pyw) else sys.executable}" "{os.path.abspath(__file__)}" --tray'
        winreg.SetValueEx(key, 'Spanwyse', 0, winreg.REG_SZ, cmd)

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide(self):
        """The close button keeps Spanwyse in the tray (rotation and hotkeys keep working)."""
        if not self.tray_ok:
            return self.quit()
        self.store()
        self.root.withdraw()
        if not self.balloon_shown:
            self.balloon_shown = True
            self.tray_balloon('Still running in the tray. Right-click the icon to quit.')

    def quit(self):
        self.store()
        ctypes.windll.shell32.Shell_NotifyIconW(2, ctypes.byref(self.tray_data()))  # NIM_DELETE
        for hid in (1, 2):
            user32.UnregisterHotKey(self.hwnd, hid)
        self.root.destroy()

    # --- monitor profiles ---
    def load_profile(self):
        prof = self.profiles.get(self.sig, {})
        for m in self.mons:
            m['mm'] = list(prof.get(m['id'], m['mm']))

    def display_changed(self):
        """Resolution, rotation or monitor set changed: switch calibration and re-apply the wallpaper for it."""
        self.disp_job = None
        mons = detect_monitors()
        if signature(mons) == self.sig:
            return
        self.store()  # keeps the old layout's calibration under its own profile
        self.mons, self.sig = mons, signature(mons)
        self.load_profile()
        self.sel = self.hover = self.drag = self.resize = None
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.fit_canvas()
        i = next((i for i, p in enumerate(self.presets) if p['id'] == self.applied), None)
        if i is not None:
            self.go(i)
            self.status['text'] = f'Display layout changed: re-applied “{self.presets[i]["name"]}” for it.'

    # --- image library ---
    def lib_add_item(self, path):
        key = hashlib.md5(path.encode()).hexdigest()[:12]
        thumb = self.thumb_path('img_' + key)
        try:
            with Image.open(path) as im:
                text = f'   {os.path.basename(path)}\n   {im.width} × {im.height}'
                if not os.path.exists(thumb):
                    im.draft('RGB', (THUMB[0] * 2, THUMB[1] * 2))  # big JPEGs decode at reduced size, fast
                    small = im.convert('RGB')
                    small.thumbnail(THUMB)
                    small.save(thumb)
            small = Image.open(thumb).convert('RGBA')
            framed = Image.new('RGBA', THUMB, (0, 0, 0, 0))  # same width for every row so the text lines up
            framed.paste(small, ((THUMB[0] - small.width) // 2, (THUMB[1] - small.height) // 2))
            self.thumbs[key] = ImageTk.PhotoImage(framed)
        except OSError:
            text, self.thumbs[key] = f'   {os.path.basename(path)}\n   missing', ''
        self.lib_paths[key] = path
        self.lib.insert('', 'end', iid=key, text=text, image=self.thumbs[key])

    def add_to_library(self, paths=None, by_hand=True):
        """From the file dialog, dropped from Explorer, or found in a watched folder. Only into the list."""
        if paths is None:
            paths = filedialog.askopenfilenames(filetypes=[('Images', ' '.join('*' + e for e in IMAGE_EXT))])
        known = set(self.library)
        new = [p for p in dict.fromkeys(os.path.normpath(p) for p in paths)
               if p.lower().endswith(IMAGE_EXT) and p not in known and (by_hand or p not in self.hidden)]
        for p in new:
            self.hidden.discard(p)
            self.library.append(p)
            self.lib_add_item(p)
        if new:
            self.lib.see(self.lib.get_children()[-1])
            self.store()
        return new

    def lib_remove(self):
        for key in self.lib.selection():
            path = self.lib_paths.pop(key)
            self.library.remove(path)
            self.hidden.add(path)
            self.lib.delete(key)
        self.store()

    def place_selected(self, _=None):
        self.place_images([self.lib_paths[k] for k in self.lib.selection()])

    def watch_menu(self):
        m = self.menu()
        for f in self.watch:
            m.add_command(label=f'✕   {f}', command=lambda f=f: self.unwatch(f))
        if self.watch:
            m.add_separator()
        m.add_command(label='Watch a folder…', command=self.add_watch)
        b = self.watch_button
        m.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())

    def add_watch(self):
        folder = filedialog.askdirectory(title='Watch a folder for new images')
        if folder and os.path.normpath(folder) not in self.watch:
            self.watch.append(os.path.normpath(folder))
            self.scan_watched(again=False)

    def unwatch(self, folder):
        self.watch.remove(folder)
        self.store()

    def scan_watched(self, again=True):
        found = [os.path.join(d, f) for folder in self.watch for d, _, files in os.walk(folder) for f in files]
        new = self.add_to_library(found, by_hand=False)
        if new:
            self.status['text'] = f'{len(new)} new image(s) from watched folders.'
        self.store()
        if again:
            self.root.after(10000, self.scan_watched)

    # dragging from the list onto the canvas: on a monitor the image covers it exactly, elsewhere all of them
    def lib_press(self, e):
        self.lib_drag = (e.x_root, e.y_root, False)

    def drop_target(self, e):
        """(is the pointer over the canvas, monitor under it or None)."""
        if self.root.winfo_containing(e.x_root, e.y_root) is not self.canvas:
            return False, None
        e.x, e.y = e.x_root - self.canvas.winfo_rootx(), e.y_root - self.canvas.winfo_rooty()
        return True, next((m['mm'] for m in reversed(self.mons) if self.inside(e, m['mm'])), None)

    def lib_motion(self, e):
        if not self.lib_drag:
            return
        x, y, moving = self.lib_drag
        if not moving and abs(e.x_root - x) + abs(e.y_root - y) < 8:
            return
        self.lib_drag = (x, y, True)
        self.lib.config(cursor='plus')
        hover = self.drop_target(e)[1]
        if hover is not self.hover:
            self.hover = hover
            self.redraw()

    def lib_release(self, e):
        drag, self.lib_drag, self.hover = self.lib_drag, None, None
        self.lib.config(cursor='')
        if not (drag and drag[2]):
            return
        on_canvas, mon = self.drop_target(e)
        if on_canvas:
            self.place_images([self.lib_paths[k] for k in self.lib.selection()], mon)
        else:
            self.redraw()

    # --- wallpapers ---
    def thumb_path(self, pid):
        return os.path.join(THUMBS, pid + '.png')

    def label(self, p):
        rule = p.get('when') or {}
        notes = [n for n in ('▶ slideshow' if p.get('show') else '', f'from {p["time"]}' if p.get('time') else '',
                             f'while {rule["app"]} runs' if rule.get('app') else '',
                             f'in {rule["theme"]} mode' if rule.get('theme') else '') if n]
        return '   ' + p['name'] + ('\n   ' + '  ·  '.join(notes) if notes else '')

    def add_item(self, p):
        path = self.thumb_path(p['id'])
        self.thumbs[p['id']] = ImageTk.PhotoImage(Image.open(path)) if os.path.exists(path) else ''
        self.tree.insert('', 'end', iid=p['id'], text=self.label(p), image=self.thumbs[p['id']])

    def fill(self):
        return (self.cur or {}).get('fill', '#000000')

    def store(self):
        """Save the editor into the current wallpaper (images + thumbnail) and write the config."""
        if self.cur:
            self.cur['layers'] = [dict(path=L['path'], place=L['place'], xf=L['xf'], crop=L['crop'],
                                       angle=L['angle'], adj=L['adj']) for L in self.layers] + self.missing
            img = preview_image(self.layers, self.mons, fit_view([m['mm'] for m in self.mons], THUMB), THUMB,
                                paper=False, fill=self.fill())
            img.save(self.thumb_path(self.cur['id']))
            self.thumbs[self.cur['id']] = ImageTk.PhotoImage(img)
            self.tree.item(self.cur['id'], image=self.thumbs[self.cur['id']])
        self.profiles[self.sig] = {m['id']: m['mm'] for m in self.mons}
        cfg = dict(profiles=self.profiles, presets=self.presets, library=self.library, applied=self.applied,
                   hidden=sorted(self.hidden), watch=self.watch,
                   rotate=dict(on=self.rot_on.get(), min=self.rot_min.get(), mode=self.rot_mode.get(),
                               unit=self.rot_unit.get()),
                   grid=dict(step=self.grid_step.get(), snap=self.grid_snap.get()),
                   options=dict(smooth=self.smooth.get(), stop_we=self.stop_we.get()))
        json.dump(cfg, open(CONFIG, 'w', encoding='utf-8'), indent=2, ensure_ascii=False)

    def select(self, _=None):
        """Load the focused wallpaper into the editor (with several selected, the one clicked last)."""
        sel = self.tree.selection()
        pid = self.tree.focus() if self.tree.focus() in sel else (sel[0] if sel else None)
        p = next((p for p in self.presets if p['id'] == pid), None)
        if p is None or p is self.cur:
            return
        self.store()
        self.cur, self.layers, self.missing = p, [], []
        for L in p['layers']:
            try:
                self.layers.append(load_layer(L['path'], L['place'], L.get('xf', (0, False)), L.get('crop'),
                                              L.get('adj'), L.get('angle', 0)))
            except OSError:  # moved/deleted file: keep the reference so saving doesn't drop it
                self.missing.append(L)
        self.active = self.layers[-1] if self.layers else None
        self.undo_stack.clear()
        self.redo_stack.clear()
        f = self.fill()
        self.fill_label.set(FILLS[0] if f == '#000000' else FILLS[1] if f == 'blur' else f'Color {f}')
        self.status['text'] = ('Missing: ' + ', '.join(L['path'] for L in self.missing)) if self.missing else self.hint
        self.redraw()

    def wallpaper_menu(self, e):
        row = self.tree.identify_row(e.y)
        if not row:
            return
        if row not in self.tree.selection():  # right-click inside a multi-selection keeps it
            self.tree.selection_set(row)
        self.tree.focus(row)
        self.select()
        chosen = [p for p in self.presets if p['id'] in self.tree.selection()]
        many = f' ({len(chosen)})' if len(chosen) > 1 else ''
        m = self.menu()
        m.add_command(label='Apply', command=self.apply, font=(FONT, 10, 'bold'))
        m.add_command(label='Preview on monitors', command=self.preview_live)
        m.add_separator()
        if all(p.get('show') for p in chosen):
            m.add_command(label='Remove from slideshow' + many, command=lambda: self.mark_slideshow(chosen, False))
        else:
            m.add_command(label='Add to slideshow' + many, command=lambda: self.mark_slideshow(chosen, True))
        m.add_command(label='Start time for “Time of day”…', command=self.set_time)
        m.add_command(label='Show when an app runs / dark mode…', command=self.set_rule)
        m.add_separator()
        m.add_command(label='Rename…\tF2', command=self.rename)
        m.add_command(label='Delete\tDel', command=self.delete)
        m.tk_popup(e.x_root, e.y_root)

    def mark_slideshow(self, chosen, on):
        for p in chosen:
            if on:
                p['show'] = True
            else:
                p.pop('show', None)
            self.tree.item(p['id'], text=self.label(p))
        self.store()

    def set_rule(self):
        """“Show when”: an app's exe name (shown while it runs) or dark / light (the Windows theme)."""
        if not self.cur:
            return
        rule = self.cur.get('when') or {}
        txt = self.ask('Show when', 'Show this wallpaper while an app runs (its exe, e.g. Photoshop.exe),\n'
                                    'or type dark / light for the Windows theme. Leave empty to clear.',
                       rule.get('app') or rule.get('theme', ''))
        if txt is None:
            return
        if not txt:
            self.cur.pop('when', None)
        elif txt.lower() in ('dark', 'light'):
            self.cur['when'] = {'theme': txt.lower()}
        elif re.fullmatch(r'[^\\/:*?"<>|]+', txt):
            self.cur['when'] = {'app': txt if txt.lower().endswith('.exe') else txt + '.exe'}
        else:
            self.status['text'] = f'“{txt}” isn\'t an exe name; use just the file name, e.g. Photoshop.exe.'
            return
        self.tree.item(self.cur['id'], text=self.label(self.cur))
        self.store()
        self.context = 'recheck'  # evaluate the new rule on the next check even if nothing else changed

    def check_context(self):
        """Every few seconds: if a “show when” rule starts or stops holding, switch wallpapers (and, when it
        stops, go back to the one shown before). Only changes act, so a manual pick isn't overridden."""
        i = context_match(self.presets, processes(), windows_dark())
        key = self.presets[i]['id'] if i is not None else None
        if key != self.context:
            if key:
                if not self.context or self.context == 'recheck':
                    self.context_return = self.applied
                if key != self.applied:
                    self.go(i)
            elif self.context and self.context != 'recheck':
                back = next((j for j, p in enumerate(self.presets) if p['id'] == self.context_return), None)
                if back is not None and self.presets[back]['id'] != self.applied:
                    self.go(back)
            self.context = key
        self.root.after(5000, self.check_context)

    def set_time(self):
        if not self.cur:
            return
        txt = self.ask('Start time', 'Show this wallpaper from (HH:MM, 24h). Leave empty to clear.',
                       self.cur.get('time', ''))
        if txt is None:
            return
        hm = re.fullmatch(r'(\d{1,2}):(\d{2})', txt)
        if txt and not (hm and int(hm[1]) < 24 and int(hm[2]) < 60):
            self.status['text'] = f'“{txt}” is not a time like 07:30 or 19:00.'
            return
        if txt:
            self.cur['time'] = f'{int(hm[1]):02d}:{hm[2]}'
        else:
            self.cur.pop('time', None)
        self.tree.item(self.cur['id'], text=self.label(self.cur))
        self.store()

    def new(self, name=None):
        name = name or self.ask('New wallpaper', 'Name')
        if not name:
            return None
        p = dict(id=uuid.uuid4().hex[:8], name=name, layers=[])
        self.presets.append(p)
        self.add_item(p)
        self.tree.selection_set(p['id'])
        self.select()
        return p

    def rename(self):
        if not self.cur:
            return
        name = self.ask('Rename wallpaper', 'Name', self.cur['name'])
        if name:
            self.cur['name'] = name
            self.tree.item(self.cur['id'], text=self.label(self.cur))
            self.store()

    def delete(self):
        if not self.cur or not messagebox.askyesno(
                'Delete wallpaper', f'Delete “{self.cur["name"]}”?\nThe image files stay on disk.', parent=self.root):
            return
        self.presets.remove(self.cur)
        self.tree.delete(self.cur['id'])
        if os.path.exists(self.thumb_path(self.cur['id'])):
            os.remove(self.thumb_path(self.cur['id']))
        self.cur, self.layers, self.missing, self.active = None, [], [], None
        self.store()
        self.redraw()

    def move(self, d):
        """Move the current wallpaper in the list (the order is also the rotation order)."""
        if not self.cur:
            return
        i = self.presets.index(self.cur)
        if 0 <= i + d < len(self.presets):
            self.presets.insert(i + d, self.presets.pop(i))
            self.tree.move(self.cur['id'], '', i + d)
            self.store()

    def go(self, i):
        """Load wallpaper i into the editor and apply it."""
        pid = self.presets[i]['id']
        self.tree.selection_set(pid)
        self.tree.see(pid)
        self.select()
        self.apply()

    def pool(self):
        """Indices the slideshow and the next/previous hotkeys cycle through: the ▶ ones, else all."""
        marked = [i for i, p in enumerate(self.presets) if p.get('show')]
        return marked or list(range(len(self.presets)))

    def step_wallpaper(self, d):
        pool = self.pool()
        if pool:
            at = next((k for k, i in enumerate(pool) if self.presets[i]['id'] == self.applied), -1 if d > 0 else 0)
            self.go(pool[(at + d) % len(pool)])

    def interval(self):
        """Slideshow interval in ms (at least 5 s: writing and fading take a moment), or None if unusable."""
        try:
            n = float(self.rot_min.get())
        except ValueError:
            return None
        return int(max(n * {'sec': 1, 'min': 60, 'h': 3600}.get(self.rot_unit.get(), 60), 5) * 1000) if n > 0 else None

    def schedule(self, *_):
        if self.rot_job:
            self.root.after_cancel(self.rot_job)
            self.rot_job = None
        if not self.rot_on.get():
            return
        if self.rot_mode.get() == 'Time of day':
            self.rot_job = self.root.after(1000, self.rotate)
            return
        ms = self.interval()
        if ms:
            self.rot_job = self.root.after(ms, self.rotate)

    def rotate(self):
        self.rot_job = None
        mode, pool = self.rot_mode.get(), self.pool()
        if self.context and self.context != 'recheck':  # a “show when” rule holds: it wins, check again later
            self.rot_job = self.root.after(30000 if mode == 'Time of day' else self.interval() or 60000, self.rotate)
            return
        if mode == 'Time of day':
            i = pick_by_time(self.presets, time.strftime('%H:%M'))
            if i is not None and self.presets[i]['id'] != self.applied:
                self.go(i)
            self.rot_job = self.root.after(30000, self.rotate)
            return
        if pool:
            if mode == 'Random':
                self.go(random.choice([i for i in pool if self.presets[i]['id'] != self.applied] or pool))
            else:
                self.step_wallpaper(1)
        self.schedule()

    def show_on_monitors(self, prefix=''):
        """Write each monitor's image and hand them to Windows, crossfading from what's shown now.
        Returns the files, or None on failure."""
        self.status['text'] = 'Applying…'
        self.root.update_idletasks()
        note = ''
        if self.stop_we.get() and stop_wallpaper_engine(processes()):
            note = '  (Wallpaper Engine stopped)'
        files = write_monitor_images(self.layers, self.mons, self.fill(), prefix)
        cover = []
        try:
            desk = DesktopWallpaper()
            if self.smooth.get():
                cover = fade_cover(self.root, self.mons, desk.snapshot()[1])
            unknown = desk.show(files)
        except OSError as e:
            for t in cover:
                t.destroy()
            self.status['text'] = f'Windows refused to change the wallpaper: {e}'
            return None
        fade_out(self.root, cover)
        self.apply_note = note
        if unknown:
            self.status['text'] = f'Windows doesn\'t know {len(unknown)} monitor(s); try again after replugging.'
        return files

    def apply(self, files=None):
        if not self.layers:
            return
        files = files or self.show_on_monitors()
        if not files:
            return
        clean_applied(files.values())
        self.applied = self.cur['id']
        self.store()
        self.status['text'] = f'Applied “{self.cur["name"]}”' + self.apply_note

    def preview_live(self):
        """Show the wallpaper on the real monitors for 15 s; keep it, or put the previous one back."""
        if not self.layers:
            return
        try:
            before = DesktopWallpaper().snapshot()
        except OSError as e:
            self.status['text'] = f'Can\'t read the current wallpaper: {e}'
            return
        files = self.show_on_monitors('preview_')
        if not files:
            return
        d = self.dialog('Preview')
        ttk.Label(d, text='Look at your monitors. Keep this wallpaper?', font=(FONT, 11)).pack(
            anchor='w', padx=22, pady=(20, 4))
        left = ttk.Label(d, style='Status.TLabel')
        left.pack(anchor='w', padx=22)
        row = ttk.Frame(d)
        row.pack(fill='x', padx=22, pady=20)
        state = {'n': 15, 'done': False}

        def finish(keep):
            if state['done']:
                return
            state['done'] = True
            d.destroy()
            if keep:
                self.apply(files)
                return
            cover = fade_cover(self.root, self.mons, files) if self.smooth.get() else []
            try:
                DesktopWallpaper().restore(before)
                self.status['text'] = 'Preview ended; previous wallpaper restored.'
            except OSError as e:
                self.status['text'] = f'Couldn\'t restore the previous wallpaper: {e}'
            fade_out(self.root, cover)
            for path in files.values():
                try:
                    os.remove(path)
                except OSError:
                    pass

        def tick():
            if state['done']:
                return
            if state['n'] <= 0:
                return finish(False)
            left['text'] = f'Going back in {state["n"]} s'
            state['n'] -= 1
            d.after(1000, tick)

        keep = ttk.Button(row, text='Keep', style='Accent.TButton', command=lambda: finish(True))
        keep.pack(side='right')
        ttk.Button(row, text='Go back', command=lambda: finish(False)).pack(side='right', padx=8)
        d.protocol('WM_DELETE_WINDOW', lambda: finish(False))
        d.bind('<Escape>', lambda _: finish(False))
        d.attributes('-topmost', True)
        tick()
        self.show_dialog(d, keep)

    def export_menu(self):
        if not self.layers:
            return
        m = self.menu()
        m.add_command(label='Combined image (whole desktop)…', command=self.export_combined)
        m.add_command(label='One image per monitor…', command=self.export_per_monitor)
        b = self.export_button
        m.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())

    def safe_name(self):
        return re.sub(r'[\\/:*?"<>|]', '_', self.cur['name'])

    def export_combined(self):
        x0, y0, vw, vh = union([m['px'] for m in self.mons])
        path = filedialog.asksaveasfilename(defaultextension='.png', initialfile=f'{self.safe_name()} {vw}x{vh}.png',
                                            filetypes=[('PNG', '*.png'), ('JPEG', '*.jpg')])
        if path:
            compose(self.layers, self.mons, self.fill()).save(path, quality=95)
            self.status['text'] = f'Exported {path}'

    def export_per_monitor(self):
        """Each monitor's part at its native resolution: a matching set to use anywhere."""
        folder = filedialog.askdirectory(title='Export one image per monitor into…')
        if not folder:
            return
        for m in sorted(self.mons, key=lambda m: m['px'][0]):
            size = tuple(m['px'][2:])
            img = paint(self.layers, m['mm'], size, base=backdrop(self.layers, m['mm'], size, self.fill()))
            img.convert('RGB').save(os.path.join(folder, f'{self.safe_name()} - {m["name"]} {size[0]}x{size[1]}.png'))
        self.status['text'] = f'Exported {len(self.mons)} images to {folder}'

    def set_fill(self, _=None):
        if not self.cur:
            self.fill_label.set(FILLS[0])
            return
        choice = self.fill_label.get()
        if choice == FILLS[2]:
            color = colorchooser.askcolor(self.fill() if self.fill().startswith('#') else '#000000',
                                          title='Color behind images')[1]
            if not color:
                return self.select_fill_label()
            self.cur['fill'] = color
        else:
            self.cur['fill'] = 'blur' if choice == FILLS[1] else '#000000'
        self.select_fill_label()
        self.redraw()
        self.store()

    # --- per-image adjustments and free rotation (sliders in the second toolbar row) ---
    def set_adj(self, key, value):
        if not self.active:
            return self.adj_vars[key].set(0)
        self.checkpoint_burst()
        self.interact()
        if key == 'angle':  # only the preview turns here; the full image waits until it's drawn full size
            set_angle(self.active, round(float(value)), fast=True)
        else:
            self.active['adj'][key] = round(float(value))
        self.redraw()

    # --- smooth interaction: quick frames while something moves, full quality once it stops ---
    def interact(self):
        self.fast = True
        if self.settle_job:
            self.root.after_cancel(self.settle_job)
        self.settle_job = self.root.after(250, self.settle)  # wheel ticks and slider drags have no "release"

    def settle(self, _=None):
        if self.settle_job:
            self.root.after_cancel(self.settle_job)
        self.settle_job = None
        if self.fast:
            self.fast = False
            for L in self.layers:
                if L.get('rough'):
                    set_rotation(L, L['angle'])
            self.redraw()

    def reset_adj(self):
        if self.active:
            self.checkpoint()
            self.active['adj'] = {}
            if self.active['angle']:
                set_angle(self.active, 0)
            self.redraw()
            self.store()

    def sync_adj(self):
        """Sliders show the selected image's values (setting a variable doesn't fire the slider's command)."""
        L = self.active or {}
        for key, var in self.adj_vars.items():
            var.set(L.get('angle', 0) if key == 'angle' else (L.get('adj') or {}).get(key, 0))

    def select_fill_label(self):
        f = self.fill()
        self.fill_label.set(FILLS[0] if f == '#000000' else FILLS[1] if f == 'blur' else f'Color {f}')

    # --- undo / redo: snapshots of the layout ---
    def snapshot(self):
        return (list(self.layers), [(list(L['place']), list(L['xf']), L['crop'], L['angle'], dict(L['adj']))
                                    for L in self.layers], [list(m['mm']) for m in self.mons])

    def checkpoint(self, snap=None):
        self.undo_stack.append(snap or self.snapshot())
        del self.undo_stack[:-100]
        self.redo_stack.clear()

    def checkpoint_burst(self):
        """One undo step for a run of wheel ticks or arrow presses."""
        if time.monotonic() - self.last_step > 0.8:
            self.checkpoint()
        self.last_step = time.monotonic()

    def restore(self, snap):
        layers, states, mms = snap
        self.layers = list(layers)
        for L, (place, xf, crop, angle, adj) in zip(layers, states):
            if L['xf'] != xf or L['crop'] != crop or L['angle'] != angle:
                set_xf(L, xf, crop, angle)
            L['place'], L['adj'] = list(place), dict(adj)
        for m, mm in zip(self.mons, mms):
            m['mm'][:] = mm
        if self.active not in self.layers:
            self.active = self.layers[-1] if self.layers else None
        self.redraw()
        self.store()

    def undo(self):
        if self.undo_stack:
            self.redo_stack.append(self.snapshot())
            self.restore(self.undo_stack.pop())

    def redo(self):
        if self.redo_stack:
            self.undo_stack.append(self.snapshot())
            self.restore(self.redo_stack.pop())

    # --- images in the current wallpaper ---
    def place_images(self, paths, target=None):
        """Put library images into the layout. Several at once = one per monitor, left to right."""
        if not paths or not (self.cur or self.new(os.path.splitext(os.path.basename(paths[0]))[0])):
            return
        self.checkpoint()
        left_to_right = sorted(self.mons, key=lambda m: m['px'][0])
        for i, path in enumerate(paths):
            try:
                L = load_layer(path)
            except OSError as e:
                self.status['text'] = f'Could not open {path}: {e}'
                continue
            if len(paths) > 1:
                cover(L, left_to_right[i % len(left_to_right)]['mm'])
            else:
                cover(L, target or self.sel or union([m['mm'] for m in self.mons]))
            self.layers.append(L)
            self.active = L
        self.redraw()
        self.store()

    def remove_layer(self):
        if self.active:
            self.checkpoint()
            self.layers.remove(self.active)
            self.active = self.layers[-1] if self.layers else None
            self.redraw()
            self.store()

    def fit_active(self):
        """The selected image covers the selected monitor, or all of them if none is selected."""
        if self.active:
            self.checkpoint()
            cover(self.active, self.sel or union([m['mm'] for m in self.mons]))
            self.redraw()
            self.store()

    def transform(self, op):
        if self.active:
            self.checkpoint()
            turn(self.active, op)
            self.redraw()
            self.store()

    def split_active(self):
        if not self.active:
            return
        pieces = split(self.active, self.mons)
        if len(pieces) == 1:
            self.status['text'] = ('Set the angle back to 0° before splitting.' if self.active['angle'] else
                                   'The selected image doesn\'t cross a seam between monitors.')
            return
        self.checkpoint()
        i = self.layers.index(self.active)
        self.layers[i:i + 1] = pieces
        self.active = pieces[-1]
        self.redraw()
        self.store()
        self.status['text'] = f'Split into {len(pieces)} pieces: drag each one to line it up.'

    # --- mouse / keyboard ---
    def to_mm(self, e):
        ox, oy, k = self.view
        return ox + e.x / k, oy + e.y / k

    def inside(self, e, r):
        x, y = self.to_mm(e)
        return 0 <= x - r[0] <= r[2] and 0 <= y - r[1] <= r[3]

    def start_drag(self, e, target):
        self.pan = self.resize = None
        self.drag = target and (target, e.x, e.y, target[0], target[1])
        self.pending = self.snapshot()  # becomes an undo step once something actually moves
        self.canvas.focus_set()
        self.redraw()

    def corners(self):
        """The selected image's corners on the canvas: top-left, top-right, bottom-left, bottom-right."""
        b = box(layer_rect(self.active), self.view)
        return [(b[0], b[1]), (b[2], b[1]), (b[0], b[3]), (b[2], b[3])]

    def handle_at(self, e):
        if self.active:
            return next((i for i, (x, y) in enumerate(self.corners()) if abs(x - e.x) <= 8 and abs(y - e.y) <= 8), None)

    def hover_cursor(self, e):
        i = self.handle_at(e)
        self.canvas.config(cursor='' if i is None else ('size_nw_se', 'size_ne_sw')[i in (1, 2)])

    def press_image(self, e):
        i = self.handle_at(e)
        if i is not None:  # corner: resize, anchored at the opposite corner
            self.start_drag(e, None)
            x, y, rw, rh = layer_rect(self.active)
            self.resize = [(x, y), (x + rw, y), (x, y + rh), (x + rw, y + rh)][3 - i]
            return
        hit = next((L for L in reversed(self.layers) if self.inside(e, layer_rect(L))), None)
        if hit:
            self.active = hit
            self.start_drag(e, hit['place'])
        else:
            self.start_pan(e)

    def start_pan(self, e):
        self.drag = self.resize = self.pending = None
        self.pan = (e.x, e.y, self.view)
        self.canvas.focus_set()

    def press_monitor(self, e):
        self.sel = next((m['mm'] for m in reversed(self.mons) if self.inside(e, m['mm'])), None)
        self.start_drag(e, self.sel)

    def motion(self, e):
        if self.pending and (self.resize or self.drag):
            self.checkpoint(self.pending)
            self.pending = None
        if self.resize or self.drag or self.pan:
            self.interact()
        if self.resize:  # aspect ratio is kept; the corner follows whichever axis you drag further
            (ax, ay), (px, py) = self.resize, self.to_mm(e)
            iw, ih = self.active['size']
            s = max(abs(px - ax) / iw, abs(py - ay) / ih, 20 / iw)
            if not e.state & 4:  # Ctrl = no snapping
                s = self.snap_scale(ax, ay, px, py, iw, ih, s)
            self.active['place'] = [ax if px >= ax else ax - iw * s, ay if py >= ay else ay - ih * s, s]
            self.redraw()
        elif self.drag:
            t, sx, sy, x0, y0 = self.drag
            k = self.view[2]
            t[0], t[1] = x0 + (e.x - sx) / k, y0 + (e.y - sy) / k
            if not e.state & 4:  # Ctrl = no snapping
                is_monitor = any(m['mm'] is t for m in self.mons)
                dx, dy = self.snap(t if is_monitor else layer_rect(self.active), exclude=t)
                t[0], t[1] = t[0] + dx, t[1] + dy
            self.redraw()
        elif self.pan:
            sx, sy, (ox, oy, k) = self.pan
            self.view = (ox - (e.x - sx) / k, oy - (e.y - sy) / k, k)
            self.redraw()

    # --- snapping: edges and centres of monitors (and of all of them together), with guides ---
    def snap_lines(self, exclude=None):
        rects = [m['mm'] for m in self.mons if m['mm'] is not exclude]
        rects.append(union(rects))
        return [{r[a] + r[a + 2] * f for r in rects for f in (0, 0.5, 1)} for a in (0, 1)]

    def step(self):
        """Grid spacing in mm (10 if the box holds something unusable)."""
        try:
            v = float(self.grid_step.get())
        except ValueError:
            return 10
        return v if v >= 1 else 10

    def targets(self, lines, v):
        """Snap targets for coordinate `v`: monitor guides, plus the nearest grid line if snapping to grid."""
        g = self.step()
        return list(lines) + ([round(v / g) * g] if self.grid_snap.get() else [])

    def snap(self, rect, exclude=None):
        """The shift (dx, dy) that sticks the nearest edge/centre of `rect` to a guide."""
        tol, lines, self.guides, out = 8 / self.view[2], self.snap_lines(exclude), [], []
        for a in (0, 1):
            vals = [rect[a] + rect[a + 2] * f for f in (0, 0.5, 1)]
            d, t = min(((t - v, t) for v in vals for t in self.targets(lines[a], v)), key=lambda p: abs(p[0]))
            if abs(d) < tol:
                self.guides.append((a, t))
            out.append(d if abs(d) < tol else 0)
        return out

    def snap_scale(self, ax, ay, px, py, iw, ih, s):
        """The scale at which the dragged corner lands on a guide (on the closer axis), else `s`."""
        tol, lines, self.guides = 8 / self.view[2], self.snap_lines(), []
        best = None
        for a, anchor, sign, n in ((0, ax, 1 if px >= ax else -1, iw), (1, ay, 1 if py >= ay else -1, ih)):
            corner = anchor + sign * n * s
            for t in self.targets(lines[a], corner):
                if abs(t - corner) < tol and sign * (t - anchor) > 0 and (best is None or abs(t - corner) < best[0]):
                    best = (abs(t - corner), a, t, sign * (t - anchor) / n)
        if best:
            self.guides = [best[1:3]]
            return best[3]
        return s

    def release(self, _=None):
        if self.guides:
            self.guides = []
            self.redraw()
        if self.cur and (self.drag or self.resize) and not self.pending:
            self.store()  # keeps the wallpaper's thumbnail in step with the layout
        self.pending = None
        self.settle()

    def wheel(self, e):
        up = 1 if e.delta > 0 else -1
        px, py = self.to_mm(e)  # the point under the cursor stays put
        if e.state & 4:  # Ctrl: zoom the selected image (Shift = fine)
            L = self.active
            if not L:
                return
            f = (1.005 if e.state & 1 else 1.05) ** up
            ix, iy, s = L['place']
            if not 50 < L['size'][0] * s * f < 50000:  # mm; sane limits
                return
            self.checkpoint_burst()
            L['place'] = [px - (px - ix) * f, py - (py - iy) * f, s * f]
        else:  # zoom the whole view
            k = self.view[2] * 1.15 ** up
            if not self.fit_k / 4 < k < self.fit_k * 40:
                return
            self.view = (px - e.x / k, py - e.y / k, k)
        self.interact()
        self.redraw()

    def nudge(self, dx, dy):
        if self.sel:
            self.checkpoint_burst()
            self.sel[0] += dx
            self.sel[1] += dy
            self.redraw()

    def edit_size(self, e):
        m = next((m['mm'] for m in self.mons if self.inside(e, m['mm'])), None)
        if not m:
            return
        txt = self.ask('Physical size', 'Visible screen area in mm, width x height', f'{m[2]:.0f}x{m[3]:.0f}')
        try:
            mw, mh = (float(v) for v in txt.lower().replace('×', 'x').split('x'))
        except (AttributeError, ValueError):
            return
        if mw > 0 and mh > 0:
            self.checkpoint()
            m[2:] = [mw, mh]
            self.redraw()
            self.store()

    # --- drawing ---
    def fit_canvas(self, _=None):
        self.view = fit_view([m['mm'] for m in self.mons], (self.canvas.winfo_width(), self.canvas.winfo_height()))
        self.fit_k = self.view[2]
        self.redraw()

    def redraw(self):
        """Ask for a redraw. Mouse moves arrive far faster than a frame can be drawn; drawing on each one
        queued them up and the picture trailed the pointer. Now every request until Tk is idle again makes
        one frame, with the latest positions."""
        if not self.redraw_job:
            self.redraw_job = self.root.after_idle(self.draw)

    def draw(self):
        self.redraw_job = None
        c = self.canvas
        c.delete('ov')  # the lines and labels; the picture item stays and gets new pixels below
        size = (c.winfo_width(), c.winfo_height())
        if size[0] < 2:
            return
        ox, oy, k = self.view
        self.sync_adj()
        # The picture is rebuilt only when something that shows in it changed; selection, guides, hover and
        # handles are canvas items drawn on top, so clicking or hovering costs no rendering at all.
        key = (tuple(self.view), size, self.step(), self.fill(), self.fast,
               tuple((id(L), tuple(L['place']), L['angle'], L.get('rough'), tuple(L['xf']), tuple(L['crop'] or ()),
                      tuple(sorted(L['adj'].items())), L.get('rotated', (None,))[0] == L['angle'])
                     for L in self.layers),
               tuple(tuple(m['mm']) for m in self.mons))
        if key != self.frame_key:
            self.frame_key = key
            pic = preview_image(self.layers, self.mons, self.view, size, step=self.step(), fill=self.fill(),
                                fast=self.fast)
            if self.photo and (self.photo.width(), self.photo.height()) == size:
                self.photo.paste(pic)  # same Tk image, new pixels: no item to delete and re-create
            else:
                self.photo = ImageTk.PhotoImage(pic)
                c.delete('pic')
                c.create_image(0, 0, anchor='nw', image=self.photo, tags='pic')
        if any(L['angle'] and L.get('rotating') == L['angle'] and L.get('rotated', (None,))[0] != L['angle']
               for L in self.layers):
            self.root.after(150, self.redraw)  # a background rotation is still running: look again shortly
        if len(self.layers) > 1:  # outline every piece faintly, so split pieces are easy to find
            for L in self.layers:
                if L is not self.active:
                    c.create_rectangle(*box(layer_rect(L), self.view), outline=MUTED, dash=(2, 4))
        if self.active:
            c.create_rectangle(*box(layer_rect(self.active), self.view), outline='white', dash=(4, 4))
        for m in self.mons:
            b = box(m['mm'], self.view)
            cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2  # centre cross
            c.create_line(cx, b[1], cx, b[3], fill=CYAN, dash=(2, 6))
            c.create_line(b[0], cy, b[2], cy, fill=CYAN, dash=(2, 6))
            if m['mm'] is self.hover:  # target of a drag from the list
                c.create_rectangle(*b, outline=GREEN, width=5)
            c.create_rectangle(*b, outline=YELLOW if m['mm'] is self.sel else CYAN, width=2)
            label = c.create_text(b[0] + 14, b[1] + 12, anchor='nw', fill=TEXT, font=(FONT, 9),
                                  text=f"{m['name']}   {m['px'][2]} × {m['px'][3]} px   "
                                       f"{m['mm'][2]:.0f} × {m['mm'][3]:.0f} mm")
            lb = c.bbox(label)
            c.tag_lower(c.create_rectangle(lb[0] - 8, lb[1] - 4, lb[2] + 8, lb[3] + 4, fill=INK, outline=''), label)
        if not self.layers:
            u = box(union([m['mm'] for m in self.mons]), self.view)
            c.create_text((u[0] + u[2]) / 2, u[3] + 34, fill=MUTED, font=(FONT, 12),
                          text='Drag an image from the list onto a monitor')
        for a, v in self.guides:
            p = (v - (ox, oy)[a]) * k
            c.create_line(*((p, 0, p, size[1]) if a == 0 else (0, p, size[0], p)), fill=PINK)
        if self.active:  # resize handles, above the monitor outlines
            for x, y in self.corners():
                c.create_rectangle(x - 5, y - 5, x + 5, y + 5, fill='white', outline=INK, width=2)
        c.addtag_all('ov')  # everything drawn above is an overlay, deleted at the next frame...
        c.dtag('pic', 'ov')  # ...except the picture itself


def selftest():
    # A line at y = 250 mm must land at the same physical height on both monitors,
    # even though their densities differ (31.5" 4K landscape + 27" 1440p portrait).
    src = Image.new('RGBA', (2400, 1400), 'white')  # 1200 x 700 mm at 0.5 mm/px, covers both monitors
    src.paste((0, 0, 0, 255), (0, 499, 2400, 501))
    mons = [dict(px=[0, 0, 3840, 2160], mm=[0, 0, 697, 392]),
            dict(px=[3840, 0, 1440, 2560], mm=[697, 0, 336, 597])]
    line = dict(orig=src, place=[0, 0, 0.5])
    set_xf(line, (0, False))
    out = compose([line], mons).convert('L')
    for m in mons:
        x, y, pw, ph = m['px']
        dark = [r for r in range(ph) if out.getpixel((x + 10, y + r)) < 128]
        row, want = sum(dark) / len(dark), 250 / m['mm'][3] * ph
        assert abs(row - want) < 2, (m['px'], row, want)
    # Pair: the second image sits only on monitor 2 and doesn't cover the first one on monitor 1.
    red = dict(orig=Image.new('RGBA', (10, 10), 'red'), place=[697, 0, 60])
    set_xf(red, (0, False))
    out = compose([line, red], mons)
    assert out.getpixel((10, 10)) == (255, 255, 255) and out.getpixel((3850, 10)) == (255, 0, 0)

    # Split at the seam: two pieces that together render exactly like the whole image.
    pieces = split(line, mons)
    assert len(pieces) == 2 and pieces[0]['size'][0] + pieces[1]['size'][0] == 2400
    assert compose(pieces, mons).tobytes() == compose([line], mons).tobytes()
    # Stacked monitors cut horizontally; a third monitor to the right adds a vertical cut, not a second row cut.
    stacked = [dict(px=[0, 0, 3840, 2160], mm=[0, 0, 697, 392]), dict(px=[0, 2160, 2560, 1440], mm=[50, 600, 597, 336]),
               dict(px=[3840, 0, 1440, 2560], mm=[697, 0, 336, 597])]
    assert seams(stacked, 1) == {496.0} and seams(stacked, 0) == {697.0}
    big = dict(orig=Image.new('RGBA', (2200, 1600), 'white'), place=[0, 0, 0.5])
    set_xf(big, (0, False))
    pieces = split(big, stacked)
    assert len(pieces) == 4 and sorted(p['size'] for p in pieces) == [(806, 608), (806, 992), (1394, 608), (1394, 992)]
    assert split(big, stacked[:1]) == [big]
    # Flip/rotate keep the crop on the same pixels: rotating a piece 4 times or flipping twice is a no-op.
    mark = dict(orig=Image.new('RGBA', (40, 20), 'white'), place=[0, 0, 1])
    mark['orig'].putpixel((3, 2), (255, 0, 0, 255))
    set_xf(mark, (0, False), [2, 1, 30, 15])
    for ops in (['rot'] * 4, ['flip'] * 2, ['rot', 'flip', 'rot', 'flip']):
        P = dict(mark, place=[0, 0, 1])
        for op in ops:
            turn(P, op)
        assert source(P).getpixel((1, 1)) == (255, 0, 0, 255), ops
    turn(mark, 'rot')
    assert mark['size'] == (14, 28) and source(mark).getpixel((14 - 1 - 1, 1)) == (255, 0, 0, 255)
    # Time of day: the most recently started slot, wrapping to yesterday's last one before the first.
    ps = [dict(time='07:00'), dict(), dict(time='19:30')]
    assert (pick_by_time(ps, '12:00'), pick_by_time(ps, '20:00'), pick_by_time(ps, '05:00')) == (0, 2, 2)
    assert pick_by_time([dict()], '12:00') is None
    # Adjustments change colour but never transparency; zero does nothing.
    px = Image.new('RGBA', (2, 1), (100, 100, 100, 255))
    px.putpixel((1, 0), (0, 0, 0, 0))
    darker = adjust(px, {'brightness': -50})
    assert darker.getpixel((0, 0)) == (50, 50, 50, 255) and darker.getpixel((1, 0))[3] == 0
    assert adjust(px, {'brightness': 0}) is px
    # The size worked out for a tilted image is exactly the one Pillow's rotation produces.
    for w_, h_ in ((7680, 4320), (1440, 2560), (333, 101)):
        for deg in (1, 7.5, 33, 45, 90, -12, 179, 180):
            assert rotated_size(w_, h_, deg) == Image.new('1', (w_, h_)).rotate(-deg, expand=True).size, (w_, h_, deg)
    # Free rotation turns around the centre; tilted images aren't split.
    tilt = dict(orig=Image.new('RGBA', (200, 100), 'white'), place=[0, 0, 1])
    set_xf(tilt, (0, False))
    set_angle(tilt, 90)
    assert source(tilt).size == tilt['size'] == (100, 200) and [round(v) for v in layer_rect(tilt)] == [50, -50, 100, 200]
    assert split(tilt, mons) == [tilt]
    # "Show when": a running app beats the theme; the theme rule follows dark/light.
    rules = [dict(when={'theme': 'dark'}), dict(), dict(when={'app': 'Game.exe'})]
    assert context_match(rules, {'game.exe': 1}, True) == 2
    assert (context_match(rules, {}, True), context_match(rules, {}, False)) == (0, None)
    print('ok')


if __name__ == '__main__':
    if '--test' in sys.argv:
        selftest()
    else:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # real pixels on every monitor
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Spanwyse')  # own taskbar icon
        root = tk.Tk()
        root.title('Spanwyse')
        root.geometry('1760x980')
        root.minsize(1200, 700)
        if os.path.exists(ICON):
            root.iconbitmap(default=ICON)
        dark_titlebar(root)
        App(root)
        if '--tray' in sys.argv:
            root.withdraw()
        root.mainloop()
