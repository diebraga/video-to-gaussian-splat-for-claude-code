#!/usr/bin/env python3
"""Self-check for normalize_up.py's ARKit-alignment math. No real capture
data needed -- builds a synthetic COLMAP/ARKit trajectory pair related by a
known rotation and checks the alignment recovers it. Run directly:

    python3 scripts/test_normalize_up.py
"""

import numpy as np
from scipy.spatial.transform import Rotation

from normalize_up import align_via_arkit, match_colmap_to_arkit


def test_align_via_arkit_recovers_known_rotation():
    rng = np.random.default_rng(0)
    true_rotation = Rotation.from_euler("xyz", [4, 12, -7], degrees=True)
    colmap_pts = rng.normal(size=(30, 3)) * 2.0  # arbitrary COLMAP-frame trajectory
    # ARKit sees the same physical trajectory, rotated + offset by an
    # arbitrary origin/translation (COLMAP and ARKit don't share an origin).
    arkit_pts = true_rotation.apply(colmap_pts) + np.array([5.0, -1.0, 2.0])

    estimated, rmsd = align_via_arkit(colmap_pts, arkit_pts)

    angle_error_deg = (estimated * true_rotation.inv()).magnitude() * 180 / np.pi
    assert angle_error_deg < 0.01, f"recovered rotation off by {angle_error_deg:.4f} degrees"
    assert rmsd < 1e-6, f"unexpectedly high rmsd for noiseless data: {rmsd}"


def test_align_via_arkit_tolerates_noise():
    rng = np.random.default_rng(1)
    true_rotation = Rotation.from_euler("xyz", [2, -30, 15], degrees=True)
    colmap_pts = rng.normal(size=(40, 3)) * 3.0
    noise = rng.normal(scale=0.01, size=(40, 3))
    arkit_pts = true_rotation.apply(colmap_pts) + np.array([-2.0, 0.5, 1.0]) + noise

    estimated, rmsd = align_via_arkit(colmap_pts, arkit_pts)

    angle_error_deg = (estimated * true_rotation.inv()).magnitude() * 180 / np.pi
    assert angle_error_deg < 1.0, f"recovered rotation off by {angle_error_deg:.4f} degrees under noise"


def test_match_colmap_to_arkit_scopes_to_video_subfolder_and_timestamp():
    fps = 1.0
    # video1: frames 1..8 at t=0..7s, positions walk along +x.
    colmap_frames = [
        {"name": f"video1/frame_{n:05d}.jpg", "center": np.array([float(n - 1), 0.0, 0.0])}
        for n in range(1, 9)
    ]
    # video2 reuses the same frame numbers (own sequence) -- must NOT match
    # against video1's ARKit timestamps despite the numeric collision.
    colmap_frames += [
        {"name": f"video2/frame_{n:05d}.jpg", "center": np.array([99.0, float(n), 0.0])}
        for n in range(1, 4)
    ]
    arkit_frames = [{"t": float(t), "position": np.array([float(t), 10.0, 0.0])} for t in range(8)]

    colmap_pts, arkit_pts = match_colmap_to_arkit(colmap_frames, arkit_frames, "video1", fps, max_dt=0.4)

    assert len(colmap_pts) == 8, f"expected all 8 video1 frames matched, got {len(colmap_pts)}"
    assert np.allclose(arkit_pts[:, 1], 10.0), "matched the wrong ARKit frames"


def test_match_colmap_to_arkit_rejects_too_few_matches():
    colmap_frames = [{"name": "video1/frame_00001.jpg", "center": np.array([0.0, 0.0, 0.0])}]
    arkit_frames = [{"t": 100.0, "position": np.array([0.0, 0.0, 0.0])}]  # far outside max_dt
    try:
        match_colmap_to_arkit(colmap_frames, arkit_frames, "video1", fps=1.0, max_dt=0.4)
        raise AssertionError("expected ValueError for insufficient matches")
    except ValueError:
        pass


if __name__ == "__main__":
    test_align_via_arkit_recovers_known_rotation()
    test_align_via_arkit_tolerates_noise()
    test_match_colmap_to_arkit_scopes_to_video_subfolder_and_timestamp()
    test_match_colmap_to_arkit_rejects_too_few_matches()
    print("All normalize_up.py self-checks passed.")
