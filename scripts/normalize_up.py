#!/usr/bin/env python3
"""Level a Brush-exported .ply using the averaged camera "up" from COLMAP,
or (more accurately) by aligning to ARKit's gravity-true camera poses if a
Coverage Scout scan_metadata.json is available.

COLMAP has no notion of true gravity, so a reconstruction's "up" is often
tilted a few degrees off vertical -- this shows up as a level front/back
view but a tilted left/right view when orbiting the result.

Default mode (no ARKit data): a handheld walkthrough video keeps the phone
roughly upright on average, so averaging every registered camera's own up
direction gives a solid *statistical* estimate of true up.

--arkit-metadata mode: if the room was captured with an app that records
ARKit camera poses alongside the video (e.g. this project's own Coverage
Scout), those poses are gravity-true by construction (ARKit's world Y-axis
is aligned to gravity via the device's IMU, not guessed from pixels). We
match COLMAP's registered frames to ARKit frames by timestamp and solve for
the rotation that best aligns COLMAP's camera-position trajectory onto
ARKit's (Kabsch/Wahba alignment) -- a *measured* correction instead of a
statistical one. Most people running this pipeline won't have this file
(it comes from a separate, optional companion app) -- the default
COLMAP-only mode is the one everyone can use.

Either way, we rotate the whole splat (positions and each splat's own
orientation) to align the estimated up with +Y, Brush's own documented "up"
convention. This is a post-process on Brush's finished export -- it does
not require re-running COLMAP or Brush.

Usage:
    python3 normalize_up.py --colmap-sparse colmap_workspace/sparse/0 --ply path/to/splat.ply [--out path/to/output.ply]

    # with ARKit ground truth (Coverage Scout or similar):
    python3 normalize_up.py --colmap-sparse colmap_workspace/sparse/0 --ply path/to/splat.ply \\
        --arkit-metadata scan_metadata.json --video-subfolder video1 --fps 1

--colmap-sparse takes COLMAP's binary sparse model directory as produced by
`colmap mapper` -- this script converts it to text internally via
`colmap model_converter`. Pass --colmap-images instead if you already have
a text-format images.txt and want to skip that conversion.
"""

import argparse
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

PLY_TYPE_TO_NUMPY = {
    "float": "<f4",
    "float32": "<f4",
    "double": "<f8",
    "uchar": "<u1",
    "int": "<i4",
    "uint": "<u4",
    "short": "<i2",
    "ushort": "<u2",
}


def colmap_sparse_to_images_txt(sparse_dir, tmp_dir):
    """Convert a COLMAP binary sparse model to text format via the CLI,
    returning the path to the resulting images.txt."""
    if shutil.which("colmap") is None:
        raise RuntimeError("colmap CLI not found on PATH -- required to read the binary sparse model")
    subprocess.run(
        [
            "colmap",
            "model_converter",
            "--input_path",
            str(sparse_dir),
            "--output_path",
            str(tmp_dir),
            "--output_type",
            "TXT",
        ],
        check=True,
        capture_output=True,
    )
    return Path(tmp_dir) / "images.txt"


def read_camera_quaternions(images_txt_path):
    quats = []
    with open(images_txt_path, "r") as f:
        lines = f.readlines()
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("#") or not line.strip():
            i += 1
            continue
        parts = line.split()
        quats.append((float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4])))
        i += 2  # each image entry is followed by a POINTS2D line
    if not quats:
        raise ValueError(f"No camera poses found in {images_txt_path}")
    return np.array(quats)


def estimate_world_up(quats_wxyz):
    # Empirically verified against this project's own COLMAP output (see
    # docs/superpowers/specs): R @ (0, 1, 0) gives a near-pure-Y result for
    # a normal walking capture, confirming this is the right convention for
    # how qvec is stored here -- verify with a print of avg_up if you ever
    # port this to a different COLMAP/Brush version.
    xyzw = quats_wxyz[:, [1, 2, 3, 0]]  # scipy wants (x, y, z, w)
    rots = Rotation.from_quat(xyzw)
    world_ups = rots.apply(np.array([0.0, 1.0, 0.0]))
    avg_up = world_ups.mean(axis=0)
    norm = np.linalg.norm(avg_up)
    if norm < 1e-8:
        raise ValueError("Camera up vectors cancelled out -- can't estimate a stable up direction")
    return avg_up / norm


def read_colmap_frames_full(images_txt_path):
    """Return [{'name', 'center'}] for every registered COLMAP image, where
    'center' is the camera's position in COLMAP's world frame (meters, but
    COLMAP's own arbitrary scale/origin -- not real-world meters)."""
    frames = []
    with open(images_txt_path, "r") as f:
        lines = f.readlines()
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("#") or not line.strip():
            i += 1
            continue
        parts = line.split()
        qw, qx, qy, qz = (float(parts[k]) for k in range(1, 5))
        tx, ty, tz = (float(parts[k]) for k in range(5, 8))
        name = parts[9]
        # COLMAP stores world-to-camera: x_cam = R_wc @ x_world + t.
        # Camera center in world coords: C = -R_wc^T @ t.
        r_wc = Rotation.from_quat([qx, qy, qz, qw])
        center = -r_wc.apply(np.array([tx, ty, tz]), inverse=True)
        frames.append({"name": name, "center": center})
        i += 2  # each image entry is followed by a POINTS2D line
    if not frames:
        raise ValueError(f"No camera poses found in {images_txt_path}")
    return frames


