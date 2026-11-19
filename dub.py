#!/usr/bin/env python3
"""
Creator Dubbing Pipeline
------------------------
Turn one creator video into ready-to-post versions in other languages,
using the ElevenLabs Dubbing API.

  1. Upload the original video to ElevenLabs and create a dubbing project.
  2. Add a language target for each language you want.
  3. Wait for each dub to finish, then download the dubbed audio and subtitles.
  4. Put the dubbed audio back onto the original video with ffmpeg,
     so every language comes out as a finished MP4.

Usage:
  python dub.py video.mp4 --languages es
  python dub.py video.mp4 --languages es pt hi --out output
  python dub.py --watch "~/Google Drive/Dubbing/Inbox" --languages es

Requires:
  - An ElevenLabs API key in the ELEVENLABS_API_KEY environment variable
  - ffmpeg installed and on your PATH
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

API_BASE = "https://api.elevenlabs.io/v1/dubbing"
VIDEO_TYPES = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
POLL_SECONDS = 15
TIMEOUT_SECONDS = 60 * 60  # give up on a single dub after one hour


# ---------------------------------------------------------------- API helpers

def api_headers() -> dict:
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        sys.exit("Set your ElevenLabs API key first:  export ELEVENLABS_API_KEY=your_key")
    return {"xi-api-key": key}


def check(response: requests.Response) -> dict:
    if response.status_code >= 400:
        sys.exit(f"ElevenLabs API error {response.status_code}: {response.text}")
    return response.json()


def create_project(video: Path, first_language: str, source_language: str | None) -> dict:
    """Upload the video and create a dubbing project with its first language target."""
    data = {"target_language": first_language, "reference": video.name}
    if source_language:
        data["source_language"] = source_language
    with video.open("rb") as f:
        resp = requests.post(
            f"{API_BASE}/project",
            headers=api_headers(),
            data=data,
            files={"file": (video.name, f)},
            timeout=600,
        )
    return check(resp)


def get_project(project_id: str) -> dict:
    return check(requests.get(f"{API_BASE}/project/{project_id}", headers=api_headers(), timeout=60))


def add_language(project_id: str, language: str) -> dict:
    resp = requests.post(
        f"{API_BASE}/project/{project_id}/language",
        headers=api_headers(),
        json={"target_language": language},
        timeout=60,
    )
    return check(resp)


def get_language(project_id: str, language_id: str) -> dict:
    return check(
        requests.get(f"{API_BASE}/project/{project_id}/language/{language_id}", headers=api_headers(), timeout=60)
    )


def wait_for(fetch, done: str, label: str) -> dict:
    """Poll until status == done. Stops on 'failed' or after the timeout."""
    start = time.time()
    while True:
        item = fetch()
        status = item.get("status")
        if status == done:
            return item
        if status == "failed":
            sys.exit(f"{label} failed: {item.get('error')}")
        if time.time() - start > TIMEOUT_SECONDS:
            sys.exit(f"{label} timed out (last status: {status})")
        print(f"  {label}: {status} ...")
        time.sleep(POLL_SECONDS)


def download(url: str, dest: Path) -> Path:
    with requests.get(url, stream=True, timeout=600) as r:
        r.raise_for_status()
        with dest.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    return dest


# ------------------------------------------------------------- video assembly

def mux(video: Path, audio: Path, out: Path) -> Path:
    """Replace the original audio track with the dubbed one. The video stream is copied untouched."""
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(video), "-i", str(audio),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-shortest", str(out),
    ]
    subprocess.run(cmd, check=True)
    return out


# ------------------------------------------------------------------- pipeline

def dub_video(video: Path, languages: list[str], out_dir: Path, source_language: str | None = None) -> list[Path]:
    print(f"\nDubbing {video.name} into: {', '.join(languages)}")
    out_dir.mkdir(parents=True, exist_ok=True)

    project = create_project(video, languages[0], source_language)
    project_id = project["project_id"]
    print(f"  Project created: {project_id}")

    project = wait_for(lambda: get_project(project_id), "ready", "Preparing source")
    language_ids = {}
    for lang_id in project.get("language_ids", []):
        target = get_language(project_id, lang_id)
        language_ids[target["target_language"]] = lang_id

    for lang in languages:
        if lang not in language_ids:
            language_ids[lang] = add_language(project_id, lang)["language_id"]

    finished = []
    for lang, lang_id in language_ids.items():
        target = wait_for(lambda: get_language(project_id, lang_id), "completed", f"Dubbing [{lang}]")
        outputs = target.get("outputs") or {}
        stem = f"{video.stem}_{lang}"
        audio = download(outputs["lossless_audio"], out_dir / f"{stem}.flac")
        if outputs.get("subtitles"):
            download(outputs["subtitles"], out_dir / f"{stem}.vtt")
        final = mux(video, audio, out_dir / f"{stem}.mp4")
        print(f"  Done: {final}")
        finished.append(final)
    return finished


def watch(folder: Path, languages: list[str], out_dir: Path, source_language: str | None) -> None:
    """Watch a folder (for example a Google Drive folder synced to your computer).
    Every new video dropped in gets dubbed, then moved to a 'done' subfolder."""
    done_dir = folder / "done"
    done_dir.mkdir(exist_ok=True)
    print(f"Watching {folder} for new videos. Press Ctrl+C to stop.")
    while True:
        for video in sorted(folder.iterdir()):
            if video.is_file() and video.suffix.lower() in VIDEO_TYPES:
                dub_video(video, languages, out_dir, source_language)
                shutil.move(str(video), done_dir / video.name)
        time.sleep(30)


def main() -> None:
    parser = argparse.ArgumentParser(description="Dub creator videos into other languages with ElevenLabs.")
    parser.add_argument("video", nargs="?", help="Path to a video file")
    parser.add_argument("--languages", nargs="+", default=["es"], help="Target languages, e.g. es pt hi (default: es)")
    parser.add_argument("--source-language", help="Language spoken in the original (auto-detected if omitted)")
    parser.add_argument("--out", default="output", help="Folder for the finished videos (default: output)")
    parser.add_argument("--watch", help="Folder to watch for new videos instead of dubbing a single file")
    args = parser.parse_args()

    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg is required. Install it from https://ffmpeg.org (Mac: brew install ffmpeg).")

    out_dir = Path(args.out).expanduser()
    if args.watch:
        watch(Path(args.watch).expanduser(), args.languages, out_dir, args.source_language)
    elif args.video:
        dub_video(Path(args.video).expanduser(), args.languages, out_dir, args.source_language)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
