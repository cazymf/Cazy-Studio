# Cazy Studio v0.3.1 — Project and Preview Format Fix

Fixes the "1280 × 720 30 FPS" project default and the heavy timeline redraw during preview.

- First video on timeline automatically matches project's format. 2560×1440 120 FPS source -> project at 2560×1440 / 120 FPS.
- Project Settings: select 720p, 1080p, 1440p, 4K and portrait formats, plus 24/30/60/120/240 FPS.
- Match Source: one-click recalibration to the original video.
- Independent Preview FPS 30/60/120 and Preview Quality Auto/720p/1080p/1440p/Native.
- Timeline playhead no longer redraws the complete timeline for every preview frame.
- Faster OpenCV frame skipping during playback.

**Verification:** 23 Python tests passed, headless GUI smoke test passed, Windows launcher cross-compiled, old v0.3 updater installed/rolled back v0.3.1 with project and Addons retained.

**Limits:** Live 120 FPS is a selectable target; actual FPS depends on decoding and display, and is reported separately. Python/Tk isn't a GPU-native preview renderer.

## Publishing this update
Create a **GitHub Release** with tag `v0.3.1`. Attach exactly `Cazy_Studio_v0_3_1_Update.zip` (the update ZIP generated for this release). The in-app Update Center will then detect it. Do not attach a nested full application ZIP under that filename. The binary has not yet been published on GitHub.
