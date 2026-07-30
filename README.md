# video-to-gaussian-splat-for-claude-code

Turn a video into a realistic 3D scene — entirely on your own machine.

```
video --ffmpeg--> frames --COLMAP--> camera poses + sparse point cloud --Brush--> splat.ply
```

## Why this exists

The motivation is simple: transform videos into realistic 3D scenes. That's it. This repository wires ffmpeg, COLMAP, and Brush together with the file layout, settings, and operational lessons (which matcher to use at what dataset size, how to tell a healthy run from a thrashing one, why a "small" dataset can still take hours if the machine's memory is oversubscribed) needed to actually get a video-to-splat pipeline working reliably on a normal Mac or Windows machine.

## What it does

1. **ffmpeg** samples frames from your source video(s).
2. **COLMAP** performs structure-from-motion: from the frames alone, it estimates where the camera was for every shot and produces a sparse 3D point cloud.
3. **Brush** trains a Gaussian Splat from COLMAP's cameras + point cloud — the actual optimization step that turns sparse points into a dense, renderable 3D scene.
4. A **lightweight vendored splat viewer** (`viewer/`) lets you look at the result in a browser with a proper turntable orbit camera (drag = rotate around a fixed vertical axis, never tilts), instead of needing Brush's own desktop viewer or a third-party site.

The output of step 3 is a single `.ply` file — a Gaussian Splat — that can be viewed, shared, or brought into any tool that understands the format (Brush's own viewer, [SuperSplat](https://superspl.at/editor), the vendored viewer here, etc.).

## Requirements

**macOS**
- Apple Silicon (M1 or later)
- [Homebrew](https://brew.sh) — installs `ffmpeg` and `colmap`
- [Brush](https://github.com/ArthurBrussee/brush/releases) (`aarch64-apple-darwin` build) — no package-manager formula exists for it; download the binary directly

**Windows**
- [Chocolatey](https://chocolatey.org) — installs `ffmpeg`
- [COLMAP](https://github.com/colmap/colmap/releases) — no Chocolatey package exists; download the Windows build directly (an NVIDIA GPU here gets you CUDA-accelerated matching, meaningfully faster than the CPU-only path this project uses on Mac)
- [Brush](https://github.com/ArthurBrussee/brush/releases) — Windows build, same as above, direct download

Both platforms: enough free RAM matters more than dataset size. Gaussian Splat training memory use grows as training adds more splats, independent of how big your input video was — see [AGENTS.md](AGENTS.md) for the specific failure mode (silent swap-thrashing, not a crash) and how to spot it.

## Usage

Full step-by-step commands, folder conventions, and every operational gotcha this project has actually run into live in [AGENTS.md](AGENTS.md) — it's written to be followed directly (by you or by a coding agent dropped into this repo). Short version:

```bash
# 1. Extract frames
ffmpeg -i raw/your_video.mov -vf "fps=1" video1/frame_%05d.jpg

# 2. COLMAP structure-from-motion (exhaustive_matcher under ~500 images,
#    vocab_tree_matcher above that — see AGENTS.md for why and the exact flags)
colmap feature_extractor --database_path colmap_workspace/database.db --image_path images_combined
colmap exhaustive_matcher --database_path colmap_workspace/database.db
colmap mapper --database_path colmap_workspace/database.db --image_path images_combined --output_path colmap_workspace/sparse

# 3. Train the splat
brush_app brush_dataset --with-viewer --total-steps 15000 --export-path brush_output --export-name splat.ply
```

Project folders are organized as `<project_name>/object_N/`, each one a fully independent capture — see AGENTS.md for the exact layout and why reconstructions are never merged at the raw-frame level.

## Viewing a result

No server, no Python, nothing to install — bake the splat directly into a single HTML file and open it:

```bash
viewer/package.sh path/to/splat.ply
# writes path/to/splat.html — double-click it, or open it directly in a browser
```

The `.ply` data is embedded straight into the page (browsers block loading local files over `file://` any other way), so the resulting file is fully self-contained — copy it anywhere, email it, whatever, it just opens. Drag to orbit, scroll to zoom. The camera is a fixed-up-axis turntable — horizontal drag always yaws around vertical, unlike Brush's own arcball-style viewer, which can accumulate roll/tilt during a session.

You can also drag a different `.ply` straight onto the open page to view it without repackaging.

Note: embedding inflates file size (base64 adds ~33%), and very large splats (hundreds of MB) can take a noticeable moment to open since the whole file has to be parsed before anything renders. Fine for typical single-object/room splats; for very large scenes, the fetch-based `?url=` mode (serve `viewer/` and pass a path) avoids that entirely.

## Project structure

```
AGENTS.md              full pipeline reference: commands, folder layout, every gotcha
viewer/                 vendored lightweight Gaussian Splat viewer (HTML + JS)
docs/superpowers/specs/ design docs for ongoing work on this repo
example/                a worked example reconstruction (frames → COLMAP → Brush output)
```

## Status / roadmap

The pipeline above works today, run by hand or by a coding agent following AGENTS.md. Still on the roadmap: fully autonomous dependency setup (an agent checking for and installing Homebrew/Chocolatey/COLMAP/Brush on request) and a checklist-driven run through the whole pipeline with resumable failure handling. See `docs/superpowers/specs/` for the current design.

## Credits

- [COLMAP](https://colmap.github.io/) — structure-from-motion
- [Brush](https://github.com/ArthurBrussee/brush) — Gaussian Splat training, Rust/wgpu
- [antimatter15/splat](https://github.com/antimatter15/splat) (MIT) — the WebGL rendering/sorting pipeline the vendored viewer in `viewer/` is built on
- Gaussian Splatting itself: Kerbl, Kopanas, Leimkühler, Drettakis, *3D Gaussian Splatting for Real-Time Radiance Field Rendering*, SIGGRAPH 2023
