"""One-time migration: upload existing local clips to Supabase Storage.

Run from the project root:
    venv/Scripts/python.exe backend/migrate_to_supabase.py

The script is idempotent: objects already in the bucket are skipped, and local
MP4s are deleted only after a successful cloud upload is verified.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import cloud  # noqa: E402
import library  # noqa: E402
from clips import probe_size  # noqa: E402

CLIP_DIR = ROOT / "work" / "clips"


def main() -> None:
    if not cloud.enabled():
        raise SystemExit("Supabase is not configured in .env")
    details = library.load_job_details()
    uploaded = skipped = missing = 0
    for job_id, detail in details.items():
        clips = detail.get("clips") or []
        changed = False
        for i, clip in enumerate(clips):
            if clip.get("render_status") not in ("done", "draft") and not clip.get("storage_path"):
                continue
            local = CLIP_DIR / f"{job_id}_clip_{i}.mp4"
            dest = clip.get("storage_path") or f"{job_id}/{i}.mp4"
            if cloud.file_exists(dest):
                if not clip.get("storage_path"):
                    clip["storage_path"] = dest
                    clip["cloud"] = True
                    changed = True
                skipped += 1
                if local.exists():
                    try:
                        local.unlink()
                    except OSError:
                        pass
                continue
            if not local.exists():
                missing += 1
                continue
            cloud.upload_file(local, dest)
            if not cloud.file_exists(dest):
                raise RuntimeError(f"Upload verification failed for {dest}")
            clip["storage_path"] = dest
            clip["cloud"] = True
            clip["size_bytes"] = local.stat().st_size
            try:
                clip["width"], clip["height"] = probe_size(str(local))
            except Exception:  # noqa: BLE001
                pass
            changed = True
            uploaded += 1
            try:
                local.unlink()
            except OSError:
                pass
        if changed or any(c.get("storage_path") for c in clips):
            library.save_job_detail(job_id, detail)
            library.upsert_job(detail)
            cloud.save_job(detail)
            cloud.save_clips(job_id, clips)
    print(f"uploaded={uploaded} already_cloud={skipped} local_missing={missing}")


if __name__ == "__main__":
    main()
