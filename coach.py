#!/usr/bin/env python3
"""
Listen to a selected audio input, transcribe speech to text with Speechmatics
streaming, and stream a chat session to Ollama Cloud.

Environment:
  OLLAMA_API_KEY            - Ollama Cloud API key (https://ollama.com/settings/keys)
  SPEECHMATICS_API_KEY      - Speechmatics API key (https://speechmatics.com/)
  OLLAMA_CHAT_MODEL         - (optional) Default when you press Enter at model prompt (default: minimax-m2:cloud)
  SPEECHMATICS_LANGUAGE     - (optional) Language code (default: en)
  CANDIDATE_SPEAKER_LABEL   - (optional) With speaker diarization, only this speaker's
                              speech is sent to Ollama (default: S1). Use S1, S2, etc.
                              If unset or diarization off, all speech is treated as candidate.
  COACH_SPEAKER_ENROLLMENT  - (optional) Path to speaker enrollment JSON from
                              enroll_speaker.py. If set, only the enrolled "candidate"
                              voice is sent to Ollama. If you see "identifier from an
                              incompatible model", re-run: uv run enroll_speaker.py
  COACH_USE_SPEAKER_ENROLLMENT - (optional) Set to 0, false or no to disable enrollment
                              and use S1/S2 labels only (avoids "incompatible model" error).
  SPEECHMATICS_URL          - (optional) Override Speechmatics real-time WebSocket URL
                              (default: wss://eu2.rt.speechmatics.com/v2). Use if you
                              get handshake timeouts or need a different region.
  COACH_FULL_MESSAGE_PAUSE_SECONDS - (optional) Seconds of silence after speech before
                              sending the transcript to the model (default: 6). Minimum 1.
  COACH_DEBUG                - (optional) Set to 1, true or yes to print transcribed
                              speech to the console; when unset, only "Thinking..." and
                              the model response are shown.
  COACH_WRAP                 - (optional) Wrap coach response lines to this width (e.g. 72).
                              Set to 0 or leave unset for no wrapping.
  COACH_TTS                  - (optional) Set to 1, true or yes to speak the coach response
                              aloud (uses macOS "say" or configurable command). Lets you
                              listen instead of read for a more natural flow.
  COACH_NO_COLOR             - (optional) Set to 1, true or yes to disable dim "Thinking..."
                              and separator styling in the terminal.
  COACH_FORMAT               - (optional) Set to 0, false or no to disable terminal formatting
                              (markdown → ASCII rules, bullets, boxes). Default on.
"""

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import termios
import tty

import numpy as np
import sounddevice as sd
import speechmatics
from dotenv import load_dotenv
from speechmatics.adapters import get_speaker, group_tokens, join_tokens
from ollama import Client as OllamaClient

# Speechmatics real-time: pcm_f32le, any sample rate (we use device rate)
DEFAULT_SPEECHMATICS_URL = "wss://eu2.rt.speechmatics.com/v2"
# Chunk size in bytes for streaming (matches SDK microphone example)
STT_CHUNK_BYTES = 1024

# Speechmatics connection retries (avoids failing on transient handshake/network errors)
SPEECHMATICS_MAX_RETRIES = 4
SPEECHMATICS_RETRY_DELAY_SECONDS = 2

# Ollama
OLLAMA_CLOUD_HOST = "https://ollama.com"
DEFAULT_CHAT_MODEL = "minimax-m2:cloud"
MODEL_OPTIONS = [
    "minimax-m2:cloud",
    "gemini-3-flash-preview:cloud",
    "qwen3-coder-next:cloud",
    "glm-5:cloud",
]
OLLAMA_MAX_RETRIES = 3
OLLAMA_RETRY_DELAY_SECONDS = 1.5
# Only send to Ollama when we have a full message: no new segment for this long.
DEFAULT_FULL_MESSAGE_PAUSE_SECONDS = 1.5
# Minimum pause we will ever use (avoids env misconfiguration sending too early).
MIN_FULL_MESSAGE_PAUSE_SECONDS = 1.0
SENTENCE_END_CHARS = ".?!"

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


