"""AOB (array-of-bytes) scanning — the job CE's `aobscan` does, with an honest
uniqueness check and a page-class filter.

CE-style pattern strings are accepted verbatim, e.g.
    "4D 8B 1A 4D 8B 5B ?? 49 8B CA 8B 55 ?? 4D 8B C1"
`??` (or `?`) is a wildcard byte. Matching compiles the pattern to a bytes
regex and lets the C engine sweep each region (a Python byte loop would be far
too slow). The player hook only scans EXECUTABLE pages — a few hundred MB of
code, ~0.1s — never the multi-GB heap.

Reads go through the read-only `core.proc.Proc` (the Rust reader); this module
performs no writes.
"""
from __future__ import annotations

import re

from .proc import Proc, ProcError

# executable page protections (PAGE_EXECUTE / _READ / _READWRITE / _WRITECOPY)
_EXEC = 0x10 | 0x20 | 0x40 | 0x80
_WRITE = 0x04 | 0x08 | 0x40 | 0x80
_CHUNK = 4 * 1024 * 1024


def parse_pattern(pattern: str) -> tuple[re.Pattern[bytes], int]:
    """Compile a CE-style AOB string to (compiled_regex, byte_length)."""
    tokens = pattern.split()
    if not tokens:
        raise ValueError("empty pattern")
    parts: list[bytes] = []
    for tok in tokens:
        if tok in ("??", "?"):
            parts.append(b".")
        else:
            try:
                parts.append(re.escape(bytes([int(tok, 16)])))
            except ValueError:
                raise ValueError(f"bad pattern token {tok!r} in {pattern!r}") from None
    return re.compile(b"".join(parts), re.DOTALL), len(tokens)


def scan(proc: Proc, pattern: str, *, executable: bool | None = None,
         writable: bool | None = None, limit: int | None = None) -> list[int]:
    """Absolute addresses of every match. `executable`/`writable` restrict the
    page class; `limit` stops early (uniqueness checks pass limit=3)."""
    if not proc.has_scan:
        raise ProcError("AOB scan needs the farever_native backend")
    rx, plen = parse_pattern(pattern)
    overlap = plen - 1
    hits: list[int] = []
    for (base, size, protect, _rw) in proc.regions():
        if executable is not None and bool(protect & _EXEC) != executable:
            continue
        if writable is not None and bool(protect & _WRITE) != writable:
            continue
        off = 0
        while off < size:
            this = min(_CHUNK, size - off)
            tail = overlap if (off + this < size) else 0
            start = base + off
            buf = proc.try_read(start, this + tail)
            if buf:
                for m in rx.finditer(buf):
                    hits.append(start + m.start())
            off += this
        if limit and len(set(hits)) >= limit:
            break
    return sorted(set(hits))


def find_unique(proc: Proc, pattern: str, **kw) -> int:
    """Scan and insist on exactly one hit — the CT's 'should be unique'
    contract. Raises ProcError (with a count) otherwise, so we never patch an
    ambiguous or missing target. If the CT is already hooked at the same site
    the original bytes are gone, so this returns 0 and we safely abort."""
    kw.setdefault("limit", 3)
    hits = scan(proc, pattern, **kw)
    if not hits:
        raise ProcError(
            "AOB not found. The game was patched, OR Farever.CT is already "
            "hooked at this site — unload it and retry.")
    if len(hits) > 1:
        preview = ", ".join(f"{h:#x}" for h in hits[:4])
        raise ProcError(
            f"AOB matched {len(hits)} sites ({preview}…); expected exactly 1. "
            "Refusing to patch an ambiguous target.")
    return hits[0]