def read_arkit_frames(metadata_path):
    """Return [{'t', 'position'}] for every 'normal'-tracking-state frame in
    a Coverage Scout (or compatible) scan_metadata.json."""
    with open(metadata_path, "r") as f:
        data = json.load(f)
    frames = []
    for fr in data.get("frames", []):
        if fr.get("tracking_state") != "normal":
            continue
        ct = fr["camera_transform"]
        position = np.array([ct[0][3], ct[1][3], ct[2][3]], dtype=np.float64)
        frames.append({"t": float(fr["video_timestamp_seconds"]), "position": position})
    if not frames:
        raise ValueError(f"No 'normal'-tracking-state frames found in {metadata_path}")
    return frames


def match_colmap_to_arkit(colmap_frames, arkit_frames, video_subfolder, fps, max_dt):
    """Match COLMAP-registered frames from one video_subfolder (e.g.
    'video1') to ARKit frames by reconstructed timestamp (frame number /
    fps), keeping only matches within max_dt seconds. Returns two aligned
    (N, 3) arrays: colmap centers and their matched ARKit positions.

    Scoped to a single video_subfolder deliberately: frame numbering restarts
    at 1 in every videoN/ folder (see AGENTS.md), so matching across folders
    by frame number alone would silently pair frames from unrelated clips.
    """
    prefix = f"{video_subfolder}/"
    arkit_ts = np.array([f["t"] for f in arkit_frames])
    colmap_pts, arkit_pts = [], []
    for cf in colmap_frames:
        if not cf["name"].startswith(prefix):
            continue
        m = re.search(r"frame_(\d+)\.\w+$", cf["name"])
        if not m:
            continue
        frame_num = int(m.group(1))
        ts = (frame_num - 1) / fps
        idx = int(np.argmin(np.abs(arkit_ts - ts)))
        if abs(arkit_ts[idx] - ts) > max_dt:
            continue
        colmap_pts.append(cf["center"])
        arkit_pts.append(arkit_frames[idx]["position"])
    if len(colmap_pts) < 6:
        raise ValueError(
            f"Only {len(colmap_pts)} COLMAP<->ARKit frame matches within {max_dt:.3f}s "
            f"(need >= 6 for a stable alignment) -- check --video-subfolder and --fps are correct"
        )
    return np.array(colmap_pts), np.array(arkit_pts)


def align_via_arkit(colmap_pts, arkit_pts):
    """Best-fit rotation mapping COLMAP's camera-position trajectory onto
    ARKit's gravity-true one (Kabsch/Wahba alignment on mean-centered
    points -- translation and scale are deliberately not solved for; only
    the rotation is applied to the splat, matching the up-only mode's
    scope). Returns (Rotation, rmsd)."""
    colmap_c = colmap_pts - colmap_pts.mean(axis=0)
    arkit_c = arkit_pts - arkit_pts.mean(axis=0)
    return Rotation.align_vectors(arkit_c, colmap_c)


