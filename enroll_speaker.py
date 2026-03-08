#!/usr/bin/env python3
"""
Enroll your voice as the "candidate" for the interview coach.

Records from your microphone, sends the audio to Speechmatics with speaker
diarization (operating_point="enhanced" to match coach.py), then saves your
speaker identifiers so coach.py can tag only your speech and send it to Ollama.

Recommended: 8–12 seconds of clear speech. Shorter = faster upload; too short
may not give a stable voiceprint. 15s is fine but takes longer to upload.

Usage:
  python enroll_speaker.py              # Record 10 seconds (default)
  python enroll_speaker.py --seconds 12
  COACH_SPEAKER_ENROLLMENT=/path/to/save.json python enroll_speaker.py

Environment:
  SPEECHMATICS_API_KEY       - Speechmatics API key (required)
  SPEECHMATICS_LANGUAGE      - (optional) Language code (default: en)
  COACH_SPEAKER_ENROLLMENT   - (optional) Where to save enrollment JSON
                              (default: ./speaker_enrollment.json)
"""

import argparse
import asyncio
import io
import json
import os
import sys

import numpy as np
import sounddevice as sd
import speechmatics
from speechmatics.exceptions import ForceEndSession
from speechmatics.models import (
    AudioSettings,
    ConnectionSettings,
    ServerMessageType,
    TranscriptionConfig,
)

# Match coach.py; override with SPEECHMATICS_URL env if needed
DEFAULT_SPEECHMATICS_URL = "wss://eu2.rt.speechmatics.com/v2"
# Use larger chunks than live streaming so upload finishes in time (fewer round-trips).
# 16 KB per chunk keeps upload well under the timeout for ~10–15 s of audio.
ENROLLMENT_CHUNK_BYTES = 16 * 1024
DEFAULT_ENROLLMENT_PATH = os.path.join(
    os.path.dirname(__file__), "speaker_enrollment.json"
)
# Max time for upload + processing; avoid hanging forever
ENROLLMENT_TIMEOUT_SECONDS = 120


def get_input_devices():
    devices = sd.query_devices()
    return [
        (i, dev)
        for i, dev in enumerate(devices)
        if dev["max_input_channels"] > 0
    ]


def record_seconds(device_id: int, seconds: float, samplerate: int) -> bytes:
    """Record mono float32 audio from the given device."""
    print(f"Recording for {seconds:.0f} seconds... Speak in your normal voice.\n")
    rec = sd.rec(
        int(seconds * samplerate),
        samplerate=samplerate,
        channels=1,
        dtype=np.float32,
        device=device_id,
    )
    sd.wait()
    return rec.tobytes()


async def run_enrollment(
    audio_bytes: bytes,
    samplerate: int,
    language: str,
    enrollment_path: str,
) -> None:
    base_url = os.environ.get("SPEECHMATICS_URL", DEFAULT_SPEECHMATICS_URL).rstrip("/")
    conn = ConnectionSettings(
        url=f"{base_url}/{language}",
        auth_token=os.environ["SPEECHMATICS_API_KEY"],
    )
    client = speechmatics.client.WebsocketClient(conn)

    result_holder: list = []

    def on_speakers_result(msg):
        speakers = msg.get("speakers") or []
        if not speakers:
            print("No speakers detected. Speak clearly and try again.", file=sys.stderr)
            return
        # Take the first (and usually only) speaker as the candidate
        first = speakers[0]
        enrollment = {
            "label": "candidate",
            "speaker_identifiers": first.get("speaker_identifiers") or [],
        }
        if not enrollment["speaker_identifiers"]:
            print("No speaker identifiers returned. Try a longer recording.", file=sys.stderr)
            return
        result_holder.append(enrollment)
        with open(enrollment_path, "w", encoding="utf-8") as f:
            json.dump(enrollment, f, indent=2)
        print(f"Saved enrollment to {enrollment_path}")
        # End session as soon as we have the result (don't wait for EndOfTranscript)
        raise ForceEndSession()

    client.add_event_handler(ServerMessageType.SpeakersResult, on_speakers_result)

    # get_speakers: true = server sends SpeakersResult when transcription is complete
    # API requires max_speakers >= 2; we only need one (you) and take the first from the result
    speaker_diarization_config = {
        "max_speakers": 2,
        "get_speakers": True,
    }
    # Must match coach.py operating_point so saved identifiers work in streaming.
    conf = TranscriptionConfig(
        language=language,
        operating_point="enhanced",
        diarization="speaker",
        speaker_diarization_config=speaker_diarization_config,
    )
    settings = AudioSettings()
    settings.encoding = "pcm_f32le"
    settings.sample_rate = samplerate
    settings.chunk_size = ENROLLMENT_CHUNK_BYTES

    stream = io.BytesIO(audio_bytes)
    print("Uploading audio and waiting for speaker IDs (max {}s)...".format(ENROLLMENT_TIMEOUT_SECONDS))
    try:
        await asyncio.wait_for(
            client.run(stream, conf, settings),
            timeout=ENROLLMENT_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        print(
            "Timed out before receiving speaker IDs. Try a shorter recording or check your connection.",
            file=sys.stderr,
        )
        sys.exit(1)
    except ForceEndSession:
        # Expected when we get SpeakersResult and raise to exit
        pass
    if not result_holder:
        print("Enrollment did not receive speaker identifiers. Try again.", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Enroll your voice as the candidate for the interview coach."
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=10,
        help="Recording length in seconds, 8–12 recommended (default: 10)",
    )
    parser.add_argument(
        "--device",
        type=int,
        default=None,
        help="Sound device index (default: system default)",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="List input devices and exit",
    )
    args = parser.parse_args()

    if args.list_devices:
        for i, dev in get_input_devices():
            print(f"  [{i}] {dev['name']} ({int(dev['default_samplerate'])} Hz)")
        return

    key = os.environ.get("SPEECHMATICS_API_KEY")
    if not key:
        print("Error: SPEECHMATICS_API_KEY is not set.", file=sys.stderr)
        sys.exit(1)

    language = os.environ.get("SPEECHMATICS_LANGUAGE", "en")
    enrollment_path = os.environ.get("COACH_SPEAKER_ENROLLMENT", DEFAULT_ENROLLMENT_PATH)

    devices = get_input_devices()
    if not devices:
        print("No audio input devices found.", file=sys.stderr)
        sys.exit(1)

    device_id = args.device
    if device_id is None:
        device_id = sd.default.device[0]
    dev = sd.query_devices(device_id)
    samplerate = int(dev["default_samplerate"])

    print("Enrolling your voice as the candidate speaker.")
    audio_bytes = record_seconds(device_id, args.seconds, samplerate)
    if len(audio_bytes) == 0:
        print("No audio recorded.", file=sys.stderr)
        sys.exit(1)

    duration_sec = len(audio_bytes) / (samplerate * 4)  # float32 = 4 bytes per sample
    print("Recording done ({:.1f}s).".format(duration_sec))

    asyncio.run(
        run_enrollment(audio_bytes, samplerate, language, enrollment_path)
    )
    print("Done. Run coach.py; it will use this enrollment to treat only your speech as the candidate.")


if __name__ == "__main__":
    main()
