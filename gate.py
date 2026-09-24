"""kantharos-a2a gate — loads body from adjacent base64 part files."""
from __future__ import annotations
import base64
from pathlib import Path

_dir = Path(__file__).resolve().parent
_parts = sorted(_dir.glob("gate_body.part*.b64"))
if not _parts:
    raise ImportError("kantharos-a2a gate_body.part*.b64 payloads missing")
_b64 = "".join("".join(p.read_text(encoding="ascii").split()) for p in _parts)
_code = base64.b64decode(_b64).decode("utf-8")
exec(compile(_code, str(_dir / "gate_body.py"), "exec"), globals())
