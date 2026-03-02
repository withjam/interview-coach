#!/usr/bin/env python3
"""
Listen to a selected audio input, transcribe speech to text with Speechmatics
streaming, and stream a chat session to Ollama Cloud.

Environment:
  OLLAMA_API_KEY            - Ollama Cloud API key (https://ollama.com/settings/keys)
  SPEECHMATICS_API_KEY      - Speechmatics API key (https://speechmatics.com/)
  OLLAMA_CHAT_MODEL         - (optional) Chat model name (default: llama3.2)
  SPEECHMATICS_LANGUAGE     - (optional) Language code (default: en)
  CANDIDATE_SPEAKER_LABEL   - (optional) With speaker diarization, only this speaker's
                              speech is sent to Ollama (default: S1). Use S1, S2, etc.
                              If unset or diarization off, all speech is treated as candidate.
  COACH_SPEAKER_ENROLLMENT  - (optional) Path to speaker enrollment JSON from
                              enroll_speaker.py. If set, only the enrolled "candidate"
                              voice is sent to Ollama.
  SPEECHMATICS_URL          - (optional) Override Speechmatics real-time WebSocket URL
                              (default: wss://eu2.rt.speechmatics.com/v2). Use if you
                              get handshake timeouts or need a different region.
"""

import asyncio
import json
import os
import signal
import sys
import threading
import time

import numpy as np
import sounddevice as sd
import speechmatics
from speechmatics.adapters import get_speaker, group_tokens, join_tokens
from ollama import Client as OllamaClient

# Speechmatics real-time: pcm_f32le, any sample rate (we use device rate)
DEFAULT_SPEECHMATICS_URL = "wss://eu2.rt.speechmatics.com/v2"
# Chunk size in bytes for streaming (matches SDK microphone example)
STT_CHUNK_BYTES = 1024

# Ollama
OLLAMA_CLOUD_HOST = "https://ollama.com"
DEFAULT_CHAT_MODEL = "llama3.2"

DEFAULT_ENROLLMENT_PATH = os.path.join(
    os.path.dirname(__file__), "speaker_enrollment.json"
)


