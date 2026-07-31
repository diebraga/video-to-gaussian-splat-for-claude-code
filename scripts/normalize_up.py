#!/usr/bin/env python3
"""Level a Brush-exported .ply using the averaged camera "up" from COLMAP.

COLMAP has no notion of true gravity, so a reconstruction's "up" is often
tilted a few degrees off vertical -- this shows up as a level front/back
view but a tilted left/right view when orbiting the result. A handheld
walkthrough video keeps the phone roughly upright on average, so averaging
every registered camera's own up direction gives a solid estimate of true
up. We rotate the whole splat (positions and each splat's own orientation)
to align that estimate with +Y, Brush's own documented "up" convention.

This is a post-process on Brush's finished export -- it does not require
re-running COLMAP or Brush.

Usage:
    python3 normalize_up.py --colmap-sparse colmap_workspace/sparse/0 --ply path/to/splat.ply [--out path/to/output.ply]

--colmap-sparse takes COLMAP's binary sparse model directory as produced by
`colmap mapper` -- this script converts it to text internally via
`colmap model_converter`. Pass --colmap-images instead if you already have
a text-format images.txt and want to skip that conversion.
"""

import argparse
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
    args = ap.parse_args()

    if not args.colmap_sparse and not args.colmap_images:
        ap.error("one of --colmap-sparse or --colmap-images is required")

    out_path = args.out or args.ply

    if args.colmap_images:
        images_txt = args.colmap_images
        quats = read_camera_quaternions(images_txt)
    else:
        with tempfile.TemporaryDirectory() as tmp_dir:
            images_txt = colmap_sparse_to_images_txt(args.colmap_sparse, tmp_dir)
            quats = read_camera_quaternions(images_txt)

    avg_up = estimate_world_up(quats)
    correction = rotation_aligning(avg_up, np.array([0.0, 1.0, 0.0]))

    print(f"Estimated world up (from {len(quats)} cameras): {avg_up}")
    print(f"Correction rotation: {np.degrees(correction.magnitude()):.2f} degrees")

    normalize_ply(args.ply, out_path, correction)
    print(f"Wrote normalized ply to {out_path}")


if __name__ == "__main__":
    main()
