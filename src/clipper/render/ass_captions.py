"""Our own ASS subtitle writer (ROADMAP 4.6): word groups of 2-4, active word highlighted,
hook box for the first 2.5 s. All text is upper-cased with `tr_upper` (never str.upper)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from clipper.render.tr_text import tr_upper, wrap_words

PUNCT_BREAK = (".", "!", "?", ",", ";", ":", "…")
HIGHLIGHT = r"{\c&H00FFFF&\fscx110\fscy110}"
RESET = r"{\r}"


@dataclass(frozen=True, slots=True)
class CapWord:
    w: str
    s: float  # seconds from the start of the rendered clip
    e: float


@dataclass(frozen=True, slots=True)
class CaptionStyle:
    font: str = "Montserrat ExtraBold"
    size: int = 78
    outline: int = 6
    hook_size: int = 64
    max_words: int = 4
    max_chars: int = 18
    gap_break: float = 0.6
    hook_seconds: float = 2.5
    # Upper-case characters that fit on one 1080 px line at `hook_size` (Montserrat
    # ExtraBold, box padding and margins included). WrapStyle 2 never wraps on its own.
    hook_line_chars: int = 18
    hook_max_lines: int = 3
    hook_min_size: int = 40


def caption_y(layout: str, height: int = 1920) -> int:
    """Vertical centre of the captions: the seam between the panels for `stack`."""
    return round(height * (640 / 1920)) if layout == "stack" else round(height * (1350 / 1920))


def _clean(text: str) -> str:
    return " ".join(text.replace("{", "(").replace("}", ")").replace("\\", "/").split())


def fit_lines(
    text: str, size: int, line_chars: int, max_lines: int, min_size: int
) -> tuple[list[str], int]:
    """Break `text` into at most `max_lines` lines that fit the frame width, shrinking
    the font when needed. `line_chars` is how many characters fit at `size`. Returns
    (lines, font size)."""
    words = text.split()
    cur = size
    while True:
        chars = max(1, int(line_chars * size / cur))
        lines = [" ".join(x) for x in wrap_words(words, chars)]
        longest = max((len(x) for x in lines), default=0)
        if (len(lines) <= max_lines and longest <= chars) or cur <= min_size:
            break
        cur = max(min_size, round(cur * 0.88))
    if longest > chars:  # one huge word: shrink until it fits the width
        cur = max(1, int(cur * chars / longest))
    return lines, cur


def group_words(
    words: Sequence[CapWord], style: CaptionStyle = CaptionStyle()
) -> list[list[CapWord]]:
    """Break into groups of up to `max_words` words / `max_chars` characters; a group also
    ends after punctuation or before a silence longer than `gap_break`."""
    groups: list[list[CapWord]] = []
    cur: list[CapWord] = []
    for i, w in enumerate(words):
        text = " ".join(x.w for x in [*cur, w])
        if cur and (len(cur) >= style.max_words or len(text) > style.max_chars):
            groups.append(cur)
            cur = []
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        if w.w.endswith(PUNCT_BREAK) or (nxt is not None and nxt.s - w.e > style.gap_break):
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    # Avoid lonely one-word groups when the previous group has room.
    merged: list[list[CapWord]] = []
    for g in groups:
        if (
            merged
            and len(g) == 1
            and len(merged[-1]) < style.max_words
            and len(" ".join(x.w for x in [*merged[-1], *g])) <= style.max_chars
            and g[0].s - merged[-1][-1].e <= style.gap_break
            and not merged[-1][-1].w.endswith(PUNCT_BREAK)
        ):
            merged[-1] = [*merged[-1], *g]
        else:
            merged.append(g)
    return merged


def ass_time(t: float) -> str:
    t = max(0.0, t)
    cs = round(t * 100)
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def build_ass(
    words: Sequence[CapWord],
    *,
    duration: float,
    layout: str,
    hook: str | None = None,
    width: int = 1080,
    height: int = 1920,
    style: CaptionStyle = CaptionStyle(),
) -> str:
    y = caption_y(layout, height)
    x = width // 2
    header = (
        "[Script Info]\nScriptType: v4.00+\nWrapStyle: 2\nScaledBorderAndShadow: yes\n"
        f"PlayResX: {width}\nPlayResY: {height}\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour,"
        " BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle,"
        " BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Cap,{style.font},{style.size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,"
        f"-1,0,0,0,100,100,0,0,1,{style.outline},0,5,40,40,0,1\n"
        f"Style: Hook,{style.font},{style.hook_size},&H00000000,&H00000000,&H00FFFFFF,"
        "&H00FFFFFF,-1,0,0,0,100,100,0,0,3,18,0,5,60,60,0,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV,"
        " Effect, Text\n"
    )
    events: list[str] = []
    if hook and hook.strip():
        end = min(style.hook_seconds, duration)
        scale = width / 1080
        lines, size = fit_lines(
            _clean(tr_upper(hook)),
            style.hook_size,
            round(style.hook_line_chars * scale),
            style.hook_max_lines,
            style.hook_min_size,
        )
        fs = "" if size == style.hook_size else f"\\fs{size}"
        events.append(
            f"Dialogue: 1,{ass_time(0)},{ass_time(end)},Hook,,0,0,0,,"
            f"{{\\pos({x},{round(height * 180 / 1920)}){fs}}}" + "\\N".join(lines)
        )
    for group in group_words([w for w in words if w.s < duration], style):
        texts = [_clean(tr_upper(w.w)) for w in group]
        g_end = min(group[-1].e, duration)
        n = len(" ".join(texts))
        # a group is at most max_chars unless one word alone is longer: shrink that one
        fs = f"\\fs{int(style.size * style.max_chars / n)}" if n > style.max_chars else ""
        for i, w in enumerate(group):
            start = w.s
            end = group[i + 1].s if i + 1 < len(group) else g_end
            if end <= start:
                continue
            parts = [f"{HIGHLIGHT}{t}{RESET}" if j == i else t for j, t in enumerate(texts)]
            events.append(
                f"Dialogue: 0,{ass_time(start)},{ass_time(min(end, duration))},Cap,,0,0,0,,"
                f"{{\\pos({x},{y}){fs}}}" + " ".join(parts)
            )
    return header + "\n".join(events) + ("\n" if events else "")


def clip_words(
    words: Sequence[tuple[str, float, float]], start: float, end: float
) -> list[CapWord]:
    """Transcript words (raw.mp4 time) inside [start, end], shifted to clip time."""
    out = []
    for w, s, e in words:
        if e <= start or s >= end:
            continue
        out.append(CapWord(w, max(0.0, s - start), min(end, e) - start))
    return out