def prompt_model() -> str:
    """Prompt user to select an Ollama chat model; return the selected model name."""
    print("\nAvailable chat models:\n")
    for idx, model in enumerate(MODEL_OPTIONS):
        print(f"  [{idx}] {model}")
    print()
    while True:
        try:
            choice = input("Select a model by number (or press Enter for default): ").strip()
            if choice == "":
                return DEFAULT_CHAT_MODEL
            choice_int = int(choice)
            if 0 <= choice_int < len(MODEL_OPTIONS):
                return MODEL_OPTIONS[choice_int]
            print(f"Please enter a number between 0 and {len(MODEL_OPTIONS) - 1}.")
        except ValueError:
            print("Invalid input. Enter a number.")
        except (EOFError, KeyboardInterrupt):
            sys.exit(0)


def _wrap_stream_print(
    chunk: str, wrap_width: int, continuation: str
) -> None:
    """Print chunk with word-wrap at wrap_width; continuation prefix for new lines.
    Uses module-level _wrap_line_buffer for state across chunks.
    """
    global _wrap_line_buffer
    for c in chunk:
        if c == "\n":
            if _wrap_line_buffer:
                print(_wrap_line_buffer, end="", flush=True)
                _wrap_line_buffer = ""
            print("\n", end="", flush=True)
            continue
        _wrap_line_buffer += c
        if wrap_width > 0 and c == " " and len(_wrap_line_buffer) >= wrap_width:
            last_space = _wrap_line_buffer.rfind(" ")
            if last_space >= 0:
                print(_wrap_line_buffer[: last_space + 1], end="", flush=True)
                print("\n" + continuation, end="", flush=True)
                _wrap_line_buffer = _wrap_line_buffer[last_space + 1 :]
            else:
                print(_wrap_line_buffer, end="", flush=True)
                print("\n" + continuation, end="", flush=True)
                _wrap_line_buffer = ""


_wrap_line_buffer: str = ""

# Terminal formatting: box-drawing and rules for easier scanning
_RULE = "  ─────────────────────────────────────────"
_BULLET = "  • "
_BOLD_ON = "\033[1m"
_BOLD_OFF = "\033[0m"