def rotation_aligning(a, b):
    """Minimal rotation that maps unit vector a onto unit vector b."""
    a = a / np.linalg.norm(a)
    b = b / np.linalg.norm(b)
    axis = np.cross(a, b)
    s = np.linalg.norm(axis)
    c = np.dot(a, b)
    if s < 1e-8:
        if c > 0:
            return Rotation.identity()
        perp = np.array([1.0, 0.0, 0.0]) if abs(a[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
        axis = np.cross(a, perp)
        axis = axis / np.linalg.norm(axis)
        return Rotation.from_rotvec(axis * np.pi)
    axis = axis / s
    angle = np.arctan2(s, c)
    return Rotation.from_rotvec(axis * angle)


def read_ply_header(f):
    if f.readline().strip() != b"ply":
        raise ValueError("Not a .ply file")
    fmt_line = f.readline().strip()
    if fmt_line != b"format binary_little_endian 1.0":
        raise ValueError(f"Unsupported ply format: {fmt_line!r}")
    header_lines = [b"ply", fmt_line]
    vertex_count = None
    props = []
    while True:
        line = f.readline()
        header_lines.append(line.rstrip(b"\n"))
        stripped = line.strip()
        if stripped.startswith(b"element vertex"):
            vertex_count = int(stripped.split()[-1])
        elif stripped.startswith(b"property"):
            parts = stripped.split()
            props.append((parts[1].decode(), parts[2].decode()))
        elif stripped == b"end_header":
            break
    if vertex_count is None or not props:
        raise ValueError("Malformed ply header")
    return header_lines, vertex_count, props


def normalize_ply(ply_in_path, ply_out_path, correction: Rotation):
    with open(ply_in_path, "rb") as f:
        header_lines, vertex_count, props = read_ply_header(f)
        dtype = np.dtype([(name, PLY_TYPE_TO_NUMPY[ptype]) for ptype, name in props])
        data = np.fromfile(f, dtype=dtype, count=vertex_count)

    field_names = {name for _, name in props}
    for required in ("x", "y", "z", "rot_0", "rot_1", "rot_2", "rot_3"):
        if required not in field_names:
            raise ValueError(f"ply is missing required field '{required}'")

    xyz = np.stack([data["x"], data["y"], data["z"]], axis=1).astype(np.float64)
    xyz_new = correction.apply(xyz)
    data["x"] = xyz_new[:, 0].astype(np.float32)
    data["y"] = xyz_new[:, 1].astype(np.float32)
    data["z"] = xyz_new[:, 2].astype(np.float32)

    # rot_0..3 stored as (w, x, y, z) -- the 3DGS reference-format convention.
    quat_wxyz = np.stack([data["rot_0"], data["rot_1"], data["rot_2"], data["rot_3"]], axis=1).astype(np.float64)
    old_rots = Rotation.from_quat(quat_wxyz[:, [1, 2, 3, 0]])
    new_xyzw = (correction * old_rots).as_quat()
    data["rot_0"] = new_xyzw[:, 3].astype(np.float32)
    data["rot_1"] = new_xyzw[:, 0].astype(np.float32)
    data["rot_2"] = new_xyzw[:, 1].astype(np.float32)
    data["rot_3"] = new_xyzw[:, 2].astype(np.float32)

    with open(ply_out_path, "wb") as f:
        f.write(b"\n".join(header_lines) + b"\n")
        data.tofile(f)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--colmap-sparse", help="Path to COLMAP's binary sparse model dir (e.g. colmap_workspace/sparse/0)")
    ap.add_argument("--colmap-images", help="Path to COLMAP's text-format images.txt (skips conversion)")
    ap.add_argument("--ply", required=True, help="Path to the Brush-exported .ply to normalize")
    ap.add_argument("--out", help="Output path (default: overwrite --ply in place)")
    ap.add_argument(
        "--arkit-metadata",
        help="Path to a Coverage Scout (or compatible) scan_metadata.json. If given, aligns to ARKit's "
             "gravity-true poses instead of averaging COLMAP's own cameras -- more accurate, but optional: "
             "most captures won't have this file, and the default mode above works fine without it.",
    )
    ap.add_argument(
        "--video-subfolder",
        help="Name of the images_combined/<name> folder that came from the ARKit-tracked video (e.g. 'video1'). "
             "Required with --arkit-metadata.",
    )
    ap.add_argument(
        "--fps",
        type=float,
        help="Frame extraction rate used in step 1 (ffmpeg -vf fps=<...>) for the ARKit-tracked video. "
             "Required with --arkit-metadata, to recover each COLMAP frame's timestamp from its filename.",
    )
    ap.add_argument(
        "--max-time-diff",
        type=float,
        default=None,
        help="Max seconds between a COLMAP frame's timestamp and its matched ARKit frame "
             "(default: half the frame interval, 0.5/fps).",
    )
    args = ap.parse_args()

    if not args.colmap_sparse and not args.colmap_images:
        ap.error("one of --colmap-sparse or --colmap-images is required")
    if args.arkit_metadata and (not args.video_subfolder or not args.fps):
        ap.error("--arkit-metadata requires both --video-subfolder and --fps")

    out_path = args.out or args.ply

    with tempfile.TemporaryDirectory() as tmp_dir:
        images_txt = args.colmap_images or colmap_sparse_to_images_txt(args.colmap_sparse, tmp_dir)

        if args.arkit_metadata:
            colmap_frames = read_colmap_frames_full(images_txt)
            arkit_frames = read_arkit_frames(args.arkit_metadata)
            max_dt = args.max_time_diff if args.max_time_diff is not None else 0.5 / args.fps
            colmap_pts, arkit_pts = match_colmap_to_arkit(
                colmap_frames, arkit_frames, args.video_subfolder, args.fps, max_dt
            )
            correction, rmsd = align_via_arkit(colmap_pts, arkit_pts)
            print(f"Aligned via {len(colmap_pts)} matched ARKit<->COLMAP frame pairs (position fit rmsd: {rmsd:.4f})")
        else:
            quats = read_camera_quaternions(images_txt)
            avg_up = estimate_world_up(quats)
            correction = rotation_aligning(avg_up, np.array([0.0, 1.0, 0.0]))
            print(f"Estimated world up (from {len(quats)} cameras): {avg_up}")

    print(f"Correction rotation: {np.degrees(correction.magnitude()):.2f} degrees")

    normalize_ply(args.ply, out_path, correction)
    print(f"Wrote normalized ply to {out_path}")


if __name__ == "__main__":
    main()
