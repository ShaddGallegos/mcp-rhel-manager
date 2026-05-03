# HAL Studio

> **aider-chat (optional):** Install separately — `pip3 install --upgrade aider-chat`. aider-chat hard-pins `filelock==3.20.3`, which conflicts with `virtualenv` (requires `filelock>=3.24.2`) and `tox`. After a system-wide install, restore the required version: `pip3 install --upgrade "filelock>=3.24.2"`. The project venv is isolated and unaffected.

HAL Studio adds stock watching, image tools, and video tools to HAL.
It now also adds OBS and LibreOffice integration.
It now also includes AI image generation, TTS vocals, and one-shot OBS scene pipelines.

## Launch

From HAL:

python3 hal.py --studio

One-shot pipeline from HAL:

python3 hal.py --studio-pipeline "launch teaser with futuristic server room" --studio-image-backend auto

Direct:

python3 hal-studio.py menu

Split scripts:

python3 hal-stocks.py menu
python3 hal-multimedia.py

## Capabilities Check

python3 hal-studio.py capabilities

## Stocks

Quote:

python3 hal-stocks.py quote AAPL

Watch with threshold alert:

python3 hal-stocks.py watch AAPL --above 280 --interval 60 --checks 120

Watch downside:

python3 hal-stocks.py watch AAPL --below 250 --interval 60 --checks 120

Notes:

- Primary quote source is Yahoo Finance endpoint.
- Fallback quote source is Stooq CSV endpoint.

## Image Tools

Describe image metadata:

python3 hal-studio.py image-describe /path/to/image.png

Describe image and run OCR:

python3 hal-studio.py image-describe /path/to/image.png --ocr

Resize image:

python3 hal-studio.py image-resize in.png out.png --width 1280 --height 720

Generate a simple poster image:

python3 hal-studio.py image-poster --text "Quarterly Ops Review" --output poster.png

Generate image from prompt text:

python3 hal-studio.py text-image "sunrise over server racks" --output prompt-art.png

High-quality AI image generation:

python3 hal-studio.py ai-image "cinematic cyberpunk datacenter at sunrise" --backend auto --output ai-image.png

Notes:

- `--backend diffusers` uses local Diffusers/Torch pipeline when installed.
- `--backend ollama-assisted` uses local Ollama to refine prompt, then renders locally.
- Diffusers model can be overridden via `HAL_DIFFUSERS_MODEL`.

## Video Tools

Video metadata:

python3 hal-studio.py video-info /path/to/video.mp4

Trim a clip:

python3 hal-studio.py video-trim in.mp4 out.mp4 --start 00:01:00 --duration 00:00:30

## Music Tools

Generate WAV music from prompt text:

python3 hal-studio.py text-music "calm focus coding beat" --output prompt-music.wav --seconds 20 --bpm 120

Notes:

- Music generation is local and deterministic from your prompt.
- Output format is WAV for wide compatibility.

Generate vocals from text (TTS):

python3 hal-studio.py text-speech "Welcome to the HAL live demo" --output vocals.wav

Layer vocals over generated music:

python3 hal-studio.py music-vocals scene-music.wav vocals.wav --output final-mix.wav

Notes:

- Uses `espeak-ng` or `espeak` by default if present.
- Uses `piper` if installed and `HAL_PIPER_MODEL` is set.

## OBS Integration

Show OBS status:

python3 hal-studio.py obs-status

Launch OBS:

python3 hal-studio.py obs-launch

Launch OBS minimized to tray:

python3 hal-studio.py obs-launch --minimized

List recent recordings:

python3 hal-studio.py obs-recordings --limit 10

Notes:

- Flatpak installs are detected automatically.
- Default recordings directory is ~/Videos.

## OBSBOT Integration

Show OBSBOT camera status:

python3 hal-studio.py obsbot-status

List controls:

python3 hal-studio.py obsbot-controls --device /dev/video0

Set a control:

python3 hal-studio.py obsbot-set zoom_absolute 180 --device /dev/video0

Notes:

- If OBSBOT is connected but appears with a generic device name, use `--device` explicitly.
- `v4l2-ctl` is required for controls and settings.

## Auto Prompt Pipeline

Create full OBS-ready scene assets from one prompt:

python3 hal-studio.py pipeline "launch teaser with futuristic server room" --outdir ./hal-scenes --seconds 20 --bpm 120 --image-backend auto

Pipeline output includes:

- scene-image.png
- scene-music.wav
- scene-vocals.wav (if TTS engine available)
- scene-audio-final.wav
- obs-scene-assets.json
- OBS-IMPORT-STEPS.txt

## LibreOffice Integration

Convert an office file to PDF:

python3 hal-studio.py lo-convert /path/to/file.docx --format pdf --outdir /tmp

Convert with a different output format:

python3 hal-studio.py lo-convert /path/to/file.odt --format html --outdir /tmp

Notes:

- Uses headless LibreOffice/soffice command execution.
- Flatpak installs are detected automatically.
- HAL now verifies that the output file was actually created.

## Interactive Menu

HAL Studio menu options:

1) Stock quote
2) Stock watch alert
3) Describe image
4) Resize image
5) Generate simple poster image
6) Video info
7) Trim video
8) OBS status
9) OBS launch
10) OBS recordings list
11) LibreOffice convert
12) Text to image
13) Text to music
14) Capabilities
15) OBSBOT status
16) OBSBOT list controls
17) OBSBOT set control
18) AI image generate
19) Text to speech
20) Mix music + vocals
21) Auto prompt pipeline
22) Exit

## Dependencies

Detected and used when available:

- requests
- pillow
- ffmpeg
- ffprobe
- tesseract (for OCR)
- obs (native or flatpak)
- libreoffice/soffice (native or flatpak)
- espeak-ng/espeak (for TTS)
- piper (optional, advanced TTS)
- diffusers + torch (for high-quality local image generation)
- ollama (optional prompt enhancement backend)