def format_for_terminal(text: str, use_color: bool, wrap_width: int = 0) -> str:
    """Convert markdown-like output to terminal-friendly layout with rules and bullets."""
    if not text or not text.strip():
        return text
    text = text.strip()
    out: list[str] = []
    # Split into blocks by double newline or by line patterns we treat as block starters
    blocks = re.split(r"\n\s*\n", text)
    for bi, block in enumerate(blocks):
        block = block.strip()
        if not block:
            continue
        lines = block.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()
            # ## Header or ### Header → rule + text; or whole line _Header_ or **Header**
            if re.match(r"^#{1,6}\s+", line) or (
                (stripped.startswith("_") and stripped.endswith("_") and len(stripped) > 2)
                or (stripped.startswith("**") and stripped.endswith("**") and len(stripped) > 4)
            ):
                header = re.sub(r"^#{1,6}\s+", "", line).strip()
                header = re.sub(r"^_(.+)_$", r"\1", header)
                header = re.sub(r"^\*\*(.+)\*\*$", r"\1", header)
                if use_color:
                    out.append(_RULE)
                    out.append(f"  {_BOLD_ON}{header}{_BOLD_OFF}")
                else:
                    out.append(_RULE)
                    out.append(f"  {header}")
                out.append("")
                i += 1
                continue
            # Bullet: - item or * item
            if re.match(r"^[\s]*[-*]\s+", line):
                bullet_line = re.sub(r"^[\s]*[-*]\s+", _BULLET, line, count=1)
                if not bullet_line.startswith("  "):
                    bullet_line = "  " + bullet_line
                out.append(bullet_line.rstrip())
                i += 1
                continue
            # Numbered: 1. item
            if re.match(r"^[\s]*\d+\.\s+", line):
                out.append("  " + stripped)
                i += 1
                continue
            # Code block start ```
            if stripped.startswith("```"):
                code_lines = []
                i += 1
                while i < len(lines) and not lines[i].strip().startswith("```"):
                    code_lines.append("  │ " + lines[i].rstrip())
                    i += 1
                if code_lines:
                    box_w = min(56, max(36, max(len(cl) for cl in code_lines)))
                    out.append("  ┌" + "─" * box_w)
                    out.extend(code_lines)
                    out.append("  └" + "─" * box_w)
                if i < len(lines) and lines[i].strip().startswith("```"):
                    i += 1
                out.append("")
                continue
            # Plain paragraph line: _emphasis_ in line (single underscore); strip ** and $...$
            if stripped:
                # Use _word_ for emphasis; remove ** and $...$ so they're not distracting
                if use_color:
                    formatted = re.sub(r"_([^_]+)_", _BOLD_ON + r"\1" + _BOLD_OFF, line)
                else:
                    formatted = re.sub(r"_([^_]+)_", r"\1", line)
                formatted = re.sub(r"\*\*([^*]+)\*\*", r"\1", formatted)
                formatted = re.sub(r"\$+([^$]*)\$+", r"\1", formatted)
                if not formatted.startswith("  "):
                    formatted = "  " + formatted
                out.append(formatted.rstrip())
            i += 1
        # Section separator between blocks (not after last block)
        if bi < len(blocks) - 1 and out and out[-1].strip():
            out.append("  ─ ─ ─")
            out.append("")
    result = "\n".join(out).strip()
    # Optional word wrap
    if wrap_width > 0:
        wrapped = []
        for line in result.split("\n"):
            if len(line) <= wrap_width:
                wrapped.append(line)
            else:
                rest = line
                while rest:
                    if len(rest) <= wrap_width:
                        wrapped.append(rest)
                        break
                    last_space = rest.rfind(" ", 0, wrap_width + 1)
                    if last_space > 0:
                        wrapped.append(rest[:last_space])
                        rest = "  " + rest[last_space + 1 :].lstrip()
                    else:
                        wrapped.append(rest[:wrap_width])
                        rest = "  " + rest[wrap_width:].lstrip()
        result = "\n".join(wrapped)
    return result


def stream_chat(
    ollama_client: OllamaClient,
    messages: list[dict],
    model: str,
    wrap_width: int = 0,
    continuation_prefix: str = "  ",
    stream_output: bool = True,
) -> str:
    """Send messages to Ollama Cloud and stream the reply; return full content.
    If stream_output=False, collect full reply without printing (caller will format/print).
    """
    global _wrap_line_buffer
    last_error = None
    for attempt in range(1, OLLAMA_MAX_RETRIES + 1):
        try:
            _wrap_line_buffer = ""
            full = []
            for part in ollama_client.chat(model=model, messages=messages, stream=True):
                content = (part.get("message") or {}).get("content") or ""
                if content:
                    full.append(content)
                    if stream_output:
                        if wrap_width > 0:
                            _wrap_stream_print(content, wrap_width, continuation_prefix)
                        else:
                            print(content, end="", flush=True)
            if stream_output:
                if _wrap_line_buffer:
                    print(_wrap_line_buffer, end="", flush=True)
                print()
            return "".join(full)
        except Exception as e:
            last_error = e
            if attempt < OLLAMA_MAX_RETRIES:
                time.sleep(OLLAMA_RETRY_DELAY_SECONDS)
                continue
            raise last_error


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
                if self._closed:
                    if len(self._buffer) == 0:
                        return b""
                    # Flush remaining partial data so we don't hang forever.
                    out = bytes(self._buffer)
                    self._buffer.clear()
                    return out
                if len(self._buffer) >= chunk_size:
                    out = bytes(self._buffer[:chunk_size])
                    del self._buffer[:chunk_size]
                    return out
            await asyncio.sleep(0.001)

    def reset(self) -> None:
        """Re-open the adapter so it can be reused after a failed connection attempt."""
        with self._lock:
            self._closed = False

    def close(self) -> None:
        self._closed = True


