#!/usr/bin/env python3
"""HAL Studio: stocks, images, and video utilities."""

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import wave
from datetime import datetime, timezone
from typing import Any, Dict, Optional

try:
    import requests
except Exception:
    requests = None


_DIFFUSERS_PIPELINE = None
_DIFFUSERS_PIPELINE_MODEL = None


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _require_pillow():
    try:
        from PIL import Image, ImageDraw, ImageFont  # noqa: F401
        return True
    except Exception:
        return False


def list_capabilities() -> Dict[str, bool]:
    return {
        "requests": requests is not None,
        "pillow": _require_pillow(),
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "ffprobe": shutil.which("ffprobe") is not None,
        "tesseract": shutil.which("tesseract") is not None,
        "obs": _obs_command() is not None,
        "libreoffice": _libreoffice_command() is not None,
        "obsbot": _find_obsbot_devices() != [],
        "v4l2-ctl": shutil.which("v4l2-ctl") is not None,
        "espeak": shutil.which("espeak-ng") is not None or shutil.which("espeak") is not None,
        "piper": shutil.which("piper") is not None,
        "ollama": shutil.which("ollama") is not None,
        "diffusers": _have_python_module("diffusers"),
        "torch": _have_python_module("torch"),
    }


def _have_python_module(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except Exception:
        return False


def print_capabilities() -> int:
    caps = list_capabilities()
    print("HAL Studio capabilities")
    print("-" * 40)
    for name, enabled in caps.items():
        print(f"{name:12s}: {'yes' if enabled else 'no'}")
    return 0


def _fetch_quote(symbol: str) -> Optional[Dict[str, Any]]:
    if requests is None:
        return None
    symbol = symbol.upper().strip()
    url = "https://query1.finance.yahoo.com/v7/finance/quote"
    try:
        response = requests.get(url, params={"symbols": symbol}, timeout=10)
        response.raise_for_status()
        data = response.json()
        items = data.get("quoteResponse", {}).get("result", [])
        if items:
            return items[0]
    except Exception:
        pass

    # Fallback source: Stooq CSV endpoint
    try:
        fallback = requests.get(
            "https://stooq.com/q/l/",
            params={"s": f"{symbol.lower()}.us", "f": "sd2t2ohlcv", "h": "", "e": "csv"},
            timeout=10,
        )
        fallback.raise_for_status()
        lines = [line.strip() for line in fallback.text.splitlines() if line.strip()]
        if len(lines) < 2:
            return None
        headers = [h.strip().lower() for h in lines[0].split(",")]
        values = [v.strip() for v in lines[1].split(",")]
        row = {headers[i]: values[i] for i in range(min(len(headers), len(values)))}

        close_value = row.get("close", "")
        if close_value in ("", "N/D"):
            return None

        price = float(close_value)
        return {
            "shortName": symbol,
            "regularMarketPrice": price,
            "regularMarketPreviousClose": None,
            "regularMarketChange": None,
            "regularMarketChangePercent": None,
            "marketState": "FALLBACK",
            "currency": "USD",
        }
    except Exception:
        return None


def stock_quote(symbol: str) -> int:
    quote = _fetch_quote(symbol)
    if not quote:
        print(f"Unable to retrieve quote for {symbol.upper()}.")
        return 1

    name = quote.get("shortName") or quote.get("longName") or symbol.upper()
    price = quote.get("regularMarketPrice")
    prev = quote.get("regularMarketPreviousClose")
    chg = quote.get("regularMarketChange")
    pct = quote.get("regularMarketChangePercent")
    state = quote.get("marketState", "UNKNOWN")
    currency = quote.get("currency", "USD")

    print("Stock quote")
    print("-" * 40)
    print(f"Symbol     : {symbol.upper()}")
    print(f"Name       : {name}")
    print(f"Price      : {price if price is not None else 'N/A'} {currency}")
    if chg is None or pct is None:
        print("Change     : N/A")
    else:
        print(f"Change     : {chg} ({pct}%)")
    print(f"Prev close : {prev if prev is not None else 'N/A'} {currency}")
    print(f"Market     : {state}")
    print(f"As of      : {_now_utc()}")
    return 0


def stock_watch(symbol: str, above: Optional[float], below: Optional[float], interval: int, checks: int) -> int:
    if above is None and below is None:
        print("You must set --above and/or --below.")
        return 2

    symbol = symbol.upper().strip()
    print(f"Watching {symbol} every {interval}s for {checks} checks...")
    print("Press Ctrl+C to stop.")

    try:
        for idx in range(checks):
            quote = _fetch_quote(symbol)
            if not quote:
                print(f"[{idx + 1}/{checks}] quote unavailable")
            else:
                price = quote.get("regularMarketPrice")
                print(f"[{idx + 1}/{checks}] {symbol}: {price}")
                if isinstance(price, (int, float)):
                    if above is not None and price >= above:
                        print(f"ALERT: {symbol} reached {price} >= {above}")
                        return 0
                    if below is not None and price <= below:
                        print(f"ALERT: {symbol} reached {price} <= {below}")
                        return 0
            if idx + 1 < checks:
                time.sleep(max(1, interval))
    except KeyboardInterrupt:
        print("Stopped.")
        return 0

    print("No alert threshold hit in this watch window.")
    return 0


def image_describe(path: str, with_ocr: bool) -> int:
    if not os.path.isfile(path):
        print(f"Image not found: {path}")
        return 2

    info: Dict[str, Any] = {
        "path": path,
        "bytes": os.path.getsize(path),
        "modified": datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M:%S"),
    }

    if _require_pillow():
        from PIL import Image
        with Image.open(path) as img:
            info["format"] = img.format
            info["mode"] = img.mode
            info["width"] = img.width
            info["height"] = img.height
    else:
        info["format"] = "unknown (Pillow not installed)"

    print("Image description")
    print("-" * 40)
    for key in ["path", "bytes", "modified", "format", "mode", "width", "height"]:
        if key in info:
            print(f"{key:10s}: {info[key]}")

    if with_ocr:
        if shutil.which("tesseract") is None:
            print("ocr       : skipped (tesseract not installed)")
        else:
            try:
                proc = subprocess.run(
                    ["tesseract", path, "stdout", "--dpi", "300"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                text = (proc.stdout or "").strip()
                if text:
                    preview = " ".join(text.split())
                    if len(preview) > 220:
                        preview = preview[:220] + "..."
                    print(f"ocr       : {preview}")
                else:
                    print("ocr       : no text detected")
            except Exception as exc:
                print(f"ocr       : failed ({exc})")

    return 0


def image_resize(src: str, dst: str, width: int, height: int) -> int:
    if not os.path.isfile(src):
        print(f"Image not found: {src}")
        return 2
    if not _require_pillow():
        print("Pillow is required for image editing.")
        return 2

    from PIL import Image

    with Image.open(src) as img:
        resized = img.resize((width, height))
        resized.save(dst)

    print(f"Saved resized image: {dst} ({width}x{height})")
    return 0


def image_poster(text: str, output: str, width: int, height: int) -> int:
    if not _require_pillow():
        print("Pillow is required to generate images.")
        return 2

    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (width, height), color=(16, 24, 40))
    draw = ImageDraw.Draw(img)

    for y in range(height):
        intensity = int(40 + (120 * y / max(1, height - 1)))
        draw.line([(0, y), (width, y)], fill=(12, intensity // 2, intensity))

    font = ImageFont.load_default()
    wrapped = []
    line = []
    for word in text.split():
        test = " ".join(line + [word])
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] <= width - 40:
            line.append(word)
        else:
            if line:
                wrapped.append(" ".join(line))
            line = [word]
    if line:
        wrapped.append(" ".join(line))

    y = max(20, (height - len(wrapped) * 14) // 2)
    for item in wrapped:
        bbox = draw.textbbox((0, 0), item, font=font)
        text_w = bbox[2] - bbox[0]
        x = max(20, (width - text_w) // 2)
        draw.text((x, y), item, fill=(240, 245, 250), font=font)
        y += 16

    stamp = f"HAL Studio {datetime.now().strftime('%Y-%m-%d')}"
    draw.text((20, height - 30), stamp, fill=(180, 200, 220), font=font)

    img.save(output)
    print(f"Generated image: {output}")
    return 0


def text_to_image(prompt: str, output: str, width: int, height: int) -> int:
    if not _require_pillow():
        print("Pillow is required to generate images.")
        return 2

    from PIL import Image, ImageDraw, ImageFont

    digest = hashlib.sha256(prompt.encode("utf-8")).digest()
    base_a = (digest[0], digest[1], digest[2])
    base_b = (digest[3], digest[4], digest[5])

    img = Image.new("RGB", (width, height), color=(0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Deterministic gradient based on prompt hash.
    for y in range(height):
        t = y / max(1, height - 1)
        r = int(base_a[0] * (1 - t) + base_b[0] * t)
        g = int(base_a[1] * (1 - t) + base_b[1] * t)
        b = int(base_a[2] * (1 - t) + base_b[2] * t)
        draw.line([(0, y), (width, y)], fill=(r, g, b))

    # Add abstract deterministic geometry from hash bytes.
    for i in range(12):
        p = digest[(i * 2) % len(digest)]
        q = digest[(i * 2 + 1) % len(digest)]
        x = int((p / 255.0) * width)
        y = int((q / 255.0) * height)
        rad = 20 + (digest[(i + 8) % len(digest)] % 120)
        alpha_color = (
            255 - digest[(i + 10) % len(digest)],
            digest[(i + 11) % len(digest)],
            digest[(i + 12) % len(digest)],
        )
        draw.ellipse([(x - rad, y - rad), (x + rad, y + rad)], outline=alpha_color, width=2)

    font = ImageFont.load_default()
    words = prompt.split()
    lines = []
    current = []
    max_w = width - 40
    for word in words:
        test = " ".join(current + [word])
        box = draw.textbbox((0, 0), test, font=font)
        if box[2] <= max_w:
            current.append(word)
        else:
            if current:
                lines.append(" ".join(current))
            current = [word]
    if current:
        lines.append(" ".join(current))

    y = max(24, height - 28 - len(lines) * 16)
    for line in lines[:10]:
        box = draw.textbbox((0, 0), line, font=font)
        x = max(20, (width - (box[2] - box[0])) // 2)
        draw.text((x, y), line, fill=(245, 245, 245), font=font)
        y += 16

    img.save(output)
    print(f"Generated image from prompt: {output}")
    return 0


def _get_ollama_url() -> str:
    return os.environ.get("OLLAMA_URL", "http://localhost:11434")


def _ollama_refine_prompt(prompt: str, model: Optional[str]) -> str:
    if requests is None:
        return prompt

    model_name = model or os.environ.get("HAL_OLLAMA_PROMPT_MODEL", "qwen2.5:7b")
    system_text = (
        "You are an expert visual prompt engineer. "
        "Return one concise, detailed image-generation prompt only. "
        "No markdown, no explanation."
    )
    body = {
        "model": model_name,
        "stream": False,
        "prompt": (
            f"System: {system_text}\n"
            f"User prompt: {prompt}\n"
            "Return improved prompt:"
        ),
    }
    try:
        resp = requests.post(f"{_get_ollama_url().rstrip('/')}/api/generate", json=body, timeout=90)
        resp.raise_for_status()
        data = resp.json()
        refined = (data.get("response") or "").strip()
        if refined:
            return refined
    except Exception:
        return prompt
    return prompt


def _diffusers_pipeline(model_id: Optional[str]):
    global _DIFFUSERS_PIPELINE, _DIFFUSERS_PIPELINE_MODEL
    chosen = model_id or os.environ.get("HAL_DIFFUSERS_MODEL", "stabilityai/stable-diffusion-2-1-base")
    if _DIFFUSERS_PIPELINE is not None and _DIFFUSERS_PIPELINE_MODEL == chosen:
        return _DIFFUSERS_PIPELINE

    from diffusers.pipelines.stable_diffusion.pipeline_stable_diffusion import StableDiffusionPipeline
    import torch

    use_cuda = torch.cuda.is_available()
    dtype = torch.float16 if use_cuda else torch.float32
    pipe = StableDiffusionPipeline.from_pretrained(chosen, torch_dtype=dtype)
    if use_cuda:
        pipe = pipe.to("cuda")
    else:
        pipe = pipe.to("cpu")

    _DIFFUSERS_PIPELINE = pipe
    _DIFFUSERS_PIPELINE_MODEL = chosen
    return pipe


def ai_image_generate(
    prompt: str,
    output: str,
    width: int,
    height: int,
    backend: str,
    model: Optional[str],
    steps: int,
    guidance: float,
) -> int:
    selected = backend
    if selected == "auto":
        selected = "diffusers" if (_have_python_module("diffusers") and _have_python_module("torch")) else "ollama-assisted"

    if selected == "diffusers":
        try:
            pipe = _diffusers_pipeline(model)
            result: Any = pipe(
                prompt,
                num_inference_steps=max(10, steps),
                guidance_scale=max(1.0, guidance),
                width=width,
                height=height,
            )
            image: Any = None
            if hasattr(result, 'images') and getattr(result, 'images', None):
                image = result.images[0]
            elif isinstance(result, tuple) and len(result) > 0:
                first = result[0]
                image = first[0] if isinstance(first, list) else first
            if image is None or not hasattr(image, 'save'):
                raise RuntimeError('Diffusers returned unsupported result format')
            image.save(output)
            print(f"Generated AI image (diffusers): {output}")
            return 0
        except Exception as exc:
            print(f"Diffusers generation failed ({exc}); falling back to deterministic renderer.")
            return text_to_image(prompt, output, width, height)

    if selected == "ollama-assisted":
        refined = _ollama_refine_prompt(prompt, model)
        return text_to_image(refined, output, width, height)

    if selected == "poster":
        return text_to_image(prompt, output, width, height)

    print(f"Unknown backend: {selected}")
    return 2


def text_to_music(prompt: str, output: str, seconds: int, bpm: int) -> int:
    if seconds < 2 or seconds > 300:
        print("seconds must be between 2 and 300")
        return 2
    if bpm < 40 or bpm > 240:
        print("bpm must be between 40 and 240")
        return 2

    sample_rate = 44100
    total_samples = seconds * sample_rate
    digest = hashlib.sha256(prompt.encode("utf-8")).digest()

    # Build a deterministic scale and melody sequence from the prompt hash.
    root_hz = 110 + (digest[0] % 8) * 15
    major_steps = [0, 2, 4, 5, 7, 9, 11]
    melody = []
    for i in range(64):
        step = major_steps[digest[i % len(digest)] % len(major_steps)]
        octave = 1 + (digest[(i + 9) % len(digest)] % 3)
        semitone = step + octave * 12
        freq = root_hz * (2 ** (semitone / 12.0))
        melody.append(freq)

    beat_seconds = 60.0 / bpm
    note_seconds = beat_seconds / 2.0
    note_samples = max(1, int(note_seconds * sample_rate))

    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)

        for n in range(total_samples):
            note_index = (n // note_samples) % len(melody)
            f1 = melody[note_index]
            f2 = f1 * 0.5

            # Basic envelope for smoother note transitions.
            phase = (n % note_samples) / note_samples
            if phase < 0.08:
                env = phase / 0.08
            elif phase > 0.85:
                env = (1.0 - phase) / 0.15
            else:
                env = 1.0
            env = max(0.0, min(1.0, env))

            t = n / sample_rate
            sample = 0.62 * math.sin(2 * math.pi * f1 * t) + 0.38 * math.sin(2 * math.pi * f2 * t)
            sample *= 0.28 * env
            int_sample = int(max(-1.0, min(1.0, sample)) * 32767)
            wav.writeframes(struct.pack("<h", int_sample))

    print(f"Generated music from prompt: {output}")
    return 0


def text_to_speech(text: str, output: str, voice: Optional[str], speed: int) -> int:
    piper = shutil.which("piper")
    espeak = shutil.which("espeak-ng") or shutil.which("espeak")

    if piper and os.environ.get("HAL_PIPER_MODEL"):
        model_path = os.environ.get("HAL_PIPER_MODEL")
        cmd = [piper, "-m", model_path, "-f", output]
        if voice:
            cmd.extend(["--speaker", voice])
        proc = subprocess.run(cmd, input=text, text=True, capture_output=True)
        if proc.returncode == 0:
            print(f"Generated vocals with Piper: {output}")
            return 0
        print(proc.stderr.strip() or "Piper TTS failed")
        return proc.returncode

    if espeak:
        cmd = [espeak, "-s", str(max(80, min(300, speed))), "-w", output]
        if voice:
            cmd.extend(["-v", voice])
        cmd.append(text)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0:
            print(f"Generated vocals with eSpeak: {output}")
            return 0
        print(proc.stderr.strip() or "eSpeak TTS failed")
        return proc.returncode

    print("No local TTS engine found. Install espeak-ng or piper.")
    return 2


def mix_music_and_vocals(music_path: str, vocals_path: str, output: str) -> int:
    if not os.path.isfile(music_path):
        print(f"Music track not found: {music_path}")
        return 2
    if not os.path.isfile(vocals_path):
        print(f"Vocals track not found: {vocals_path}")
        return 2

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        shutil.copy2(music_path, output)
        print("ffmpeg not installed; copied music track without vocal layer.")
        return 0

    cmd = [
        ffmpeg,
        "-y",
        "-i",
        music_path,
        "-i",
        vocals_path,
        "-filter_complex",
        "[0:a]volume=0.75[a0];[1:a]volume=1.35[a1];[a0][a1]amix=inputs=2:duration=longest:dropout_transition=2",
        "-c:a",
        "pcm_s16le",
        output,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stderr.strip() or "Audio mix failed")
        return proc.returncode
    print(f"Created mixed track: {output}")
    return 0


def _safe_slug(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return slug[:48] or "prompt"


def prompt_pipeline(
    prompt: str,
    outdir: str,
    seconds: int,
    bpm: int,
    image_backend: str,
    image_model: Optional[str],
    voice: Optional[str],
) -> int:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    scene_dir = os.path.join(outdir, f"hal-scene-{ts}-{_safe_slug(prompt)}")
    os.makedirs(scene_dir, exist_ok=True)

    image_path = os.path.join(scene_dir, "scene-image.png")
    music_path = os.path.join(scene_dir, "scene-music.wav")
    vocals_path = os.path.join(scene_dir, "scene-vocals.wav")
    final_audio = os.path.join(scene_dir, "scene-audio-final.wav")

    rc = ai_image_generate(prompt, image_path, 1280, 720, image_backend, image_model, 30, 7.5)
    if rc != 0:
        return rc

    rc = text_to_music(prompt, music_path, seconds, bpm)
    if rc != 0:
        return rc

    tts_rc = text_to_speech(prompt, vocals_path, voice, 165)
    if tts_rc == 0:
        mix_rc = mix_music_and_vocals(music_path, vocals_path, final_audio)
        if mix_rc != 0:
            return mix_rc
    else:
        shutil.copy2(music_path, final_audio)
        with open(os.path.join(scene_dir, "tts-warning.txt"), "w", encoding="utf-8") as handle:
            handle.write("TTS engine was not available. scene-audio-final.wav contains music only.\n")

    manifest = {
        "created_at": _now_utc(),
        "prompt": prompt,
        "assets": {
            "image": image_path,
            "music": music_path,
            "vocals": vocals_path if os.path.exists(vocals_path) else None,
            "final_audio": final_audio,
        },
        "obs_scene": {
            "canvas": {"width": 1920, "height": 1080},
            "sources": [
                {"name": "HAL Scene Image", "type": "image", "path": image_path, "fit": "center"},
                {"name": "HAL Scene Audio", "type": "media", "path": final_audio, "loop": True},
            ],
        },
    }
    manifest_path = os.path.join(scene_dir, "obs-scene-assets.json")
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)

    with open(os.path.join(scene_dir, "OBS-IMPORT-STEPS.txt"), "w", encoding="utf-8") as handle:
        handle.write(
            "1) In OBS, create a new scene named HAL Auto Scene.\n"
            "2) Add Image Source -> scene-image.png\n"
            "3) Add Media Source -> scene-audio-final.wav (enable Loop)\n"
            "4) Fit image source to screen (Ctrl+F).\n"
        )

    print("Prompt pipeline completed")
    print("-" * 40)
    print(f"Scene directory: {scene_dir}")
    print(f"Image         : {image_path}")
    print(f"Final audio   : {final_audio}")
    print(f"Manifest      : {manifest_path}")
    return 0


def video_info(path: str) -> int:
    if not os.path.isfile(path):
        print(f"Video not found: {path}")
        return 2
    if shutil.which("ffprobe") is None:
        print("ffprobe is not installed.")
        return 2

    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration,bit_rate,size:stream=codec_name,codec_type,width,height,avg_frame_rate",
        "-of",
        "json",
        path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stderr.strip() or "ffprobe failed")
        return 1

    data = json.loads(proc.stdout)
    print(json.dumps(data, indent=2))
    return 0


def video_trim(src: str, dst: str, start: str, duration: str) -> int:
    if not os.path.isfile(src):
        print(f"Video not found: {src}")
        return 2
    if shutil.which("ffmpeg") is None:
        print("ffmpeg is not installed.")
        return 2

    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        start,
        "-i",
        src,
        "-t",
        duration,
        "-c",
        "copy",
        dst,
    ]
    proc = subprocess.run(cmd)
    return proc.returncode


def _audio_duration_seconds(audio_path: str) -> float:
    """Return duration of audio file in seconds using ffprobe, or 0 on failure."""
    if shutil.which("ffprobe") is None:
        return 0.0
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                audio_path,
            ],
            capture_output=True, text=True, timeout=15,
        )
        return float(proc.stdout.strip())
    except Exception:
        return 0.0


def video_build(
    images: list,
    audio: Optional[str],
    output: str,
    fps: int = 25,
    width: int = 1280,
    height: int = 720,
) -> int:
    """Build an MP4 video from one or more images and an optional audio track.

    Single image  → loops for the duration of the audio (or 10 s if no audio).
    Multiple images → slideshow: each image shown for an equal share of total time.
    Audio can be WAV, MP3, OGG, FLAC, etc.  Omit for a silent video.
    Output is H.264/AAC MP4, compatible with OBS, VLC, and most editors.
    """
    if not images:
        print("No images provided.")
        return 2
    for img in images:
        if not os.path.isfile(img):
            print(f"Image not found: {img}")
            return 2
    if audio and not os.path.isfile(audio):
        print(f"Audio file not found: {audio}")
        return 2
    if shutil.which("ffmpeg") is None:
        print("ffmpeg is not installed. Install it with: sudo dnf install -y ffmpeg")
        return 2

    # Determine total duration
    if audio:
        total_dur = _audio_duration_seconds(audio)
        if total_dur <= 0:
            print("Could not detect audio duration; defaulting to 30 seconds.")
            total_dur = 30.0
    else:
        total_dur = max(10.0, len(images) * 5.0)

    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)

    # Scale/pad filter to ensure consistent resolution and even dimensions
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
    )

    if len(images) == 1:
        # Single image — loop it for the full audio duration
        cmd = ["ffmpeg", "-y", "-loop", "1", "-framerate", str(fps), "-i", images[0]]
        if audio:
            cmd += ["-i", audio]
        cmd += [
            "-vf", vf,
            "-c:v", "libx264", "-tune", "stillimage", "-preset", "fast",
            "-pix_fmt", "yuv420p",
            "-t", str(total_dur),
        ]
        if audio:
            cmd += ["-c:a", "aac", "-b:a", "192k", "-shortest"]
        else:
            cmd += ["-an"]
        cmd.append(output)
        print(f"Building video: 1 image × {total_dur:.1f}s → {output}")
    else:
        # Multiple images — build concat list file (one temp file per ffmpeg concat demuxer)
        dur_each = total_dur / len(images)
        import tempfile
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        for img in images:
            tmp.write(f"file '{os.path.abspath(img)}'\n")
            tmp.write(f"duration {dur_each:.3f}\n")
        # Repeat last image once more (required by concat demuxer to avoid last-frame drop)
        tmp.write(f"file '{os.path.abspath(images[-1])}'\n")
        tmp.flush()
        tmp.close()

        print(f"Building slideshow: {len(images)} images × {dur_each:.1f}s each → {output}")

        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", tmp.name,
        ]
        if audio:
            cmd += ["-i", audio]
        cmd += [
            "-vf", vf,
            "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
            "-r", str(fps),
            "-t", str(total_dur),
        ]
        if audio:
            cmd += ["-c:a", "aac", "-b:a", "192k", "-shortest"]
        else:
            cmd += ["-an"]
        cmd.append(output)

    proc = subprocess.run(cmd)
    if proc.returncode == 0:
        print(f"Video saved: {output}")
    else:
        print(f"ffmpeg exited with code {proc.returncode}")
    # clean up temp concat file if it exists
    try:
        os.unlink(tmp.name)  # type: ignore[name-defined]
    except Exception:
        pass
    return proc.returncode


def _has_flatpak_app(app_id: str) -> bool:
    if shutil.which("flatpak") is None:
        return False
    try:
        proc = subprocess.run(["flatpak", "info", app_id], capture_output=True)
        return proc.returncode == 0
    except Exception:
        return False


def _obs_command() -> Optional[list]:
    obs_bin = shutil.which("obs")
    if obs_bin:
        return [obs_bin]
    if _has_flatpak_app("com.obsproject.Studio"):
        return ["flatpak", "run", "com.obsproject.Studio"]
    return None


def _libreoffice_command() -> Optional[list]:
    lo_bin = shutil.which("libreoffice") or shutil.which("soffice")
    if lo_bin:
        return [lo_bin]
    common_path = "/usr/lib64/libreoffice/program/soffice"
    if os.path.isfile(common_path) and os.access(common_path, os.X_OK):
        return [common_path]
    if _has_flatpak_app("org.libreoffice.LibreOffice"):
        return ["flatpak", "run", "org.libreoffice.LibreOffice"]
    return None


def _default_obs_recordings_dir() -> str:
    return os.path.expanduser("~/Videos")


def _v4l2_name_for_device(dev_path: str) -> str:
    dev = os.path.basename(dev_path)
    name_path = f"/sys/class/video4linux/{dev}/name"
    try:
        with open(name_path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except Exception:
        return "unknown"


def _find_obsbot_devices() -> list:
    """Find /dev/video* nodes belonging to an OBSBOT camera.

    Detection strategy (in order):
    1. Card name contains 'obsbot' or 'tiny' (most reliable when driver reports it)
    2. udevadm info shows an OBSBOT-related vendor/model string
    3. USB vendor ID matches known OBSBOT vendors (2d6c, 3474)
    """
    OBSBOT_VENDOR_IDS = {"2d6c", "3474"}
    devices = []
    for idx in range(0, 20):
        dev_path = f"/dev/video{idx}"
        if not os.path.exists(dev_path):
            continue
        name = _v4l2_name_for_device(dev_path)
        lowered = name.lower()
        if "obsbot" in lowered or "tiny" in lowered:
            devices.append({"device": dev_path, "name": name})
            continue
        # Fallback: check udevadm for vendor/model strings
        if shutil.which("udevadm"):
            try:
                proc = subprocess.run(
                    ["udevadm", "info", "--query=all", "--name", dev_path],
                    capture_output=True, text=True, timeout=3
                )
                udev_out = (proc.stdout or "").lower()
                if "obsbot" in udev_out or any(vid in udev_out for vid in OBSBOT_VENDOR_IDS):
                    devices.append({"device": dev_path, "name": name or dev_path})
            except Exception:
                pass
    return devices


def _list_video_devices() -> list:
    devices = []
    for idx in range(0, 20):
        dev_path = f"/dev/video{idx}"
        if not os.path.exists(dev_path):
            continue
        devices.append({"device": dev_path, "name": _v4l2_name_for_device(dev_path)})
    return devices


def _obsbot_usb_present() -> bool:
    """Return True if an OBSBOT camera is visible via lsusb (by name or known vendor IDs)."""
    if shutil.which("lsusb") is None:
        return False
    # Known OBSBOT/YOS USB vendor IDs
    OBSBOT_VENDOR_IDS = {"2d6c", "3474"}
    try:
        proc = subprocess.run(["lsusb"], capture_output=True, text=True)
        if proc.returncode != 0:
            return False
        out = (proc.stdout or "").lower()
        if "obsbot" in out:
            return True
        # Check by vendor ID (lsusb line format: "Bus NNN Device NNN: ID vvvv:pppp Name")
        for line in proc.stdout.splitlines():
            parts = line.split()
            for part in parts:
                if ":" in part:
                    vid = part.split(":")[0].lower()
                    if vid in OBSBOT_VENDOR_IDS:
                        return True
        return False
    except Exception:
        return False


def obsbot_status() -> int:
    devices = _find_obsbot_devices()
    all_devices = _list_video_devices()
    usb_present = _obsbot_usb_present()
    print("OBSBOT camera status")
    print("-" * 40)
    print(f"detected   : {'yes' if devices else 'no'}")
    print(f"usb seen   : {'yes' if usb_present else 'no'}")
    print(f"v4l2-ctl   : {'yes' if shutil.which('v4l2-ctl') else 'no'}")
    if not devices:
        print("No OBSBOT-labeled V4L2 device found.")
        if all_devices:
            print("Available video devices:")
            for idx, item in enumerate(all_devices, 1):
                print(f"{idx:2d}) {item['device']}  {item['name']}")
            print("Tip: Use --device with obsbot-controls/obsbot-set if your OBSBOT shows a generic name.")
        else:
            print("No /dev/video* devices found. Make sure camera is connected and powered.")
        return 1
    for idx, item in enumerate(devices, 1):
        print(f"{idx:2d}) {item['device']}  {item['name']}")
    return 0


def obsbot_list_controls(device: Optional[str]) -> int:
    v4l2 = shutil.which("v4l2-ctl")
    if not v4l2:
        print("v4l2-ctl is not installed. Install v4l-utils to inspect camera controls.")
        return 2

    devices = _find_obsbot_devices()
    target = device
    if not target:
        if not devices:
            print("No OBSBOT camera detected.")
            return 1
        target = devices[0]["device"]

    cmd = [v4l2, "-d", target, "--list-ctrls"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stderr.strip() or "Failed to read camera controls")
        return proc.returncode

    print(f"OBSBOT controls for {target}")
    print("-" * 40)
    print(proc.stdout.strip())
    return 0


def obsbot_set_control(device: Optional[str], control: str, value: str) -> int:
    v4l2 = shutil.which("v4l2-ctl")
    if not v4l2:
        print("v4l2-ctl is not installed. Install v4l-utils to set camera controls.")
        return 2

    devices = _find_obsbot_devices()
    target = device
    if not target:
        if not devices:
            print("No OBSBOT camera detected.")
            return 1
        target = devices[0]["device"]

    cmd = [v4l2, "-d", target, f"--set-ctrl={control}={value}"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stderr.strip() or "Failed to set camera control")
        return proc.returncode
    print(f"Set {control}={value} on {target}")
    return 0


def obs_status() -> int:
    obs_cmd = _obs_command()
    running = False
    try:
        proc = subprocess.run(["pgrep", "-x", "obs"], capture_output=True, text=True)
        running = proc.returncode == 0
    except Exception:
        running = False

    print("OBS status")
    print("-" * 40)
    print(f"installed  : {'yes' if obs_cmd else 'no'}")
    print(f"running    : {'yes' if running else 'no'}")
    print(f"command    : {' '.join(obs_cmd) if obs_cmd else 'N/A'}")
    print(f"recordings : {_default_obs_recordings_dir()}")
    print(f"profiles   : {os.path.expanduser('~/.config/obs-studio/basic/profiles')}")
    print(f"scenes     : {os.path.expanduser('~/.config/obs-studio/basic/scenes')}")
    return 0


def obs_launch(minimized: bool) -> int:
    obs_cmd = _obs_command()
    if not obs_cmd:
        print("OBS is not installed or not in PATH.")
        return 2

    cmd = list(obs_cmd)
    if minimized:
        cmd.append("--minimize-to-tray")

    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("OBS launch command sent.")
        return 0
    except Exception as exc:
        print(f"Failed to launch OBS: {exc}")
        return 1


def obs_recordings(limit: int) -> int:
    rec_dir = _default_obs_recordings_dir()
    if not os.path.isdir(rec_dir):
        print(f"Recordings directory not found: {rec_dir}")
        return 2

    entries = []
    for name in os.listdir(rec_dir):
        if name.lower().endswith((".mkv", ".mp4", ".mov", ".flv", ".avi", ".webm")):
            full = os.path.join(rec_dir, name)
            try:
                mtime = os.path.getmtime(full)
            except OSError:
                continue
            entries.append((mtime, full))

    entries.sort(reverse=True)
    print("OBS recordings")
    print("-" * 40)
    if not entries:
        print("No recordings found.")
        return 0

    for idx, (_, full) in enumerate(entries[:max(1, limit)], 1):
        print(f"{idx:2d}) {full}")
    return 0


def libreoffice_convert(src: str, fmt: str, outdir: str) -> int:
    lo_cmd = _libreoffice_command()
    if not lo_cmd:
        print("LibreOffice is not installed or not in PATH.")
        return 2
    if not os.path.isfile(src):
        print(f"Input file not found: {src}")
        return 2

    os.makedirs(outdir, exist_ok=True)
    src_stem = os.path.splitext(os.path.basename(src))[0]
    before = set(os.listdir(outdir))

    cmd = list(lo_cmd) + ["--headless", "--convert-to", fmt, "--outdir", outdir, src]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stderr.strip() or "LibreOffice conversion failed")
        return proc.returncode

    after = set(os.listdir(outdir))
    created = sorted(after - before)
    expected_suffix = f".{fmt.lower().split(':', 1)[0]}"
    matched = [
        name for name in created
        if name.lower().endswith(expected_suffix) and src_stem.lower() in name.lower()
    ]

    if not matched:
        print("LibreOffice reported success but no output file was detected.")
        if proc.stdout.strip():
            print(proc.stdout.strip())
        if proc.stderr.strip():
            print(proc.stderr.strip())
        return 1

    print(f"Conversion completed: {os.path.join(outdir, matched[0])}")
    return 0


def run_menu() -> int:
    while True:
        print("\nHAL Studio")
        print("-" * 40)
        print("1) Stock quote")
        print("2) Stock watch alert")
        print("3) Describe image")
        print("4) Resize image")
        print("5) Generate simple poster image")
        print("6) Video info")
        print("7) Trim video")
        print("8) OBS status")
        print("9) OBS launch")
        print("10) OBS recordings list")
        print("11) LibreOffice convert")
        print("12) Text to image")
        print("13) Text to music")
        print("14) Capabilities")
        print("15) OBSBOT status")
        print("16) OBSBOT list controls")
        print("17) OBSBOT set control")
        print("18) AI image generate")
        print("19) Text to speech")
        print("20) Mix music + vocals")
        print("21) Auto prompt pipeline")
        print("23) Build video from my images + music")
        print("22) Exit")

        choice = input("Choose an option: ").strip()

        if choice == "1":
            symbol = input("Ticker symbol (example: AAPL): ").strip()
            stock_quote(symbol)
        elif choice == "2":
            symbol = input("Ticker symbol: ").strip()
            above_s = input("Alert when price >= (blank to skip): ").strip()
            below_s = input("Alert when price <= (blank to skip): ").strip()
            interval_s = input("Interval seconds [60]: ").strip() or "60"
            checks_s = input("Checks [30]: ").strip() or "30"
            above = float(above_s) if above_s else None
            below = float(below_s) if below_s else None
            stock_watch(symbol, above, below, int(interval_s), int(checks_s))
        elif choice == "3":
            path = input("Image path: ").strip()
            ocr = input("Run OCR if available? [y/N]: ").strip().lower() == "y"
            image_describe(path, ocr)
        elif choice == "4":
            src = input("Input image path: ").strip()
            dst = input("Output image path: ").strip()
            width = int(input("Width: ").strip())
            height = int(input("Height: ").strip())
            image_resize(src, dst, width, height)
        elif choice == "5":
            text = input("Poster text: ").strip()
            out = input("Output image path [poster.png]: ").strip() or "poster.png"
            image_poster(text, out, 1280, 720)
        elif choice == "6":
            path = input("Video path: ").strip()
            video_info(path)
        elif choice == "7":
            src = input("Input video path: ").strip()
            dst = input("Output video path: ").strip()
            start = input("Start (HH:MM:SS or seconds): ").strip()
            duration = input("Duration (HH:MM:SS or seconds): ").strip()
            rc = video_trim(src, dst, start, duration)
            print("Trim complete" if rc == 0 else f"Trim failed (exit {rc})")
        elif choice == "8":
            obs_status()
        elif choice == "9":
            minimized = input("Launch minimized to tray? [y/N]: ").strip().lower() == "y"
            obs_launch(minimized)
        elif choice == "10":
            limit_s = input("How many files to show [10]: ").strip() or "10"
            obs_recordings(int(limit_s))
        elif choice == "11":
            src = input("Input office file path: ").strip()
            fmt = input("Output format [pdf]: ").strip() or "pdf"
            outdir = input("Output directory [.]: ").strip() or "."
            libreoffice_convert(src, fmt, outdir)
        elif choice == "12":
            prompt = input("Image prompt: ").strip()
            out = input("Output image path [prompt-art.png]: ").strip() or "prompt-art.png"
            text_to_image(prompt, out, 1280, 720)
        elif choice == "13":
            prompt = input("Music prompt: ").strip()
            out = input("Output audio path [prompt-music.wav]: ").strip() or "prompt-music.wav"
            seconds_s = input("Length in seconds [20]: ").strip() or "20"
            bpm_s = input("BPM [120]: ").strip() or "120"
            text_to_music(prompt, out, int(seconds_s), int(bpm_s))
        elif choice == "14":
            print_capabilities()
        elif choice == "15":
            obsbot_status()
        elif choice == "16":
            dev = input("Device path (blank = auto-detect): ").strip() or None
            obsbot_list_controls(dev)
        elif choice == "17":
            dev = input("Device path (blank = auto-detect): ").strip() or None
            ctrl = input("Control name (example: zoom_absolute): ").strip()
            val = input("Control value: ").strip()
            obsbot_set_control(dev, ctrl, val)
        elif choice == "18":
            prompt = input("Prompt: ").strip()
            out = input("Output image path [ai-image.png]: ").strip() or "ai-image.png"
            backend = input("Backend [auto/diffusers/ollama-assisted/poster]: ").strip() or "auto"
            ai_image_generate(prompt, out, 1280, 720, backend, None, 30, 7.5)
        elif choice == "19":
            text = input("Text for vocals: ").strip()
            out = input("Output vocals WAV [vocals.wav]: ").strip() or "vocals.wav"
            voice = input("Voice (blank = default): ").strip() or None
            text_to_speech(text, out, voice, 165)
        elif choice == "20":
            music = input("Music WAV path: ").strip()
            vocals = input("Vocals WAV path: ").strip()
            out = input("Output mixed WAV [final-mix.wav]: ").strip() or "final-mix.wav"
            mix_music_and_vocals(music, vocals, out)
        elif choice == "21":
            prompt = input("Master prompt: ").strip()
            outdir = input("Output directory [./hal-scenes]: ").strip() or "./hal-scenes"
            sec = int(input("Music seconds [20]: ").strip() or "20")
            bpm = int(input("BPM [120]: ").strip() or "120")
            backend = input("Image backend [auto/diffusers/ollama-assisted/poster]: ").strip() or "auto"
            voice = input("TTS voice (blank = default): ").strip() or None
            prompt_pipeline(prompt, outdir, sec, bpm, backend, None, voice)
        elif choice == "23":
            print("Enter image paths one per line (blank line when done):")
            imgs = []
            while True:
                p = input("  Image path: ").strip()
                if not p:
                    break
                imgs.append(p)
            if not imgs:
                print("No images entered.")
            else:
                audio = input("Audio file path (WAV/MP3/etc, blank = silent): ").strip() or None
                out = input("Output video path [my-video.mp4]: ").strip() or "my-video.mp4"
                fps_s = input("FPS [25]: ").strip() or "25"
                w_s = input("Width [1280]: ").strip() or "1280"
                h_s = input("Height [720]: ").strip() or "720"
                video_build(imgs, audio, out, int(fps_s), int(w_s), int(h_s))
        elif choice == "22":
            return 0
        else:
            print("Invalid option")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="HAL Studio tools")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("menu", help="Launch interactive studio menu")
    sub.add_parser("capabilities", help="Show studio capabilities")

    quote = sub.add_parser("stock-quote", help="Get stock quote")
    quote.add_argument("symbol")

    watch = sub.add_parser("stock-watch", help="Watch stock price thresholds")
    watch.add_argument("symbol")
    watch.add_argument("--above", type=float)
    watch.add_argument("--below", type=float)
    watch.add_argument("--interval", type=int, default=60)
    watch.add_argument("--checks", type=int, default=30)

    describe = sub.add_parser("image-describe", help="Describe an image")
    describe.add_argument("path")
    describe.add_argument("--ocr", action="store_true")

    resize = sub.add_parser("image-resize", help="Resize image")
    resize.add_argument("src")
    resize.add_argument("dst")
    resize.add_argument("--width", type=int, required=True)
    resize.add_argument("--height", type=int, required=True)

    poster = sub.add_parser("image-poster", help="Generate simple poster image")
    poster.add_argument("--text", required=True)
    poster.add_argument("--output", default="poster.png")
    poster.add_argument("--width", type=int, default=1280)
    poster.add_argument("--height", type=int, default=720)

    t2i = sub.add_parser("text-image", help="Generate an image from text prompt")
    t2i.add_argument("prompt")
    t2i.add_argument("--output", default="prompt-art.png")
    t2i.add_argument("--width", type=int, default=1280)
    t2i.add_argument("--height", type=int, default=720)

    t2m = sub.add_parser("text-music", help="Generate a WAV music track from text prompt")
    t2m.add_argument("prompt")
    t2m.add_argument("--output", default="prompt-music.wav")
    t2m.add_argument("--seconds", type=int, default=20)
    t2m.add_argument("--bpm", type=int, default=120)

    aii = sub.add_parser("ai-image", help="Generate image with diffusers or ollama-assisted backend")
    aii.add_argument("prompt")
    aii.add_argument("--output", default="ai-image.png")
    aii.add_argument("--backend", choices=["auto", "diffusers", "ollama-assisted", "poster"], default="auto")
    aii.add_argument("--model", default=None)
    aii.add_argument("--width", type=int, default=1280)
    aii.add_argument("--height", type=int, default=720)
    aii.add_argument("--steps", type=int, default=30)
    aii.add_argument("--guidance", type=float, default=7.5)

    tts = sub.add_parser("text-speech", help="Generate vocals WAV from text")
    tts.add_argument("text")
    tts.add_argument("--output", default="vocals.wav")
    tts.add_argument("--voice", default=None)
    tts.add_argument("--speed", type=int, default=165)

    mix = sub.add_parser("music-vocals", help="Layer vocals over music")
    mix.add_argument("music")
    mix.add_argument("vocals")
    mix.add_argument("--output", default="final-mix.wav")

    pipe = sub.add_parser("pipeline", help="One prompt -> image + music + vocals + OBS scene assets")
    pipe.add_argument("prompt")
    pipe.add_argument("--outdir", default="./hal-scenes")
    pipe.add_argument("--seconds", type=int, default=20)
    pipe.add_argument("--bpm", type=int, default=120)
    pipe.add_argument("--image-backend", choices=["auto", "diffusers", "ollama-assisted", "poster"], default="auto")
    pipe.add_argument("--image-model", default=None)
    pipe.add_argument("--voice", default=None)

    vinfo = sub.add_parser("video-info", help="Show video metadata")
    vinfo.add_argument("path")

    vtrim = sub.add_parser("video-trim", help="Trim a video segment")
    vtrim.add_argument("src")
    vtrim.add_argument("dst")
    vtrim.add_argument("--start", required=True)
    vtrim.add_argument("--duration", required=True)

    vbuild = sub.add_parser(
        "video-build",
        help="Build a video from your own images + music (slideshow or looped still)",
    )
    vbuild.add_argument(
        "images",
        nargs="+",
        metavar="IMAGE",
        help="One or more image files (PNG, JPG, …). Multiple images = slideshow.",
    )
    vbuild.add_argument("--audio", default=None, metavar="AUDIO",
                        help="Audio file (WAV, MP3, OGG, …). Omit for silent video.")
    vbuild.add_argument("--output", default="my-video.mp4")
    vbuild.add_argument("--fps", type=int, default=25)
    vbuild.add_argument("--width", type=int, default=1280)
    vbuild.add_argument("--height", type=int, default=720)

    obs_s = sub.add_parser("obs-status", help="Show OBS installation and runtime status")
    obs_s.set_defaults(_obs_status=True)

    obs_l = sub.add_parser("obs-launch", help="Launch OBS")
    obs_l.add_argument("--minimized", action="store_true")

    obs_r = sub.add_parser("obs-recordings", help="List OBS recording files")
    obs_r.add_argument("--limit", type=int, default=10)

    lo_c = sub.add_parser("lo-convert", help="Convert documents with LibreOffice headless")
    lo_c.add_argument("src")
    lo_c.add_argument("--format", default="pdf")
    lo_c.add_argument("--outdir", default=".")

    obss = sub.add_parser("obsbot-status", help="Show OBSBOT camera status")
    obss.set_defaults(_obsbot_status=True)

    obsl = sub.add_parser("obsbot-controls", help="List OBSBOT V4L2 controls")
    obsl.add_argument("--device", default=None)

    obset = sub.add_parser("obsbot-set", help="Set OBSBOT V4L2 control")
    obset.add_argument("control")
    obset.add_argument("value")
    obset.add_argument("--device", default=None)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command in (None, "menu"):
        return run_menu()
    if args.command == "capabilities":
        return print_capabilities()
    if args.command == "stock-quote":
        return stock_quote(args.symbol)
    if args.command == "stock-watch":
        return stock_watch(args.symbol, args.above, args.below, args.interval, args.checks)
    if args.command == "image-describe":
        return image_describe(args.path, args.ocr)
    if args.command == "image-resize":
        return image_resize(args.src, args.dst, args.width, args.height)
    if args.command == "image-poster":
        return image_poster(args.text, args.output, args.width, args.height)
    if args.command == "text-image":
        return text_to_image(args.prompt, args.output, args.width, args.height)
    if args.command == "text-music":
        return text_to_music(args.prompt, args.output, args.seconds, args.bpm)
    if args.command == "ai-image":
        return ai_image_generate(args.prompt, args.output, args.width, args.height, args.backend, args.model, args.steps, args.guidance)
    if args.command == "text-speech":
        return text_to_speech(args.text, args.output, args.voice, args.speed)
    if args.command == "music-vocals":
        return mix_music_and_vocals(args.music, args.vocals, args.output)
    if args.command == "pipeline":
        return prompt_pipeline(args.prompt, args.outdir, args.seconds, args.bpm, args.image_backend, args.image_model, args.voice)
    if args.command == "video-info":
        return video_info(args.path)
    if args.command == "video-trim":
        return video_trim(args.src, args.dst, args.start, args.duration)
    if args.command == "video-build":
        return video_build(args.images, args.audio, args.output, args.fps, args.width, args.height)
    if args.command == "obs-status":
        return obs_status()
    if args.command == "obs-launch":
        return obs_launch(args.minimized)
    if args.command == "obs-recordings":
        return obs_recordings(args.limit)
    if args.command == "lo-convert":
        return libreoffice_convert(args.src, args.format, args.outdir)
    if args.command == "obsbot-status":
        return obsbot_status()
    if args.command == "obsbot-controls":
        return obsbot_list_controls(args.device)
    if args.command == "obsbot-set":
        return obsbot_set_control(args.device, args.control, args.value)

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