def load_speaker_enrollment() -> dict | None:
    """Load candidate speaker enrollment from COACH_SPEAKER_ENROLLMENT or default path."""
    path = os.environ.get("COACH_SPEAKER_ENROLLMENT", DEFAULT_ENROLLMENT_PATH)
    path = os.path.expanduser(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("speaker_identifiers"):
            return data
        return None
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def load_prompt() -> str | None:
    """Load optional prompt instructions from prompt.md next to this script."""
    prompt_path = os.path.join(os.path.dirname(__file__), "prompt.md")
    try:
        with open(prompt_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        return content or None
    except FileNotFoundError:
        return None
    except OSError as e:
        print(f"Warning: could not read prompt.md: {e}", file=sys.stderr)
        return None


def get_input_devices():
    devices = sd.query_devices()
    inputs = []
    for i, dev in enumerate(devices):
        if dev["max_input_channels"] > 0:
            inputs.append((i, dev))
    return inputs


def prompt_device(inputs):
    print("\nAvailable audio input sources:\n")
    for idx, (device_id, dev) in enumerate(inputs):
        sr = int(dev["default_samplerate"])
        ch = dev["max_input_channels"]
        print(f"  [{idx}] {dev['name']}  ({ch} ch, {sr} Hz)")
    print()
    while True:
        try:
            choice = int(input("Select a source by number: "))
            if 0 <= choice < len(inputs):
                return inputs[choice]
            print(f"Please enter a number between 0 and {len(inputs) - 1}.")
        except ValueError:
            print("Invalid input. Enter a number.")
        except (EOFError, KeyboardInterrupt):
            sys.exit(0)


def stream_chat(ollama_client: OllamaClient, messages: list[dict], model: str) -> str:
    """Send messages to Ollama Cloud and stream the reply; return full content."""
    full = []
    for part in ollama_client.chat(model=model, messages=messages, stream=True):
        content = (part.get("message") or {}).get("content") or ""
        if content:
            print(content, end="", flush=True)
            full.append(content)
    print()
    return "".join(full)


class SpeechmaticsAudioAdapter:
    """Thread-safe audio buffer for Speechmatics run_synchronously; supports async read."""

    def __init__(self):
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._closed = False

    def write_audio(self, data: bytes) -> None:
        with self._lock:
            self._buffer.extend(data)

    async def read(self, chunk_size: int) -> bytes:
        while True:
            with self._lock:
                if self._closed and len(self._buffer) == 0:
                    return b""
                if len(self._buffer) >= chunk_size:
                    out = bytes(self._buffer[:chunk_size])
                    del self._buffer[:chunk_size]
                    return out
            await asyncio.sleep(0.001)

    def close(self) -> None:
        self._closed = True


def main():
    ollama_key = os.environ.get("OLLAMA_API_KEY")
    speechmatics_key = os.environ.get("SPEECHMATICS_API_KEY")
    if not ollama_key:
        print("Error: OLLAMA_API_KEY is not set.", file=sys.stderr)
        sys.exit(1)
    if not speechmatics_key:
        print("Error: SPEECHMATICS_API_KEY is not set.", file=sys.stderr)
        sys.exit(1)

    inputs = get_input_devices()
    if not inputs:
        print("No audio input devices found.")
        sys.exit(1)

    device_id, dev = prompt_device(inputs)
    samplerate = int(dev["default_samplerate"])
    channels = dev["max_input_channels"]
    chat_model = os.environ.get("OLLAMA_CHAT_MODEL", DEFAULT_CHAT_MODEL)
    language = os.environ.get("SPEECHMATICS_LANGUAGE", "en")

    enrollment = load_speaker_enrollment()
    if enrollment:
        candidate_speaker_label = "candidate"
    else:
        raw = os.environ.get("CANDIDATE_SPEAKER_LABEL", "S1").strip()
        candidate_speaker_label = raw or None

    if enrollment:
        candidate_label = "candidate (enrolled)"
    else:
        candidate_label = os.environ.get("CANDIDATE_SPEAKER_LABEL", "S1").strip() or "all"
    print(f"\nListening on: {dev['name']}")
    print(f"  Sample rate : {samplerate} Hz")
    print(f"  Channels    : {channels}")
    print(f"  STT         : Speechmatics streaming (speaker diarization on)")
    if candidate_label == "candidate (enrolled)":
        print(f"  Candidate   : {candidate_label}")
    elif candidate_label != "all":
        print(f"  Candidate   : speaker {candidate_label} only")
    else:
        print("  Candidate   : all speech")
    print(f"  Chat model  : {chat_model}")
    print("\nPress Ctrl+C to stop.\n")

    ollama_client = OllamaClient(
        host=OLLAMA_CLOUD_HOST,
        headers={"Authorization": f"Bearer {ollama_key}"},
    )

    messages: list[dict] = []
    prompt_text = load_prompt()
    if prompt_text:
        messages.append({"role": "system", "content": prompt_text})
    transcript_queue: list[str] = []
    transcript_lock = threading.Lock()
    stop = threading.Event()

    audio_adapter = SpeechmaticsAudioAdapter()
    speechmatics_base_url = os.environ.get("SPEECHMATICS_URL", DEFAULT_SPEECHMATICS_URL).rstrip("/")
    connection_url = f"{speechmatics_base_url}/{language}"

    def on_final_transcript(msg):
        try:
            results = msg.get("results")
            if results:
                # Speaker diarization: only queue segments from the candidate speaker.
                groups = group_tokens(results)
                for group in groups:
                    if not group:
                        continue
                    speaker = get_speaker(group[0])
                    if candidate_speaker_label is None or speaker is None or speaker == candidate_speaker_label:
                        text = join_tokens(group).strip()
                        if text:
                            with transcript_lock:
                                transcript_queue.append(text)
            else:
                # Fallback when API returns a single transcript string (e.g. metadata.transcript).
                text = (msg.get("metadata") or {}).get("transcript") or ""
                if isinstance(text, str) and text.strip():
                    with transcript_lock:
                        transcript_queue.append(text.strip())
        except Exception:
            pass

    conn = speechmatics.models.ConnectionSettings(
        url=connection_url,
        auth_token=speechmatics_key,
    )
    ws = speechmatics.client.WebsocketClient(conn)
    ws.add_event_handler(
        event_name=speechmatics.models.ServerMessageType.AddTranscript,
        event_handler=on_final_transcript,
    )
    speaker_diarization_config: dict | object = {
        "max_speakers": 2,
        "prefer_current_speaker": True,
    }
    if enrollment:
        speaker_diarization_config = dict(speaker_diarization_config)
        speaker_diarization_config["speakers"] = [enrollment]

    conf = speechmatics.models.TranscriptionConfig(
        language=language,
        enable_partials=False,
        operating_point="enhanced",
        max_delay=1,
        diarization="speaker",
        speaker_diarization_config=speaker_diarization_config,
        conversation_config=speechmatics.models.ConversationConfig(
            end_of_utterance_silence_trigger=0.6,
        ),
    )
    settings = speechmatics.models.AudioSettings()
    settings.encoding = "pcm_f32le"
    settings.sample_rate = samplerate
    settings.chunk_size = STT_CHUNK_BYTES

    def audio_callback(indata, frames, time_info, status):
        if status:
            print(f"  ⚠ {status}", file=sys.stderr)
        mono = indata if indata.ndim == 1 else indata.mean(axis=1)
        audio_adapter.write_audio(mono.astype(np.float32).tobytes())

    def ollama_worker():
        while not stop.is_set():
            with transcript_lock:
                if not transcript_queue:
                    texts = []
                else:
                    texts = transcript_queue[:]
                    transcript_queue.clear()
            for text in texts:
                if not text:
                    continue
                print(f"\n  You: {text}")
                messages.append({"role": "user", "content": text})
                try:
                    reply = stream_chat(ollama_client, messages, chat_model)
                    if reply:
                        messages.append({"role": "assistant", "content": reply})
                except Exception as e:
                    print(f"  [Ollama error] {e}", file=sys.stderr)
            stop.wait(timeout=0.3)

    def run_speechmatics():
        try:
            ws.run_synchronously(audio_adapter, conf, settings)
        except TimeoutError as e:
            if not stop.is_set():
                msg = str(e).lower()
                if "handshake" in msg or "open" in msg:
                    print(
                        "\n  [Speechmatics] Connection timed out during handshake.",
                        "\n  Try: check firewall/VPN; use SPEECHMATICS_URL if you have another region (e.g. wss://neu.rt.speechmatics.com/v2).",
                        file=sys.stderr,
                    )
                else:
                    print(f"\n  [Speechmatics error] {e}", file=sys.stderr)
        except Exception as e:
            if not stop.is_set():
                print(f"\n  [Speechmatics error] {e}", file=sys.stderr)
        finally:
            audio_adapter.close()

    signal.signal(signal.SIGINT, lambda *_: (stop.set(), sys.exit(0)))

    worker = threading.Thread(target=ollama_worker, daemon=True)
    worker.start()

    stt_thread = threading.Thread(target=run_speechmatics, daemon=True)
    stt_thread.start()

    with sd.InputStream(
        device=device_id,
        samplerate=samplerate,
        channels=channels,
        callback=audio_callback,
        blocksize=int(samplerate * 0.05),
    ):
        print("  (stream open — speaking will be transcribed and sent to Ollama)")
        while not stop.is_set():
            time.sleep(0.2)
        stop.set()


if __name__ == "__main__":
    main()
