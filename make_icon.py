"""Draws spanwyse.ico in the style of the other FileLabs apps: gradient rounded square, dark
#170a2c shapes with a soft shadow. Two monitors (landscape + portrait) show one continuous
scene, which is what Spanwyse does.   python make_icon.py [preview.png]"""
import sys

from PIL import Image, ImageChops, ImageDraw, ImageFilter

S = 4  # drawn at 4x, downsampled for clean edges
N = 512 * S
INK = '#170a2c'
BODIES = [(42, 177, 321, 334), (335, 136, 469, 375)]  # landscape 4K + portrait 1440p, same mm scale
SCREENS = [(54, 189, 309, 322), (347, 148, 457, 363)]


def sc(box):
    return [round(v * S) for v in box]


def gradient(c1, c2, diagonal):
    v = Image.linear_gradient('L').resize((N, N))
    mask = ImageChops.add(v, v.rotate(90), scale=2) if diagonal else v  # 0 top-left -> 255 bottom-right
    return Image.composite(Image.new('RGB', (N, N), c2), Image.new('RGB', (N, N), c1), mask)


def rounded(boxes, radius, offset=0):
    m = Image.new('L', (N, N), 0)
    d = ImageDraw.Draw(m)
    for b in boxes:
        x0, y0, x1, y1 = sc(b)
        d.rounded_rectangle((x0, y0 + offset, x1, y1 + offset), radius * S, fill=255)
    return m


def draw():
    tile = rounded([(0, 0, 511.75, 511.75)], 104)
    icon = Image.new('RGBA', (N, N), (0, 0, 0, 0))
    icon.paste(gradient('#19e3ff', '#ff2bd6', True), mask=tile)

    shadow = rounded(BODIES, 22, offset=10 * S).filter(ImageFilter.GaussianBlur(11 * S))
    shadow = ImageChops.multiply(shadow.point(lambda a: a * 35 // 100), tile)
    icon.alpha_composite(Image.merge('RGBA', (*Image.new('RGB', (N, N)).split(), shadow)))
    icon.paste(INK, mask=rounded(BODIES, 22))

    # One scene behind both screens: it lines up across the bezel gap.
    scene = gradient('#4b2a7a', '#1d1035', False)
    d = ImageDraw.Draw(scene)
    d.ellipse(sc((328 - 62, 242 - 62, 328 + 62, 242 + 62)), fill='#ffd400')
    d.polygon(sc((40, 336, 112, 270, 162, 302, 240, 238, 302, 292, 362, 254, 420, 304, 472, 276, 472, 380, 40, 380)),
              fill='#ff2bd6')
    icon.paste(scene, mask=rounded(SCREENS, 10))
    return icon.resize((512, 512), Image.LANCZOS)


if __name__ == '__main__':
    img = draw()
    img.save('spanwyse.ico', sizes=[(s, s) for s in (16, 20, 24, 32, 40, 48, 64, 128, 256)])
    if len(sys.argv) > 1:
        img.save(sys.argv[1])
