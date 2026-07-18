#!/usr/bin/env python3
"""
Hermes K10 Bridge — ESP32 voice device ↔ Hermes API (voice profile)

Protocols:
  1. Xiaozhi WebSocket (Opus)  — ws://0.0.0.0:8765  (existing xiaozhi firmware)
  2. Hermes-K10 PCM16 JSON     — same port, detected by message type
  3. Button webhook HTTP       — http://0.0.0.0:8766/button
  4. K10-Δ TCP sensors         — tcp://0.0.0.0:5555   (btn field triggers talk)

LLM backend: Hermes API server (voice profile gateway on :8642)
TTS: Piper (local) with edge-tts fallback
STT: faster-whisper
Mood: sentiment_daemon → voice/memories/MOOD.json + MEMORY.md (Honcho)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import struct
import subprocess
import uuid
from pathlib import Path
from typing import Dict, List, Optional

import httpx
import numpy as np

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
except ImportError:
    raise SystemExit("pip install websockets httpx numpy faster-whisper")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("k10-bridge")

# ── Paths & config ─────────────────────────────────────────────────────────────

SCRIPTS_DIR = Path(__file__).resolve().parent
VOICE_HOME = Path(os.environ.get(
    "VOICE_HERMES_HOME",
    Path.home() / "AppData" / "Local" / "hermes" / "profiles" / "voice",
))
SOUL_PATH = VOICE_HOME / "SOUL.md"

HERMES_API_BASE = os.environ.get("HERMES_API_URL", "http://127.0.0.1:8642/v1").rstrip("/")
HERMES_API_KEY = os.environ.get("HERMES_API_KEY", "k10-hermes-voice-local")
HERMES_MODEL = os.environ.get("HERMES_API_MODEL", "hermes-agent")

WS_HOST = os.environ.get("K10_WS_HOST", "0.0.0.0")
WS_PORT = int(os.environ.get("K10_WS_PORT", "8765"))
HTTP_PORT = int(os.environ.get("K10_HTTP_PORT", "8766"))
TCP_PORT = int(os.environ.get("K10_TCP_PORT", "5555"))

PIPER_VOICE = os.environ.get(
    "PIPER_VOICE",
    r"C:\piper\en_US-ryan-high.onnx",
)
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")
FFMPEG = os.environ.get("FFMPEG", "")

DEVICE_SR = 16000
FRAME_MS = 60
FRAME_SAMPLES = DEVICE_SR * FRAME_MS // 1000

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

# Import sentiment after path setup
import sys
sys.path.insert(0, str(SCRIPTS_DIR))
from sentiment_daemon import get_daemon  # noqa: E402

sentiment = get_daemon(VOICE_HOME)


def _load_system_prompt() -> str:
    base = ""
    if SOUL_PATH.exists():
        base = SOUL_PATH.read_text(encoding="utf-8").strip()
    mood = sentiment.mood_prompt_injection()
    voice_rules = (
        "You are speaking aloud through a voice device. "
        "Use short conversational sentences. No markdown, bullets, or code. "
        "Sound natural when read by TTS."
    )
    return f"{voice_rules}\n\n{mood}\n\n{base}"[:8000]


def _read_env_key() -> str:
    env_path = VOICE_HOME / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("API_SERVER_KEY="):
                return line.split("=", 1)[1].strip()
    return HERMES_API_KEY


API_KEY = _read_env_key()

# ── Lazy models ────────────────────────────────────────────────────────────────

_whisper = None
_piper = None


def _get_whisper():
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        log.info("Loading Whisper '%s'...", WHISPER_MODEL)
        _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    return _whisper


def _get_piper():
    global _piper
    if _piper is None:
        from piper.voice import PiperVoice
        log.info("Loading Piper '%s'...", PIPER_VOICE)
        _piper = PiperVoice.load(PIPER_VOICE, use_cuda=False)
    return _piper


# ── Audio helpers (from local_gateway.py) ──────────────────────────────────────

def _resample(audio: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst:
        return audio
    n = int(round(len(audio) * dst / src))
    return np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio)


def _unpack_bp3(data: bytes) -> bytes:
    _, _, size = struct.unpack_from(">BBH", data)
    return data[4 : 4 + size]


def _pack_bp3(opus: bytes) -> bytes:
    return struct.pack(">BBH", 0, 0, len(opus)) + opus


def _decode_opus(frames: List[bytes]) -> np.ndarray:
    import av
    codec = av.CodecContext.create("opus", "r")
    codec.sample_rate = DEVICE_SR
    codec.channels = 1
    chunks: List[np.ndarray] = []
    actual_sr = None
    for raw in frames:
        pkt = av.Packet(raw)
        try:
            for frame in codec.decode(pkt):
                if actual_sr is None:
                    actual_sr = frame.sample_rate
                arr = frame.to_ndarray()
                if arr.ndim > 1:
                    arr = arr[0]
                chunks.append(arr.astype(np.float32))
        except Exception:
            pass
    if not chunks:
        return np.array([], dtype=np.float32)
    pcm = np.concatenate(chunks)
    if actual_sr and actual_sr != DEVICE_SR:
        pcm = _resample(pcm, actual_sr, DEVICE_SR)
    return pcm


def _pcm16_to_f32(pcm_bytes: bytes) -> np.ndarray:
    arr = np.frombuffer(pcm_bytes, dtype=np.int16)
    return arr.astype(np.float32) / 32768.0


def _f32_to_pcm16_bytes(pcm: np.ndarray) -> bytes:
    return (pcm * 32767).clip(-32768, 32767).astype(np.int16).tobytes()


def _find_ffmpeg() -> str:
    if FFMPEG and Path(FFMPEG).exists():
        return FFMPEG
    candidates = [
        Path(r"C:\Users\jacob\AppData\Local\Microsoft\WinGet\Packages")
        / "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
        / "ffmpeg-8.1-full_build\bin\ffmpeg.exe",
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return "ffmpeg"


def _pcm_to_ogg_opus(pcm_i16: np.ndarray) -> bytes:
    ffmpeg = _find_ffmpeg()
    result = subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error",
         "-f", "s16le", "-ar", str(DEVICE_SR), "-ac", "1", "-i", "pipe:0",
         "-c:a", "libopus", "-b:a", "32k", "-frame_duration", str(FRAME_MS),
         "-f", "ogg", "pipe:1"],
        input=pcm_i16.tobytes(),
        capture_output=True,
    )
    return result.stdout


def _parse_ogg_packets(ogg: bytes) -> List[bytes]:
    packets: List[bytes] = []
    pos = 0
    page_num = 0
    while pos < len(ogg) - 27:
        if ogg[pos : pos + 4] != b"OggS":
            break
        num_segs = ogg[pos + 26]
        seg_table = list(ogg[pos + 27 : pos + 27 + num_segs])
        hdr_len = 27 + num_segs
        seg_pos = pos + hdr_len
        seg_idx = 0
        while seg_idx < num_segs:
            pkt_len = 0
            while seg_idx < num_segs:
                s = seg_table[seg_idx]
                pkt_len += s
                seg_idx += 1
                if s < 255:
                    break
            if page_num >= 2 and pkt_len > 0:
                packets.append(ogg[seg_pos : seg_pos + pkt_len])
            seg_pos += pkt_len
        pos += hdr_len + sum(seg_table)
        page_num += 1
    return packets


def _tts_to_opus_frames(text: str) -> List[bytes]:
    try:
        piper = _get_piper()
        chunks = list(piper.synthesize(text))
        if not chunks:
            return []
        src_sr = chunks[0].sample_rate
        pcm_f32 = np.concatenate([c.audio_float_array for c in chunks])
        if src_sr != DEVICE_SR:
            pcm_f32 = _resample(pcm_f32, src_sr, DEVICE_SR)
        pcm_i16 = (pcm_f32 * 32767).clip(-32768, 32767).astype(np.int16)
        ogg = _pcm_to_ogg_opus(pcm_i16)
        return [_pack_bp3(p) for p in _parse_ogg_packets(ogg)]
    except Exception as exc:
        log.warning("Piper TTS failed (%s), trying edge-tts", exc)
        raise exc


async def _tts_to_opus_frames_async(text: str) -> List[bytes]:
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, _tts_to_opus_frames, text)
    except Exception:
        return await _tts_edge_opus_frames_async(text)


async def _tts_edge_opus_frames_async(text: str) -> List[bytes]:
    import tempfile
    import edge_tts
    voice = "en-US-AriaNeural"
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        mp3_path = tmp.name
    comm = edge_tts.Communicate(text, voice)
    await comm.save(mp3_path)
    ffmpeg = _find_ffmpeg()
    result = subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-i", mp3_path,
         "-f", "s16le", "-ar", str(DEVICE_SR), "-ac", "1", "pipe:1"],
        capture_output=True,
    )
    Path(mp3_path).unlink(missing_ok=True)
    if not result.stdout:
        return []
    pcm = np.frombuffer(result.stdout, dtype=np.int16)
    ogg = _pcm_to_ogg_opus(pcm)
    return [_pack_bp3(p) for p in _parse_ogg_packets(ogg)]


def _transcribe(pcm: np.ndarray) -> str:
    rms = float(np.sqrt(np.mean(pcm ** 2))) if len(pcm) else 0.0
    if rms < 0.005:
        return ""
    segments, _ = _get_whisper().transcribe(pcm, beam_size=5, language="en")
    return " ".join(s.text.strip() for s in segments).strip()


# ── Hermes API client ──────────────────────────────────────────────────────────

class HermesClient:
    def __init__(self):
        self.session_id: Optional[str] = None
        self.system_prompt = _load_system_prompt()

    async def stream_reply(self, user_text: str):
        headers = {
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        }
        if self.session_id:
            headers["X-Hermes-Session-Id"] = self.session_id

        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_text},
        ]
        payload = {"model": HERMES_MODEL, "messages": messages, "stream": True, "temperature": 0.9}

        buffer = ""
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST", f"{HERMES_API_BASE}/chat/completions",
                headers=headers, json=payload,
            ) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    raise RuntimeError(f"Hermes API {resp.status_code}: {body[:300]}")
                sid = resp.headers.get("X-Hermes-Session-Id")
                if sid:
                    self.session_id = sid
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    chunk = line[6:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        delta = json.loads(chunk)["choices"][0]["delta"].get("content") or ""
                    except Exception:
                        continue
                    if not delta:
                        continue
                    buffer += delta
                    parts = _SENTENCE_END.split(buffer)
                    for s in parts[:-1]:
                        if s.strip():
                            yield s.strip()
                    buffer = parts[-1]
        if buffer.strip():
            yield buffer.strip()

    def refresh_mood(self):
        self.system_prompt = _load_system_prompt()


hermes = HermesClient()

# ── Shared turn logic ──────────────────────────────────────────────────────────

async def _run_turn(
    transcript: str,
    *,
    send_json=None,
    send_bin=None,
    send_pcm=None,
    session_tag: str = "",
    abort: Optional[asyncio.Event] = None,
) -> str:
    if not transcript.strip():
        return ""
    log.info("[%s] user: %r", session_tag, transcript)
    if send_json:
        await send_json({"type": "stt", "text": transcript})

    hermes.refresh_mood()
    full = ""
    if send_json:
        await send_json({"type": "tts", "state": "start"})

    loop = asyncio.get_running_loop()
    async for sentence in hermes.stream_reply(transcript):
        if abort and abort.is_set():
            break
        full += sentence + " "
        log.info("[%s] tts: %r", session_tag, sentence)
        if send_json:
            await send_json({"type": "tts", "state": "sentence_start", "text": sentence})

        if send_pcm:
            frames = await _tts_to_opus_frames_async(sentence)
            for f in frames:
                if abort and abort.is_set():
                    break
                pcm_payload = _unpack_bp3(f)
                await send_pcm(base64.b64encode(pcm_payload).decode())
        elif send_bin:
            frames = await _tts_to_opus_frames_async(sentence)
            for f in frames:
                if abort and abort.is_set():
                    break
                await send_bin(f)

    full = full.strip()
    if send_json:
        await send_json({"type": "tts", "state": "stop"})

    loop.run_in_executor(
        None,
        lambda: sentiment.record_exchange(
            transcript, full, trigger="voice", session_id=hermes.session_id or session_tag
        ),
    )
    return full


# ── Xiaozhi Opus WebSocket session ─────────────────────────────────────────────

class OpusSession:
    def __init__(self, ws):
        self.ws = ws
        self.sid = uuid.uuid4().hex[:8]
        self.opus_frames: List[bytes] = []
        self.listening = False
        self.processing = False
        self.abort = asyncio.Event()
        self._inject_text = ""

    async def _send(self, data: dict):
        await self.ws.send(json.dumps({"session_id": self.sid, **data}))

    async def _send_bin(self, frame: bytes):
        await self.ws.send(frame)

    async def run(self):
        raw = await self.ws.recv()
        hello = json.loads(raw)
        log.info("[%s] opus hello version=%s", self.sid, hello.get("version"))
        await self._send({
            "type": "hello",
            "transport": "websocket",
            "audio_params": {
                "format": "opus", "sample_rate": DEVICE_SR,
                "channels": 1, "frame_duration": FRAME_MS,
            },
        })
        async for msg in self.ws:
            if isinstance(msg, bytes):
                if self.listening:
                    self.opus_frames.append(_unpack_bp3(msg))
            else:
                await self._dispatch(json.loads(msg))

    async def _dispatch(self, msg: dict):
        mtype = msg.get("type")
        if mtype == "listen":
            state = msg.get("state")
            if state == "start":
                self.listening = True
                self.opus_frames.clear()
                self._inject_text = ""
                self.abort.clear()
            elif state in ("stop", "detect"):
                self.listening = False
                if state == "detect" and msg.get("text"):
                    self._inject_text = msg["text"]
                asyncio.ensure_future(self._turn())
        elif mtype == "abort":
            self.listening = False
            self.abort.set()

    async def _turn(self):
        if self.processing:
            return
        self.processing = True
        frames = list(self.opus_frames)
        self.opus_frames.clear()
        inject = self._inject_text
        self._inject_text = ""
        try:
            loop = asyncio.get_running_loop()
            if inject:
                transcript = inject
            else:
                pcm = await loop.run_in_executor(None, _decode_opus, frames)
                if len(pcm) < DEVICE_SR // 4:
                    return
                transcript = await loop.run_in_executor(None, _transcribe, pcm)
                if not transcript:
                    return
            await _run_turn(
                transcript,
                send_json=self._send,
                send_bin=self._send_bin,
                session_tag=self.sid,
                abort=self.abort,
            )
        except Exception as exc:
            log.error("[%s] turn error: %s", self.sid, exc, exc_info=True)
            try:
                await self._send({"type": "tts", "state": "stop"})
            except Exception:
                pass
        finally:
            self.processing = False

    async def trigger_button(self, text: str = ""):
        """External button press — inject text or process buffered audio."""
        if text:
            self._inject_text = text
            await self._turn()
        elif self.opus_frames:
            self.listening = False
            await self._turn()
        else:
            await self._send({"type": "listen", "state": "start", "mode": "manual"})
            self.listening = True


# ── PCM16 JSON WebSocket session (Hermes-K10 protocol) ─────────────────────────

class PCM16Session:
    def __init__(self, ws):
        self.ws = ws
        self.sid = uuid.uuid4().hex[:8]
        self.pcm_buffer = bytearray()
        self.in_session = False
        self.processing = False
        self.abort = asyncio.Event()

    async def _send(self, data: dict):
        await self.ws.send(json.dumps(data))

    async def run(self):
        async for msg in self.ws:
            if isinstance(msg, bytes):
                if self.in_session:
                    self.pcm_buffer.extend(msg)
            else:
                await self._dispatch(json.loads(msg))

    async def _dispatch(self, msg: dict):
        mtype = msg.get("type")
        if mtype == "hello":
            await self._send({
                "type": "hello",
                "transport": "websocket",
                "audio_params": {"format": "pcm16", "sample_rate": DEVICE_SR, "channels": 1},
            })
        elif mtype == "session_start":
            self.in_session = True
            self.pcm_buffer.clear()
            self.sid = msg.get("session_id", self.sid)
            self.abort.clear()
            log.info("[%s] pcm16 session_start trigger=%s", self.sid, msg.get("trigger"))
        elif mtype == "audio_chunk":
            b64 = msg.get("audio_base64") or msg.get("data", "")
            if b64:
                self.pcm_buffer.extend(base64.b64decode(b64))
        elif mtype == "session_end":
            self.in_session = False
            asyncio.ensure_future(self._turn())
        elif mtype == "abort":
            self.abort.set()

    async def _turn(self):
        if self.processing:
            return
        self.processing = True
        pcm_bytes = bytes(self.pcm_buffer)
        self.pcm_buffer.clear()
        try:
            loop = asyncio.get_running_loop()
            pcm = _pcm16_to_f32(pcm_bytes)
            if len(pcm) < DEVICE_SR // 4:
                return
            transcript = await loop.run_in_executor(None, _transcribe, pcm)
            if not transcript:
                return

            async def send_pcm(b64chunk: str):
                await self._send({
                    "type": "audio_chunk",
                    "audio_format": "pcm16",
                    "sample_rate": DEVICE_SR,
                    "audio_base64": b64chunk,
                })

            await _run_turn(
                transcript,
                send_json=self._send,
                send_pcm=send_pcm,
                session_tag=self.sid,
                abort=self.abort,
            )
            await self._send({"type": "session_end", "session_id": self.sid})
        except Exception as exc:
            log.error("[%s] pcm16 turn error: %s", self.sid, exc, exc_info=True)
        finally:
            self.processing = False


# ── Active sessions registry (for button webhook) ──────────────────────────────

_active_opus: Dict[str, OpusSession] = {}


async def _ws_handler(ws):
    addr = ws.remote_address
    log.info("WS connect from %s", addr)
    try:
        first = await ws.recv()
        if isinstance(first, bytes):
            session = OpusSession(ws)
            _active_opus[session.sid] = session
            session.opus_frames.append(_unpack_bp3(first))
            await session._send({
                "type": "hello", "transport": "websocket",
                "audio_params": {"format": "opus", "sample_rate": DEVICE_SR,
                                 "channels": 1, "frame_duration": FRAME_MS},
            })
            async for msg in ws:
                if isinstance(msg, bytes) and session.listening:
                    session.opus_frames.append(_unpack_bp3(msg))
                elif isinstance(msg, str):
                    await session._dispatch(json.loads(msg))
        else:
            hello = json.loads(first)
            if hello.get("type") == "hello" or hello.get("capabilities"):
                session = PCM16Session(ws)
                await session._dispatch(hello)
                async for msg in ws:
                    if isinstance(msg, str):
                        await session._dispatch(json.loads(msg))
                    elif session.in_session:
                        session.pcm_buffer.extend(msg)
            else:
                session = OpusSession(ws)
                _active_opus[session.sid] = session
                await session._dispatch(hello)
                async for msg in ws:
                    if isinstance(msg, bytes) and session.listening:
                        session.opus_frames.append(_unpack_bp3(msg))
                    elif isinstance(msg, str):
                        await session._dispatch(json.loads(msg))
    except ConnectionClosed:
        pass
    except Exception as exc:
        log.error("WS fatal: %s", exc, exc_info=True)
    finally:
        for sid, s in list(_active_opus.items()):
            if s.ws == ws:
                del _active_opus[sid]
        log.info("WS disconnected %s", addr)


# ── HTTP button webhook ────────────────────────────────────────────────────────

async def _http_handler(reader, writer):
    try:
        raw = await reader.read(65536)
        text = raw.decode("utf-8", errors="replace")
        line = text.split("\r\n")[0] if text else ""
        body = ""
        if "\r\n\r\n" in text:
            body = text.split("\r\n\r\n", 1)[1]

        if "POST" not in line:
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
            await writer.drain()
            writer.close()
            return

        payload = {}
        if body.strip():
            try:
                payload = json.loads(body)
            except Exception:
                payload = {"text": body.strip()}

        log.info("Button webhook: %s", payload)
        inject = payload.get("text", "")
        triggered = 0
        for session in list(_active_opus.values()):
            asyncio.ensure_future(session.trigger_button(inject))
            triggered += 1

        resp = json.dumps({"ok": True, "triggered_sessions": triggered, "payload": payload})
        writer.write(
            f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(resp)}\r\n\r\n{resp}".encode()
        )
        await writer.drain()
    except Exception as exc:
        log.error("HTTP handler error: %s", exc)
    finally:
        try:
            writer.close()
        except Exception:
            pass


# ── K10-Δ TCP sensor listener ──────────────────────────────────────────────────

async def _tcp_handler(reader, writer):
    addr = writer.get_extra_info("peername")
    log.info("TCP connect from %s", addr)
    last_btn = 0
    buf = ""
    try:
        while True:
            data = await reader.read(4096)
            if not data:
                break
            buf += data.decode("utf-8", errors="replace")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                if msg.get("t") == "sensor":
                    btn = int(msg.get("btn", 0))
                    if btn and not last_btn:
                        log.info("K10-Δ button press from %s", addr)
                        for session in list(_active_opus.values()):
                            asyncio.ensure_future(session.trigger_button())
                    last_btn = btn
    except Exception as exc:
        log.error("TCP error %s: %s", addr, exc)
    finally:
        writer.close()
        log.info("TCP disconnected %s", addr)


# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    log.info("Voice home: %s", VOICE_HOME)
    log.info("Hermes API: %s (key configured)", HERMES_API_BASE)

    # Probe Hermes API
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(
                HERMES_API_BASE.replace("/v1", "") + "/health",
                headers={"Authorization": f"Bearer {API_KEY}"},
            )
            if r.status_code == 200:
                log.info("Hermes API health OK")
            else:
                log.warning("Hermes API health returned %s — start: hermes -p voice gateway start", r.status_code)
    except Exception as exc:
        log.warning("Hermes API not reachable (%s). Run: hermes -p voice gateway start", exc)

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _get_whisper)

    ws_server = await websockets.serve(_ws_handler, WS_HOST, WS_PORT)
    http_server = await asyncio.start_server(_http_handler, WS_HOST, HTTP_PORT)
    tcp_server = await asyncio.start_server(_tcp_handler, WS_HOST, TCP_PORT)

    log.info("Ready:")
    log.info("  WebSocket (Opus + PCM16): ws://%s:%d", WS_HOST, WS_PORT)
    log.info("  Button webhook HTTP:      http://%s:%d/button", WS_HOST, HTTP_PORT)
    log.info("  K10-Δ TCP sensors:        tcp://%s:%d", WS_HOST, TCP_PORT)

    await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())