def main():
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
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
    chat_model = prompt_model()
    samplerate = int(dev["default_samplerate"])
    channels = dev["max_input_channels"]
    language = os.environ.get("SPEECHMATICS_LANGUAGE", "en")

    enrollment = load_speaker_enrollment()
    if os.environ.get("COACH_USE_SPEAKER_ENROLLMENT", "1").strip().lower() in ("0", "false", "no"):
        enrollment = None
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
    # Load but do not send the initial prompt until there is actual conversation.
    prompt_text = load_prompt()
    prompt_applied = False
    has_responded = False
    # Queue of (speaker_label, text) from Speechmatics, before grouping into full messages.
    # speaker_label is whatever Speechmatics returns (e.g. "candidate", "S1", "S2") or None.
    transcript_queue: list[tuple[str | None, str]] = []
    transcript_lock = threading.Lock()
    stop = threading.Event()
    capture_enabled = threading.Event()
    capture_enabled.set()
    debug = os.environ.get("COACH_DEBUG", "").strip().lower() in ("1", "true", "yes")
    try:
        wrap_width = max(0, int(os.environ.get("COACH_WRAP", "0") or "0"))
    except ValueError:
        wrap_width = 0
    tts = os.environ.get("COACH_TTS", "").strip().lower() in ("1", "true", "yes")
    use_color = sys.stdout.isatty() and os.environ.get("COACH_NO_COLOR", "").strip().lower() not in ("1", "true", "yes")
    format_output = os.environ.get("COACH_FORMAT", "1").strip().lower() not in ("0", "false", "no")
    _dim = "\033[2m" if use_color else ""
    _reset = "\033[0m" if use_color else ""

    audio_adapter = SpeechmaticsAudioAdapter()
    speechmatics_base_url = os.environ.get("SPEECHMATICS_URL", DEFAULT_SPEECHMATICS_URL).rstrip("/")
    connection_url = f"{speechmatics_base_url}/{language}"

    def on_final_transcript(msg):
        try:
            results = msg.get("results")
            if results:
                # Speaker diarization: queue segments from all speakers, labeled.
                groups = group_tokens(results)
                for group in groups:
                    if not group:
                        continue
                    speaker = get_speaker(group[0])
                    text = join_tokens(group).strip()
                    if text:
                        with transcript_lock:
                            transcript_queue.append((speaker, text))
            else:
                # Fallback when API returns a single transcript string (e.g. metadata.transcript).
                text = (msg.get("metadata") or {}).get("transcript") or ""
                if isinstance(text, str) and text.strip():
                    with transcript_lock:
                        transcript_queue.append((None, text.strip()))
        except Exception as e:
            print(f"  [Transcript parse] {e}", file=sys.stderr)

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
            # Longer trigger so we get fewer, larger utterance chunks and don't reset debounce every 0.6s.
            end_of_utterance_silence_trigger=1.5,
        ),
    )
    settings = speechmatics.models.AudioSettings()
    settings.encoding = "pcm_f32le"
    settings.sample_rate = samplerate
    settings.chunk_size = STT_CHUNK_BYTES

    def audio_callback(indata, frames, time_info, status):
        try:
            if status:
                print(f"  ⚠ {status}", file=sys.stderr)
            if not capture_enabled.is_set():
                return
            mono = indata if indata.ndim == 1 else indata.mean(axis=1)
            audio_adapter.write_audio(mono.astype(np.float32).tobytes())
        except Exception as e:
            print(f"  [Audio] {e}", file=sys.stderr)

    def keyboard_listener():
        """Toggle audio capture with spacebar while streaming.

        Space: pause/resume capture (does not stop transcription threads).
        Ctrl+C is still handled by the main SIGINT handler.
        """
        if not sys.stdin.isatty():
            return
        fd = sys.stdin.fileno()
        try:
            old_settings = termios.tcgetattr(fd)
        except termios.error:
            return
        try:
            tty.setcbreak(fd)
            while not stop.is_set():
                try:
                    ch = sys.stdin.read(1)
                except (OSError, KeyboardInterrupt):
                    break
                if not ch:
                    continue
                if ch == " ":
                    if capture_enabled.is_set():
                        capture_enabled.clear()
                        print("\n  [Audio] Paused (press Space to resume)", flush=True)
                    else:
                        capture_enabled.set()
                        print("\n  [Audio] Resumed (press Space to pause)", flush=True)
                # Let Ctrl+C be handled by SIGINT; don't intercept here.
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            except termios.error:
                pass

    def should_send_to_coach(text: str, already_responded: bool) -> bool:
        """Decide whether to send this utterance to the coach.

        - Always send the first substantive utterance.
        - After that, require a clear question or problem/topic-like content.
        """
        t = (text or "").strip()
        if not t:
            return False

        if not already_responded:
            return True

        lower = t.lower()

        # Any explicit question.
        if "?" in t:
            return True

        # Obvious problem/topic indicators.
        keywords = (
            "problem",
            "question",
            "stuck",
            "issue",
            "bug",
            "exercise",
            "task",
            "topic",
            "interview",
        )
        if any(k in lower for k in keywords):
            return True

        # Longer utterances are usually substantive enough to treat as topics.
        if len(lower.split()) >= 6:
            return True

        return False

    def ollama_worker():
        nonlocal prompt_applied, has_responded
        # Buffer of (speaker_label, text) segments that together form a full message.
        message_buffer: list[tuple[str | None, str]] = []
        last_segment_time: float | None = None  # Only set when we have added at least one segment.
        # Pause duration: read once, enforce minimum so we always debounce.
        try:
            pause_seconds = float(
                os.environ.get("COACH_FULL_MESSAGE_PAUSE_SECONDS", DEFAULT_FULL_MESSAGE_PAUSE_SECONDS)
            )
        except ValueError:
            pause_seconds = DEFAULT_FULL_MESSAGE_PAUSE_SECONDS
        pause_seconds = max(pause_seconds, MIN_FULL_MESSAGE_PAUSE_SECONDS)
        while not stop.is_set():
            with transcript_lock:
                if not transcript_queue:
                    new_segments: list[tuple[str | None, str]] = []
                else:
                    new_segments = transcript_queue[:]
                    transcript_queue.clear()
            for speaker, text in new_segments:
                if text and text.strip():
                    message_buffer.append((speaker, text.strip()))
                    last_segment_time = time.monotonic()
            full_message = None
            # Only send after a full pause with no new segments this round (strict debounce).
            if message_buffer and last_segment_time is not None and not new_segments:
                now = time.monotonic()
                if (now - last_segment_time) >= pause_seconds:
                    parts: list[str] = []
                    for speaker, text in message_buffer:
                        if enrollment and speaker == "candidate":
                            label = "Candidate"
                        elif speaker == candidate_speaker_label and candidate_speaker_label is not None:
                            label = f"Speaker {candidate_speaker_label} (candidate)"
                        elif speaker:
                            label = f"Speaker {speaker}"
                        else:
                            label = "Speaker"
                        parts.append(f"[{label}] {text}")
                    full_message = "\n".join(parts)
                    message_buffer.clear()
                    last_segment_time = None
            if full_message:
                if not should_send_to_coach(full_message, has_responded):
                    # Ignore utterances that don't look like questions or real topics.
                    stop.wait(timeout=0.3)
                    continue
                if debug:
                    print(f"\n  You: {full_message}")
                if prompt_text and not prompt_applied:
                    messages.insert(0, {"role": "system", "content": prompt_text})
                    prompt_applied = True
                messages.append({"role": "user", "content": full_message})
                try:
                    print("\n  —— Coach ——", flush=True)
                    print(f"  {_dim}Thinking...{_reset}", flush=True)
                    if format_output:
                        reply = stream_chat(
                            ollama_client, messages, chat_model, stream_output=False
                        )
                        if reply:
                            formatted = format_for_terminal(
                                reply, use_color, wrap_width
                            )
                            print(formatted)
                    else:
                        reply = stream_chat(
                            ollama_client,
                            messages,
                            chat_model,
                            wrap_width=wrap_width,
                            stream_output=True,
                        )
                    if reply:
                        messages.append({"role": "assistant", "content": reply})
                        has_responded = True
                        if tts:
                            try:
                                subprocess.run(
                                    ["say", reply],
                                    check=False,
                                    timeout=300,
                                    capture_output=True,
                                )
                            except (OSError, subprocess.TimeoutExpired):
                                pass
                except Exception as e:
                    print(f"  [Ollama error] {e}", file=sys.stderr)
            stop.wait(timeout=0.3)

    def run_speechmatics():
        last_error = None
        for attempt in range(1, SPEECHMATICS_MAX_RETRIES + 1):
            if stop.is_set():
                break
            audio_adapter.reset()
            try:
                ws.run_synchronously(audio_adapter, conf, settings)
                return
            except TimeoutError as e:
                last_error = e
                if stop.is_set():
                    break
                if attempt < SPEECHMATICS_MAX_RETRIES:
                    print(
                        f"\n  [Speechmatics] Connection timed out (attempt {attempt}/{SPEECHMATICS_MAX_RETRIES}). "
                        f"Retrying in {SPEECHMATICS_RETRY_DELAY_SECONDS}s...",
                        file=sys.stderr,
                    )
                    for _ in range(SPEECHMATICS_RETRY_DELAY_SECONDS * 10):
                        if stop.is_set():
                            break
                        time.sleep(0.1)
                else:
                    print(
                        "\n  [Speechmatics] Connection timed out after retries.",
                        "Check firewall/VPN or set SPEECHMATICS_URL (e.g. wss://neu.rt.speechmatics.com/v2).",
                        file=sys.stderr,
                    )
            except (ConnectionError, OSError) as e:
                last_error = e
                if stop.is_set():
                    break
                if attempt < SPEECHMATICS_MAX_RETRIES:
                    print(
                        f"\n  [Speechmatics] {e} (attempt {attempt}/{SPEECHMATICS_MAX_RETRIES}). "
                        f"Retrying in {SPEECHMATICS_RETRY_DELAY_SECONDS}s...",
                        file=sys.stderr,
                    )
                    for _ in range(SPEECHMATICS_RETRY_DELAY_SECONDS * 10):
                        if stop.is_set():
                            break
                        time.sleep(0.1)
                else:
                    if not stop.is_set():
                        print(f"\n  [Speechmatics error] {e}", file=sys.stderr)
                    break
            except Exception as e:
                last_error = e
                msg = str(e).lower()
                is_connection_error = any(
                    x in msg for x in ("handshake", "connection", "timeout", "closed", "open")
                )
                if is_connection_error and attempt < SPEECHMATICS_MAX_RETRIES and not stop.is_set():
                    print(
                        f"\n  [Speechmatics] {e} (attempt {attempt}/{SPEECHMATICS_MAX_RETRIES}). "
                        f"Retrying in {SPEECHMATICS_RETRY_DELAY_SECONDS}s...",
                        file=sys.stderr,
                    )
                    for _ in range(SPEECHMATICS_RETRY_DELAY_SECONDS * 10):
                        if stop.is_set():
                            break
                        time.sleep(0.1)
                else:
                    if not stop.is_set():
                        print(f"\n  [Speechmatics error] {e}", file=sys.stderr)
                    break
        audio_adapter.close()

    signal.signal(signal.SIGINT, lambda *_: (stop.set(), sys.exit(0)))

    worker = threading.Thread(target=ollama_worker, daemon=True)
    worker.start()

    stt_thread = threading.Thread(target=run_speechmatics, daemon=True)
    stt_thread.start()

    key_thread = threading.Thread(target=keyboard_listener, daemon=True)
    key_thread.start()

    with sd.InputStream(
        device=device_id,
        samplerate=samplerate,
        channels=channels,
        callback=audio_callback,
        blocksize=int(samplerate * 0.05),
    ):
        print("  (stream open — speaking will be transcribed and sent to Ollama)")
        print("  (press Space to pause/resume audio capture)")
        while not stop.is_set():
            time.sleep(0.2)
        stop.set()


if __name__ == "__main__":
    main()
