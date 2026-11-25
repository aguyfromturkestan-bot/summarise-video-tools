# summarise-video-tools

`vidtools.py` is the helper script for the `summarise-video` Claude skill. The skill downloads it at a pinned commit and checks its SHA-256 before running it, so changing this file doesn't change the skill until the skill is updated too.

Commands:

- `fetch`: metadata and captions for a URL (yt-dlp) or local file
- `transcribe`: faster-whisper speech-to-text
- `frames`: keyframe contact sheets
- `normalize`: subtitles or pasted transcripts to timestamped text
- `check`: verify quotes and timestamps in a summary
- `mentions`: actor and theme co-mention counts

Requires Python 3, ffmpeg, and `pip install yt-dlp faster-whisper pillow numpy`.
