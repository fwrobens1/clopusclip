"""Visual signal: classify gameplay vs IRL and find the subject to punch in on.

Uses OpenCV YuNet face detection on a few sampled frames per candidate:
  - gameplay: exactly one small facecam face in a corner, lots of on-screen UI
  - IRL: multiple / large faces, a real-world scene
Returns a gameplay score and a suggested 9:16 crop centre.
"""
from __future__ import annotations

import subprocess
import shutil
from pathlib import Path

import cv2
import numpy as np

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
MODEL = Path(__file__).resolve().parent.parent / "models" / "face_detection_yunet_2023mar.onnx"
_detector = None


def detector():
    global _detector
    if _detector is None:
        _detector = cv2.FaceDetectorYN_create(str(MODEL), "", (320, 320), score_threshold=0.6)
    return _detector


def sample_frames(video_url: str, times: list[float], width: int = 640) -> list[np.ndarray]:
    """Fetch a few frames via ffmpeg remote seek (no full download)."""
    from sources import _resolve_media_url
    media_url, headers = _resolve_media_url(video_url)
    frames = []
    for t in times:
        cmd = [FFMPEG, "-v", "error", *headers, "-ss", f"{max(0, t):.2f}",
               "-i", media_url, "-frames:v", "1", "-vf", f"scale={width}:-1",
               "-f", "image2pipe", "-vcodec", "png", "-"]
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode == 0 and r.stdout:
            arr = np.frombuffer(r.stdout, np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is not None:
                frames.append(img)
    return frames


def analyze_frame(img: np.ndarray) -> dict:
    h, w = img.shape[:2]
    det = detector()
    det.setInputSize((w, h))
    _, faces = det.detect(img)
    faces = [] if faces is None else faces.tolist()

    big_faces = [f for f in faces if f[2] >= w * 0.08 or f[3] >= h * 0.12]
    corner_faces = [f for f in faces if (f[0] < w * 0.35 or f[0] > w * 0.65) and f[1] < h * 0.45]

    # On-screen UI / high-frequency detail: gameplay has dense hard edges.
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    edges = float(cv2.Laplacian(gray, cv2.CV_32F).var())

    # Gameplay heuristic: one small face pinned in a corner + dense UI edges.
    gameplay = 0.0
    if len(faces) == 1 and corner_faces and not big_faces:
        gameplay += 0.6
    if edges > 250:
        gameplay += 0.25
    if len(big_faces) >= 2:
        gameplay -= 0.7
    gameplay = max(0.0, min(1.0, gameplay))

    if faces:
        target = corner_faces[0] if (gameplay > 0.5 and corner_faces) else max(faces, key=lambda f: f[2] * f[3])
        face_cx = (target[0] + target[2] / 2) / w
        cy = (target[1] + target[3] / 2) / h
        if gameplay > 0.5:
            # Keep the corner facecam visible at the edge of the vertical crop
            # but leave room for the gameplay action next to it.
            cx = min(0.5, face_cx + 0.22) if face_cx < 0.5 else max(0.5, face_cx - 0.22)
        else:
            cx = float(np.median([(f[0] + f[2] / 2) / w for f in faces]))
    else:
        cx, cy = 0.5, 0.5
    return {"gameplay": round(gameplay, 2), "faces": len(faces), "edges": round(edges, 1),
            "cx": round(cx, 3), "cy": round(cy, 3)}


def analyze_url(video_url: str, times: list[float]) -> dict:
    frames = sample_frames(video_url, times)
    if not frames:
        return {"gameplay": 0.0, "faces": 0, "cx": 0.5, "cy": 0.5, "samples": 0}
    results = [analyze_frame(f) for f in frames]
    return {
        "gameplay": round(float(np.mean([r["gameplay"] for r in results])), 2),
        "faces": max(r["faces"] for r in results),
        "cx": round(float(np.median([r["cx"] for r in results])), 3),
        "cy": round(float(np.median([r["cy"] for r in results])), 3),
        "samples": len(results),
        "detail": results,
    }
