"""Keep only speakable text from a runtime's streamed reply (voice turns): drop code blocks, markdown marks
and URLs as deltas arrive, without waiting for whole sentences."""

from __future__ import annotations

_URL_PREFIXES = ("https://", "http://")
_MARKS = set("*#`~|>")


class SpeechFilter:
    def __init__(self) -> None:
        self._in_code: bool = False
        self._pending: str = ""

    def feed(self, delta: str) -> str:
        text = self._pending + delta
        self._pending = ""
        out: list[str] = []
        i = 0
        n = len(text)
        while i < n:
            rest = text[i:]
            if rest.startswith("```"):
                self._in_code = not self._in_code
                i += 3
                continue
            if rest and set(rest) <= {"`"} and len(rest) < 3:
                self._pending = rest  # maybe the start of a fence
                break
            if self._in_code:
                i += 1
                continue
            if rest.startswith(_URL_PREFIXES):
                end = next((j for j in range(i, n) if text[j].isspace()), -1)
                if end == -1:
                    self._pending = rest  # the URL may continue in the next delta
                    break
                out.append("the link")
                i = end
                continue
            if any(p.startswith(rest) for p in _URL_PREFIXES) and rest[:1] == "h" and len(rest) < 8:
                self._pending = rest
                break
            ch = text[i]
            if ch not in _MARKS:
                out.append(ch)
            i += 1
        return "".join(out)

    def flush(self) -> str:
        rest, self._pending = self._pending, ""
        if self._in_code or rest.startswith(_URL_PREFIXES):
            return "the link" if rest.startswith(_URL_PREFIXES) else ""
        return "".join(ch for ch in rest if ch not in _MARKS)
