# Spanwyse

**Download and details:** [protagonistlabs.app/spanwyse](https://protagonistlabs.app/spanwyse/)
· [latest release](https://github.com/limburatorul/spanwyse/releases/latest)

Wallpapers that line up across monitors of different sizes, resolutions and orientations.

Windows spreads a spanning wallpaper by pixels. On a 31.5" 4K screen next to a 27" 1440p screen
turned to portrait, a pixel is 0.18 mm on one and 0.23 mm on the other, so a horizon that crosses
from one to the other lands at a different height on each. Spanwyse lays the images and the
monitors out in real millimetres, the way LittleBigMouse does for the cursor, and gives every
monitor exactly what sits physically in front of it.

Free, MIT, Windows 10 and 11. No account and no telemetry. Over the network it only checks GitHub for a
newer version, at start and every six hours (a new version is verified against the SHA-256 GitHub
publishes and installed in place; untick *Check for updates automatically* to stop it), and, if you add
a picture-of-the-day wallpaper, downloads that picture from Bing or Wallhaven once a day.

## Starting it

Download **`Spanwyse.exe`** from the [latest release](https://github.com/limburatorul/spanwyse/releases/latest)
and run it. One file, no installer, no administrator rights. It keeps its settings next to itself
(`spanwyse.json`, `thumbs\`, `applied\`), so put it in a folder of its own.

It isn't code-signed yet, so SmartScreen may warn the first time: **More info**, then **Run anyway**.

## How it works

- **Monitors in millimetres.** The physical size of each screen comes from its EDID; rotated
  monitors are recognised. Drag the monitors (right mouse button) to where they really sit on the
  desk, with the arrow keys for 1 mm steps; double-click one to correct its size. Each display layout
  (which monitors, their resolution and position) keeps its own calibration, and Spanwyse switches
  to it, and re-applies the wallpaper, when the layout changes.
- **One image or a pair.** Drag an image from the list onto a monitor and it covers that monitor;
  drop it beside the monitors and it covers all of them, slid to where the screens show the most of
  the picture (the same as **Fit image** with no monitor selected). Two images placed together go
  one per monitor, left to right. When an image has fewer pixels than a monitor shows, the monitor's
  label says how much it is enlarged there, so you know before applying that it will look soft.
- **Painting your own.** *Export › Template to paint on* saves a PNG of your layout at the sharpest
  monitor's density, monitors outlined and labelled. Paint over it at that size and place the result
  with no monitor selected: it lands exactly where you painted it.
- **Picture of the day.** The cloud button in the image list adds a wallpaper that gets Bing's image
  of the day or Wallhaven's top picture of the day, fitted across all monitors and renewed daily.
- **Split at seam.** A single large image across two monitors that still don't line up can be cut
  where they meet (side by side or stacked); each piece then moves and resizes on its own.
- **Arranging.** Zoom the view with the wheel, the selected image with Ctrl+wheel. Corners resize.
  Edges and centres snap to the monitors' edges and centres, with guide lines, and to a grid whose
  step you set in mm; hold Ctrl to place freely. Flip, rotate by 90° or by any angle, and adjust
  brightness, contrast and saturation per image. Undo and redo everything (Ctrl+Z / Ctrl+Y).
- **Applying.** Every monitor gets its own image at its native resolution, through Windows'
  per-monitor wallpaper API (`IDesktopWallpaper`). Windows 11 ignores the old registry "Span" style
  once per-monitor wallpapers exist and fills each screen with the whole image; at native resolution
  "Fill" draws it 1:1. Changes crossfade from the previous wallpaper. Optionally the main monitor's
  part also becomes the lock screen picture. **Preview** shows a wallpaper
  for 15 seconds and puts the old one back unless you keep it.
- **Wallpapers and slideshow.** Every arrangement is saved as a wallpaper with a thumbnail. Mark the
  ones you want for the slideshow and pick in order, random or by time of day, every N seconds,
  minutes or hours. A wallpaper can also be shown while an app runs (by its exe name) or when
  Windows is in dark or light mode.
- **Out of the way.** Closing the window keeps Spanwyse in the notification area; Ctrl+Alt+← and →
  switch wallpapers from anywhere, and it can start with Windows. If Wallpaper Engine is running it
  is stopped when you apply, so it doesn't paint over the result (you can turn that off).
- **Export.** The whole desktop as one image, or one image per monitor at its native resolution.
- **Install with a package manager.** `winget install ProtagonistLabs.Spanwyse`, or with Scoop:
  `scoop bucket add protagonist https://github.com/limburatorul/scoop-bucket` then
  `scoop install protagonist/spanwyse`.

## Files

| File | Role |
|---|---|
| `spanwyse.py` | the whole application (Tkinter + Pillow, Windows APIs through ctypes) |
| `make_icon.py` | draws `spanwyse.ico` |
| `spanwyse.ico` | the icon |
| `build.cmd` | runs the self-check and builds `dist\Spanwyse.exe` |

## Running from source and rebuilding

Python 3.11 or newer with Pillow:

```
pip install pillow
python spanwyse.py
python spanwyse.py --test
```

`--test` checks the alignment math: that a line lands at the same physical height on monitors of
different densities, that split pieces put back together match the whole image exactly, flips and
rotations, the time-of-day choice, the "show when" rules, the template, auto-fit and the sharpness
warning.

To build the exe, also `pip install pyinstaller`, then run `build.cmd`.

## More from Protagonist Labs

- [Frostpane](https://protagonistlabs.app/frostpane/?utm_source=github&utm_medium=readme&utm_campaign=spanwyse): desktop icons in glass panes that stay blurred over an animated wallpaper.
- [Cursor Selector](https://protagonistlabs.app/cursorselector/?utm_source=github&utm_medium=readme&utm_campaign=spanwyse): preview and apply Windows cursor schemes, free.
- [All apps](https://protagonistlabs.app/?utm_source=github&utm_medium=readme&utm_campaign=spanwyse): Windows apps that each do one job properly.
