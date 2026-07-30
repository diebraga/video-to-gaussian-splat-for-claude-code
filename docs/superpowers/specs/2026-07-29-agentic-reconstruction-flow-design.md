# Agentic Reconstruction Flow — Design

Date: 2026-07-29

## Purpose

Turn this repository into a self-contained, agent-driven pipeline: any coding
agent (Claude Code, Codex, Gemini CLI, etc.) that gets pointed at this repo —
locally or via a dropped GitHub link — should be able to read `AGENTS.md`,
check for/install the tools it needs, take a video from the user, and hand
back a viewable Gaussian splat, entirely on its own, with no manual setup
steps from the user beyond approving installs and dropping a video.

This replaces the current house/room-oriented workflow with a simpler,
generic one, and removes everything related to collision-mesh generation and
splat merging (out of scope for this flow).

## Repo cleanup (one-time)

- Delete `Sources/` (stray Swift/AR file, unrelated to this pipeline) and
  `target/` (stray build artifacts).
- Rename `coverage_scout_tests/` → `example/`. This keeps the existing
  `object_01` test data (frames, COLMAP workspace, brush_dataset,
  brush_output) in place as a worked example agents/users can inspect.
- `.gitignore` covers `my_house/` and every runtime data folder produced by
  the pipeline (`raw/`, `videoN/`, `images_combined/`, `colmap_workspace/`,
  `brush_dataset/`, `brush_output/` under any project folder) — these stay on
  disk locally but are never committed.
- `git init` this folder and make a first commit. No GitHub remote is
  created and nothing is pushed as part of this work — that's a separate,
  later step the user does themselves.

## Folder structure (replaces house/room)

```
<project_name>/
  object_1/
    raw/               original source video(s)
    video1/            frames extracted from one raw video
    images_combined/   flat symlink structure COLMAP reads
    colmap_workspace/  database.db + sparse/0/
    brush_dataset/     layout Brush's CLI expects
    brush_output/      exported splat_object1.ply
  object_2/
    ...
```

`<project_name>` is asked for once per project; `object_N` is auto-numbered
per capture within that project. Neither name is required from the user —
see "New project intake" below for the default-naming behavior.

## Language matching

The agent conducts the entire flow (prompts, checklists, error messages) in
whichever language the user is writing in. If a user opens the repo and
writes in Portuguese, the agent responds in Portuguese from the first
message onward, including all checklist labels and prompts. This is a
standing instruction in `AGENTS.md`, not a one-time translation step.

## Dependency requirements & install flow

### Required tools

| Tool    | macOS                          | Windows                                  |
|---------|---------------------------------|-------------------------------------------|
| ffmpeg  | Homebrew (`brew install ffmpeg`) | Chocolatey (`choco install ffmpeg`)        |
| COLMAP  | Homebrew (`brew install colmap`) | Direct download from COLMAP's GitHub Releases (Windows zip) — no Chocolatey package exists |
| Brush   | Direct download from Brush's GitHub Releases (`aarch64-apple-darwin` build) — no Homebrew formula exists | Direct download from Brush's GitHub Releases (Windows build) — no Chocolatey package exists |

Chocolatey itself (and Homebrew on Mac) is installed first if missing, since
it's needed to get ffmpeg either way.

### Check → ask → install flow

1. Agent checks for each tool (`which`/`where` the binary, or the package
   manager itself if that's also missing).
2. If everything is present, skip straight to "New project intake."
3. If anything is missing, the agent asks **one combined permission
   question** listing everything it needs to install (package manager +
   ffmpeg + COLMAP + Brush, whichever subset is actually missing) — not one
   question per tool.
4. If the user declines, the agent clearly states it cannot run the
   reconstruction without these tools and stops there (no partial install).
5. If the user accepts, the agent installs everything needed in sequence
   without asking again per-tool, confirms success for each, then tells the
   user setup is finished and moves to "New project intake."

## New project intake

Once dependencies are confirmed ready, the agent tells the user setup is
done and asks them to drop a video. When a video arrives, the agent asks for
a project name and an object name. If the user doesn't provide one or both:

- Project name defaults to something reasonable and unique (e.g. based on
  today's date) rather than blocking on an answer.
- Object name defaults to auto-numbered `object_1`, incrementing to
  `object_2`, `object_3`, ... for subsequent captures dropped into the same
  project.

## Pipeline execution with checklist

Steps, run in order:

1. Extract frames (ffmpeg)
2. Structure-from-motion (COLMAP — matcher chosen by image count, per the
   existing exhaustive-vs-vocab-tree rule)
3. Train the splat (Brush)
4. Generate and open the viewer

After each step completes, the agent prints an updated Markdown checklist to
the chat, e.g.:

```
- [x] Extract frames
- [x] COLMAP reconstruction
- [ ] Brush training
- [ ] Open viewer
```

If a step fails, the agent reports what went wrong in plain terms and asks
whether to retry that specific step — it does not silently restart the
whole pipeline from scratch.

All existing operational guardrails carry over unchanged: the
exhaustive/vocab-tree matcher rule by image count, the memory-thrashing
detection checks (`ps -p <pid> -o stat,%cpu`, `memory_pressure`) before and
during COLMAP/Brush runs, and the capture-technique notes.

## Final viewer

A small, static, dependency-free HTML+JS Gaussian-splat viewer is vendored
into the repo once (compiled ahead of time — no npm/Node needed at run
time).

Technical constraint: browsers block `fetch()` of local files over the
`file://` scheme, so the viewer can't simply be pointed at a `.ply` path.
Fix: for each finished reconstruction, the agent generates a one-off copy of
the vendored viewer with the `.ply` data embedded inline as base64 (using
tools already built into the OS — `base64` on macOS, `[Convert]::ToBase64String`
via PowerShell on Windows — no new dependency), then opens that generated
file directly in the default browser. No local server, no CORS issue, no
manual drag-and-drop, no account.

## Removed from AGENTS.md

- "Cleaning up a splat (floaters, ghosting, collision mesh)" — depends on
  `splat-transform`/Node, which this flow deliberately does not add.
- "Merging/editing splats afterward" — same dependency, and the
  house/room→single-space-merge use case goes away with the folder
  structure change.

Everything else (matcher-choice rule, thrashing/memory checks, capture
technique notes, the `filter_blurry_frames.sh` note that blur filtering was
tried and reverted) is preserved as-is.
