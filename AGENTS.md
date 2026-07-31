# 3D Reconstruction Pipeline — Video to Gaussian Splat

This project turns walkthrough videos into 3D Gaussian Splats, entirely locally on macOS (Apple Silicon), no CUDA/Windows/paid software required. It supports multiple houses, each with multiple rooms, each reconstructed independently.

## Pipeline

```
video(s) --ffmpeg--> frames --COLMAP--> camera poses + sparse point cloud --Brush--> splat.ply
```

1. **ffmpeg** extracts frames from the source video(s).
2. **COLMAP** (installed via `brew install colmap`, CPU build, no CUDA) does structure-from-motion: estimates camera poses and a sparse 3D point cloud from the frames.
3. **Brush** (`~/bin/brush_app`, downloaded from https://github.com/ArthurBrussee/brush releases, `aarch64-apple-darwin` build) trains the actual Gaussian Splat from COLMAP's output.

Whenever the user hands over a new video and asks to process it, do all steps without needing to be told each one individually — extract frames, run COLMAP, then run Brush. Blur filtering (step 1b) is NOT part of the default flow — it was tried and made results worse (see step 1b for why).

## Multi-property organization

Reconstructions are organized **by house, then by room**. Each room is its own fully independent, self-contained pipeline — there is no house-level or automatic multi-room merge step.

```
<house_name>/<room_name>/
```

- `<house_name>` — one folder per property. A short name (`my_house`) or an address-based name (`123_main_st`) both work — just keep it a single valid folder name (snake_case recommended, no spaces/slashes).
- `<room_name>` — one folder per room/location in that house (`hall`, `kitchen`, `bathroom`, `living_room`, etc.)

If the user hands over a video without explicitly naming **both** the house **and** the room, **ask** — never guess or default to an existing one, even if only one house/room currently exists. Naming the house but not the room (e.g. "put this in my_house") is not enough — always ask which room, and whether it's an existing room or a brand new one (e.g. "kitchen") that needs creating. Mixing footage of two different rooms into one reconstruction silently produces garbage (COLMAP will either fail to find a consistent pose solution, or worse, partially succeed with a nonsensical merged geometry).

If the user later wants multiple rooms combined into one walkable space, that's a manual step done *after* each room has its own finished splat — merge the finished `.ply` files with splat-transform/SuperSplat (see "Merging/editing splats afterward" below). Never merge raw frames from two different rooms through COLMAP+Brush together.

## Folder structure (inside one `<house_name>/<room_name>/`)

- `raw/` — original, untouched source video files as received (e.g. `IMG_2474.MOV`). Always copy/move the source video here first, before doing anything else — never extract frames directly from a file sitting loose in Downloads or elsewhere, so there's always an unmodified original kept on hand.
- `video1/`, `video2/`, ... (or a descriptive name like `wall_south/`, `corner_ne/` for a targeted supplementary clip) — each holds `frame_%05d.jpg` extracted from one raw video in `raw/`. Never reuse a name — a new video always gets its own subfolder so frame numbering never collides with an existing one.
- `images_combined/` — flat parent folder COLMAP actually reads. Contains one subfolder per `videoN/`, populated with **file-level symlinks** to the real frames (see gotcha below — do NOT symlink whole directories).
- `colmap_workspace/` — `database.db` + `sparse/0/` (`cameras.bin`, `images.bin`, `points3D.bin`) — COLMAP's output.
- `brush_dataset/` — `images/` (mirrors `images_combined`'s subfolder structure via symlinks, matching the relative paths COLMAP recorded) + `sparse` (symlink to `colmap_workspace/sparse`). This is the exact layout Brush's CLI expects.
- `brush_output/` — exported `.ply` splat files.

Example for two houses, each with a couple of rooms:

```
my_house/
  hall/
    raw/IMG_2474.MOV
    video1/ ... video2/ ...
    images_combined/  colmap_workspace/  brush_dataset/  brush_output/
  kitchen/
    raw/ ...
    ...
123_main_st/
  bathroom/
    raw/ ...
    ...
```

## Step-by-step commands

`<BASE>` below always means one specific `<house_name>/<room_name>/` folder — never the project root.

### 0. File the raw video

```bash
mkdir -p "<BASE>/raw"
mv "<incoming_video>" "<BASE>/raw/"
```

### 1. Inspect and extract frames

```bash
ffprobe -v error -show_entries format=duration -show_entries stream=width,height,r_frame_rate,codec_name -of default=noprint_wrappers=1 "<BASE>/raw/<file>"

mkdir -p "<BASE>/videoN"
ffmpeg -i "<BASE>/raw/<file>" -vf "fps=1,scale=iw:ih:flags=lanczos" -c:v mjpeg -q:v 2 -y "<BASE>/videoN/frame_%05d.jpg"
```

1 fps is the default sampling rate; raise it if the clip is short or coverage feels sparse.

### 1b. Blur filtering — NOT a default step, tried and reverted

`filter_blurry_frames.sh "<BASE>/videoN" [--percent 15]` exists (scores frames via ffmpeg's `blurdetect`, higher = blurrier — direction verified empirically) and moves the blurriest N% into `videoN/excluded_blurry/` (uses `mv`, not `rm`, so it's reversible).

**Tried this on the hall dataset (15% cut) and it made visual quality *worse*, not better** — more black spots / transparent gaps in the final splat, despite COLMAP's registration ratio improving (90.5%→93.2%) and per-frame sharpness presumably being higher. Likely explanation: Gaussian Splatting quality depends heavily on view *overlap/redundancy* (see capture technique notes above — "more capture positions = more parallax = more accuracy"), and cutting 15% of frames — even the blurriest ones — reduced coverage enough to hurt more than the sharpness gain helped, especially in already-thinly-covered areas (corners, etc.) where a "blurry" frame may have been the *only* coverage. Reverted; all 966 hall frames restored.

**Do not apply this automatically.** If revisited, treat it as an experiment to A/B compare, not an assumed improvement — and use a much smaller cut (a handful of truly unusable/near-unregisterable frames, not a blanket top-N%) rather than a percentage-based cut across the whole set.

### 2. Rebuild `images_combined/` with file-level symlinks

```bash
mkdir -p "<BASE>/images_combined/videoN"
for f in "<BASE>/videoN"/*.jpg; do ln -sf "$f" "<BASE>/images_combined/videoN/$(basename "$f")"; done
```

Do this for every `videoN/` folder that should be part of the reconstruction (existing + new), all within the same room's `<BASE>`.

### 3. Run COLMAP (always a full rebuild, not incremental)

**Pick the matcher based on dataset size first** — this matters a lot, see gotcha below:

- **Under ~500 images:** `exhaustive_matcher`. Simple, no extra files needed, finishes in well under a minute (confirmed: 385 images took ~29s).
- **~500+ images:** `vocab_tree_matcher` instead. Exhaustive matching is O(n²) and becomes impractical fast — confirmed: 966 images projected at 4-5+ **hours** with exhaustive vs. ~17 minutes total (matching + mapper) with vocab tree.
  - **The pretrained `demuc.de/colmap` vocab tree files are in the legacy FLANN format and crash this COLMAP version** (4.1.1 uses FAISS, switched May 2025 — loading a FLANN-format tree throws `Check failed: file_version == 1 || file_version == 2` and aborts). Don't download those.
  - Instead, **build a custom tree from the project's own already-extracted features** with `colmap vocab_tree_builder --database_path <db> --vocab_tree_path <out.bin> --num_visual_words 4096 --num_iterations 20` (needs `feature_extractor` to have already run). Takes a while (~30 min for ~1000 images) but only needs doing once.
  - **A custom-built tree at `~/.colmap/vocab_tree_hall_custom.bin` already exists** (built from `my_house/hall`'s images) — reuse it for any new video added to that same room (or any visually similar room) rather than rebuilding. Only build a new one if working in a visually very different space and reuse doesn't retrieve well.

```bash
rm -rf "<BASE>/colmap_workspace"
mkdir -p "<BASE>/colmap_workspace/sparse"

colmap feature_extractor \
  --database_path "<BASE>/colmap_workspace/database.db" \
  --image_path "<BASE>/images_combined" \
  --ImageReader.camera_model SIMPLE_RADIAL \
  --ImageReader.single_camera_per_folder 1

# under ~500 images:
colmap exhaustive_matcher \
  --database_path "<BASE>/colmap_workspace/database.db"

# ~500+ images, use this instead of exhaustive_matcher:
colmap vocab_tree_matcher \
  --database_path "<BASE>/colmap_workspace/database.db" \
  --VocabTreeMatching.vocab_tree_path ~/.colmap/vocab_tree_flickr100K_words32K.bin \
  --VocabTreeMatching.num_images 30

colmap mapper \
  --database_path "<BASE>/colmap_workspace/database.db" \
  --image_path "<BASE>/images_combined" \
  --output_path "<BASE>/colmap_workspace/sparse"
```

Check the result before moving on:

```bash
colmap model_analyzer --path "<BASE>/colmap_workspace/sparse/0"
```

Look at "Registered images" vs. total frame count. If COLMAP split the result into multiple numbered models (`sparse/0`, `sparse/1`, ...) instead of one, the capture had disconnected segments — see gotchas below.

### 4. Rebuild `brush_dataset/`

```bash
rm -rf "<BASE>/brush_dataset"
mkdir -p "<BASE>/brush_dataset/images"
for videoDir in "<BASE>/images_combined"/*/; do
  name=$(basename "$videoDir")
  mkdir -p "<BASE>/brush_dataset/images/$name"
  for f in "<BASE>/$name"/*.jpg; do ln -sf "$f" "<BASE>/brush_dataset/images/$name/$(basename "$f")"; done
done
ln -sf "<BASE>/colmap_workspace/sparse" "<BASE>/brush_dataset/sparse"
```

### 5. Run Brush training

```bash
mkdir -p "<BASE>/brush_output"
~/bin/brush_app "<BASE>/brush_dataset" \
  --with-viewer \
  --total-steps 15000 \
  --export-every 15000 \
  --export-path "<BASE>/brush_output" \
  --export-name "splat_<name>.ply" \
  > "<BASE>/brush_output/train_<name>.log" 2>&1 &
disown
```

- 5000 steps for a quick preview, 15000+ for a real result.
- If the source video was shot at 4K, `--max-resolution 3840` would use the full detail instead of the default 1920px cap — **but be careful with this on large datasets, see gotcha below: it caused severe memory thrashing on this Mac (16GB RAM) with 874 images.** Default to leaving `--max-resolution` unset (1920 default) unless the dataset is small (roughly under ~400 images) or you're actively monitoring memory.
- Long-running: launch in background, then poll with a `run_in_background` wait loop or `Monitor` rather than blocking.

### 6. Normalize the scene's up axis — always run this before viewing

COLMAP has no notion of true gravity, so the reconstruction's "up" is
usually tilted a few degrees off vertical. This is easy to miss by eye —
front/back views can look level while left/right views are visibly
tilted, since the further you orbit from the two azimuths where the tilt
happens to project to zero, the more roll shows up. Fix it in place, right
after training, before anyone looks at the result.

**Before running this, ask the user: do you have a `scan_metadata.json`
from Coverage Scout (or another ARKit-based capture app) for this room?**
Almost nobody using this pipeline will — it comes from a separate,
optional companion app — so "no" is the expected default answer, not
something to chase down. Branch on the answer:

**No ARKit metadata (default, works for everyone):**

```bash
python3 scripts/normalize_up.py \
  --colmap-sparse "<BASE>/colmap_workspace/sparse/0" \
  --ply "<BASE>/brush_output/splat_<name>.ply"
```

- How it works: averages every registered camera's own "up" direction from the COLMAP sparse model (a handheld walkthrough keeps the phone roughly upright on average, so this is a solid *statistical* estimate of true up), then rotates every splat's position *and* its own orientation quaternion to align that estimate with +Y — Brush's own documented vertical-axis convention.
- Sanity-check the printed correction angle: a few degrees is the expected case for a normal walking capture. Tens of degrees usually means the capture wasn't a level walkthrough (e.g. orbiting a tabletop object with the phone angled down) — the correction is less reliable in that case and the result is worth a visual check before trusting it blindly.

**Yes, ARKit metadata is available:**

```bash
python3 scripts/normalize_up.py \
  --colmap-sparse "<BASE>/colmap_workspace/sparse/0" \
  --ply "<BASE>/brush_output/splat_<name>.ply" \
  --arkit-metadata "<path/to/scan_metadata.json>" \
  --video-subfolder "<videoN whose footage is the ARKit-tracked one>" \
  --fps <the fps used in step 1's ffmpeg extraction for that video>
```

- How it works: ARKit's world Y-axis is gravity-true by construction (from the phone's IMU, not guessed from pixels). This mode matches COLMAP's registered frames to ARKit frames by reconstructed timestamp (frame number / fps), then solves for the rotation that best aligns COLMAP's camera-position trajectory onto ARKit's (a measured Kabsch/Wahba fit, not a statistical average) — generally more accurate than the default mode, especially for captures that aren't a level walkthrough.
- `--video-subfolder` is required and scopes matching to one `videoN/` folder deliberately: frame numbering restarts at 1 in every `videoN/` (see folder structure above), so without this a `video2/frame_00042.jpg` could silently match `video1`'s ARKit timestamp for the same frame number.
- `--fps` must match whatever `fps=<...>` was actually used in step 1 for that specific video — needed to reconstruct each COLMAP frame's timestamp from its filename.
- Sanity-check the printed match count and position-fit RMSD (in COLMAP's own arbitrary units) — the script requires at least 6 matched frames and errors out otherwise (usually means `--video-subfolder` or `--fps` is wrong).

**Either mode:**

- Requires `numpy` and `scipy` (`python3 -m pip install numpy scipy` if missing) and the `colmap` CLI on PATH (already required for step 3).
- Post-processes the finished `.ply` in place — does **not** require re-running COLMAP or Brush. Safe to run again later on an existing export.
- Pass `--out <path>` to write to a new file instead of overwriting; omit it to normalize in place.
- `scripts/test_normalize_up.py` is a dependency-free self-check for the alignment math (synthetic data, no real capture needed) — run it after touching `normalize_up.py`.

### 7. Open the finished splat for the user — automatically, locally, no internet

Once `brush_output/splat_<name>.ply` exists, package it into a single self-contained HTML file with this repo's own viewer and open it — this is the default "show the result" step, not optional/manual:

```bash
viewer/package.sh "<BASE>/brush_output/splat_<name>.ply"
open "<BASE>/brush_output/splat_<name>.html"   # macOS; use the OS equivalent elsewhere
```

This is entirely local — no server, no upload, no internet connection needed. It's the intended default every time training finishes, not something the user has to ask for separately.

- Why not just `~/bin/brush_app "<path/to/splat.ply>"` (Brush's own viewer)? It works, but its camera is arcball-style and can accumulate roll/tilt as you orbit — this repo's viewer uses a fixed-up-axis turntable instead (drag always yaws/pitches cleanly, never tilts).
- The deployed demo at `viewer/` on Vercel (see README) is a separate, standalone showcase of the viewer itself — it is **not** part of this per-result flow and nothing from a real run is ever uploaded there. Every actual result stays local.

## Known gotchas (do not repeat these mistakes)

- **Directory symlinks are invisible to COLMAP's scanner.** It does not follow symlinked directories when scanning `--image_path` recursively (only symlinked *files*). Always symlink individual files into real subdirectories, never `ln -s` a whole folder in as a shortcut.
- **Never point `--image_path` at a folder containing anything other than image subfolders.** It recursively picks up `.DS_Store`, `.ply`, `.db`, `.log`, raw video files, etc., and COLMAP's image decoder can **segfault** trying to read them as images. Always point it at the dedicated `images_combined/` folder that contains nothing but real image subfolders — never at a room's root (which also has `raw/`, `colmap_workspace/`, etc.) and never at the project root.
- **Never use `colmap automatic_reconstructor` with `--data_type video --quality high`.** This combination silently enables vocabulary-tree loop-closure detection, which segfaults without a downloaded vocab-tree resource file. Use the manual three-step pipeline instead (`feature_extractor` → `exhaustive_matcher` → `mapper`), which needs no external files.
- **`sequential_matcher` alone is not reliable for these captures.** It only matches temporally-nearby frames, which fragments the reconstruction into disconnected pieces whenever the capture has abrupt direction changes (e.g. center-spin → walk to corner → spin again) rather than one smooth continuous pan.
- **`exhaustive_matcher` does not scale.** It's O(n²) — fine under ~500 images (385 images: ~29s), but becomes impractical past that (966 images: projected 4-5+ hours, killed and redone). Use `vocab_tree_matcher` instead once a room's combined dataset gets into the many-hundreds — see the size-based rule and exact command in step 3 above. This is the correct default assumption for any room built from several long/high-res videos; don't default to exhaustive and find out the hard way.
- **Every new clip needs visual overlap with the existing footage of the same room**, or it becomes an orphaned disconnected reconstruction that COLMAP can't link to the rest. Start any targeted/close-up clip a step or two back (wide enough to still recognize the room) before moving in tight.
- **Never mix frames from two different rooms (or two different houses) into one COLMAP+Brush run.** Each `<house_name>/<room_name>/` is an independent pipeline. See "Multi-property organization" above.
- **Brush has no working checkpoint/resume mechanism** as of CLI v0.3.0 (no `--init-ply`/`--checkpoint-path` flag; `--start-iter` alone does nothing useful without a way to point it at saved state). Every training run starts fresh from the COLMAP sparse point cloud. This is fine at current dataset sizes (COLMAP: ~1-2 min for ~400 images; Brush: a few minutes for 15000 steps) — not worth engineering around unless a room's dataset grows into the thousands of frames.
- **Fresh COLMAP runs are not guaranteed to share a coordinate frame or scale with previous runs** (SfM has no absolute reference — origin/scale are arbitrary per run). This is why Brush training also has to restart from scratch each time a room's dataset changes, and why merging two separately-trained `.ply` splats requires manual/visual alignment, not blind concatenation.
- **Don't mix frames of the same room in different states** (e.g. furniture moved or removed between two capture sessions) into one COLMAP+Brush run. It causes ghosting/floater artifacts (the optimizer gets contradictory photometric supervision for the same 3D location). If the scene changed between sessions, train separate splats and merge the finished `.ply` files afterward (see below) instead of merging the raw frames.
- **Deleting/removing raw footage:** prefer moving to `~/.Trash` over `rm`, and always double-check for filename collisions (e.g. two different phones both producing an `IMG_2474.MOV`) before moving files between folders — compare file size/hash, don't assume same name means same file. Rename on conflict rather than silently overwriting.
- **Brush can silently thrash instead of crashing when it runs out of memory.** Confirmed: `--max-resolution 3840` with 874 images on a 16GB Mac ran for 1.5+ hours making no real progress — system had ~0.4% free RAM, process stuck in uninterruptible-sleep (I/O wait from swapping), CPU usage near-zero-to-erratic instead of the steady load a healthy training run shows. It doesn't error out on its own; it just quietly never finishes. **Check for this whenever a Brush run's elapsed time looks off**: `ps -p <pid> -o stat,%cpu` (state `U` = uninterruptible sleep = suspicious) and `memory_pressure | head -6` (free pages near zero = confirmed). If found: kill and restart at a lower `--max-resolution` (leave it unset/default 1920 is safe) — nothing is lost since Brush has no incremental checkpoint anyway (see above), so killing a thrashing run costs nothing but wall-clock time.
- **Always default to the leanest settings that are known to work reliably** (exhaustive vs vocab-tree matcher by size, default 1920 res unless dataset is small) rather than maximizing quality settings by default — push a setting up (resolution, step count) only when there's a specific reason to, and watch for the failure modes above when doing so.

## Preventing resource issues (do this proactively, not just when something looks stuck)

- **Preflight-check memory before launching any heavy step** (`feature_extractor` on many 4K images, `vocab_tree_builder`, and especially Brush training) — run `memory_pressure | head -6` first. If free pages are already low before you've even started, close other heavy apps or scale settings down before launching, rather than finding out an hour in.
- **Rule of thumb for this Mac (16GB RAM):** default `--max-resolution` (1920, unset) is safe regardless of dataset size. Only raise it for datasets under roughly ~400 images — above that, the combination of more images and higher per-image memory use is what caused the thrashing incident. If a bigger high-res run is genuinely needed, monitor actively (see below) rather than assuming it'll be fine.
- **While a long step is running in the background, periodically check both the process state and memory — not just "is it still alive."** A process can be alive and still not making progress:
  ```bash
  ps -p <pid> -o pid,%cpu,stat,etime,command
  memory_pressure | head -6
  ```
  Healthy: %CPU actively fluctuating in a meaningful range (not near-zero), free memory pages not near zero. Suspicious: `STAT` shows `U` (uninterruptible sleep) combined with near-zero/erratic %CPU and near-zero free pages — that's thrashing, not just "slow." Don't wait it out; kill and restart with lighter settings (see gotcha above — nothing is lost since neither COLMAP's mapper nor Brush training have usable partial/incremental output to preserve anyway).
- **This applies to any resource-heavy step, not just Brush** — if COLMAP's `feature_extractor` or `vocab_tree_builder` ever looks similarly stuck (elapsed time far past what similar-sized past runs took, per the timings recorded in this file), run the same two checks before assuming it just needs more time.

## Capture technique notes (relay to the user when relevant)

- **Continuous walking (translation) matters far more than standing and rotating in place.** SfM triangulates depth from camera *translation* between frames; pure rotation gives ~zero parallax. Never stop recording to do a static 360° spin — keep walking/moving the whole time.
- Good pattern for a room: walk the walls with the camera aimed toward the center (tilting up/down for ceiling/floor), then a second pass with the camera aimed at the walls themselves for surface/corner detail. Retracing the same path (loop closure) helps COLMAP correct drift.
- Go into corners deliberately — don't just orbit the middle, corners are easy to under-cover and give strong geometric anchors.
- If using an actual 360° camera (not just a regular phone), the operator's hand/arm/body will be visible in a consistent relative position across every frame — this breaks COLMAP's static-scene assumption (looks like a rigidly-moving object) and can corrupt pose estimation. Mask it out: COLMAP's `feature_extractor --mask_path` and Brush's `masks/` folder convention both support excluding fixed pixel regions. One reusable mask usually suffices since the rig geometry is constant.
- For a house exterior, a drone beats ground-level walking capture — orbit at multiple heights, gimbal-stabilized (less motion blur), and dedicated flight-planning apps can automate the overlap pattern.
- To exclude something from a reconstruction (e.g. a neighbor's yard in drone footage), the simplest fix is to just not point the camera at it. If it's unavoidably in-frame, per-frame masking is usually more effort than it's worth (the unwanted area moves around the frame every shot on an orbit) — easier to reconstruct everything and crop the unwanted region out of the finished splat afterward in SuperSplat's editor.
- **Transfer video without compression.** WhatsApp (and similar apps) re-encode/downscale video hard — verified in this project that a phone video came through WhatsApp at only 1024×576. Use AirDrop, USB cable, or an uncompressed cloud upload instead. This matters even more if shooting at 4K, since compression during transfer throws away exactly the detail 4K was meant to capture.
- Shooting 4K/30fps is worth it (more detail for COLMAP + Brush) — just remember to raise Brush's `--max-resolution` accordingly (see step 5).

## Cleaning up a splat (floaters, ghosting, collision mesh)

- **Always clip far-flung outlier points AND abnormally large-scale gaussians BEFORE running any voxel-based splat-transform operation** (`--filter-floaters`, `--collision-mesh`/voxel pipeline). Confirmed costly mistake, twice: running `--filter-floaters` on a raw COLMAP-derived splat with no clipping took **12h17m** (removed only 0.5% of gaussians — useless); clipping *position only* with `-B/--filter-box` still took **2h18m** (695-898m "scene" reported). Root cause has two parts, both from COLMAP/training noise visible in `--stats` output:
  1. A handful of far-outlier **positions** (`x`/`y`/`z` columns reaching ±100+ units when the real scene's median/mean cluster near 0).
  2. A handful of gaussians with absurd **scale** (`scale_0`/`scale_1`/`scale_2` reaching 100-300+ when the median is ~0.008) — `-B/--filter-box` only tests the gaussian's *center position*, not its rendered extent, so a huge-scale gaussian sitting near-origin still blows up the effective bounding box used for voxelization.
  - **The fix that actually worked (1m25s, not hours):** combine both — `-B -50,-50,-50,50,50,50 -V scale_0,lt,1 -V scale_1,lt,1 -V scale_2,lt,1 -F output.ply`. Position box AND per-axis scale filters together, before `-F`.
  - **Before filtering, always run `--stats` first** (`splat-transform input.ply null --stats`) and sanity-check both position *and* scale columns' min/max against median/mean — if either is wildly larger than the median±few-stdDev range, clip it.
  - **Cheap way to verify a clip actually worked before committing to the slow `-F` step**: dry-run the same filters with `null --stats` output (no voxelization, seconds not hours) and check the resulting position min/max look sane (roughly matches your box) before adding `-F` and waiting.
  - `--collision-mesh` combined with `--filter-cluster` (as in the recommended interior-scene pipeline) is less exposed to this because cluster-filtering already discards disconnected far points early — but plain `--filter-floaters` alone is not.
- **`--filter-floaters` alone is not a fix for ghosting.** It only removes isolated points not part of a solid voxel structure — true ghosting (semi-transparent duplicate/overlapping geometry near real surfaces, from inconsistent training data) is a different phenomenon it won't meaningfully touch. See "don't mix frames of the same room in different states" above — that's the actual fix for ghosting, not post-hoc filtering.
- **For a real walkthrough with collision (not just an orbit/fly viewer):** `splat-transform`'s own `.html` output does NOT include physics/collision — it's viewer-only. Use `@playcanvas/supersplat-viewer` instead:
  1. `git clone https://github.com/playcanvas/supersplat-viewer.git`, `npm install`, `npm run build` (generates a `public/` folder with the app).
  2. **Copy** (don't symlink — the bundled `serve` static server 404s on symlinks) the `.ply`/`.compressed.ply` and `.collision.glb` files into `public/`, plus a `settings.json` (see README for the schema/example).
  3. `npm run serve` (serves `public/`, prints the actual port — not always 3000).
  4. Open `http://localhost:<port>/?content=./scene.ply&collision=./scene.collision.glb`.
- **NVIDIA's [ArtiFixer](https://github.com/nv-tlabs/artifixer)** is a real, relevant tool for a different failure mode: under-observed areas (insufficient viewpoint coverage) that Gaussian Splatting extrapolates poorly. Uses video diffusion to generate plausible content for those areas. Needs a CUDA GPU (won't run on this Mac) — would need a cloud instance, same as the COLMAP/Brush CUDA discussion above.

## Merging/editing splats afterward

- **[splat-transform](https://github.com/playcanvas/splat-transform)** (CLI) — merges multiple `.ply` files with per-file transforms (rotate/scale/translate), e.g. `splat-transform -w a.ply -r 0,90,0 b.ply -s 1.2 merged.ply`.
- **[SuperSplat editor](https://superspl.at/editor)** (browser) — same engine, visual/interactive. Use this for eyeballing alignment when merging two splats that don't share a coordinate frame (e.g. combining two rooms of the same house into one walkable space), and for post-hoc cleanup (lasso/box select + delete) — e.g. cropping out something unwanted is much easier this way than per-frame masking during capture.
