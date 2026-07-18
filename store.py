import json
import threading
from pathlib import Path

SOUL_PATH = Path("./soul.json")
EPISODES_PATH = Path("./episodes.jsonl")
NOTES_PATH = Path("./notes.json")
HYPO_PATH = Path("./hypotheses.json")
DREAM_LOG = Path("./dream_log.jsonl")
WORKSPACE = Path("./workspace")
K10_ROOT = Path(__file__).resolve().parent

state_lock = threading.Lock()
_TEXT_ENCODING = "utf-8"


def _read_text(path: Path) -> str:
    return path.read_text(encoding=_TEXT_ENCODING, errors="replace")


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding=_TEXT_ENCODING)


def load_soul() -> dict:
    if not SOUL_PATH.exists():
        return {"identity": "K10-Δ", "boot_count": 0, "axioms": [], "semantic": {}}
    return json.loads(_read_text(SOUL_PATH))


def save_soul(s: dict):
    _write_text(SOUL_PATH, json.dumps(s, indent=2))


def append_episode(ep: dict):
    with EPISODES_PATH.open("a", encoding=_TEXT_ENCODING) as f:
        f.write(json.dumps(ep, ensure_ascii=False) + "\n")


def read_episodes(n: int = 100) -> list:
    if not EPISODES_PATH.exists():
        return []
    try:
        fsize = EPISODES_PATH.stat().st_size
        if fsize < 8192:
            lines = _read_text(EPISODES_PATH).splitlines()
            out = []
            for line in lines[-n:]:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
            return out
        with open(EPISODES_PATH, "rb") as f:
            chunk_size = 4096
            bufs = []
            collected = 0
            offset = fsize
            while collected <= n and offset > 0:
                read_size = min(chunk_size, offset)
                offset -= read_size
                f.seek(offset)
                chunk = f.read(read_size)
                bufs.insert(0, chunk)
                collected += chunk.count(b"\n")
            raw = b"".join(bufs).decode(_TEXT_ENCODING, errors="replace")
            lines = raw.splitlines()
            out = []
            for line in lines[-n:]:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
            return out
    except Exception:
        lines = _read_text(EPISODES_PATH).splitlines()
        out = []
        for line in lines[-n:]:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
        return out


def load_notes() -> dict:
    if not NOTES_PATH.exists():
        return {}
    return json.loads(_read_text(NOTES_PATH))


def save_notes(n: dict):
    _write_text(NOTES_PATH, json.dumps(n, indent=2))