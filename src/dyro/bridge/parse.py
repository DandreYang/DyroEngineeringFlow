"""Bounded, duplicate-key-aware JSON parser for one-shot Bridge requests."""

from __future__ import annotations

MAX_REQUEST_BYTES = 256 * 1024
MAX_DEPTH = 64
MAX_NODES = 10_000
MAX_NUMERIC_TOKEN = 128


class BoundedJSONError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def load_bounded_json(raw: bytes) -> object:
    if not isinstance(raw, (bytes, bytearray)):
        raise BoundedJSONError("INVALID_JSON")
    if len(raw) > MAX_REQUEST_BYTES:
        raise BoundedJSONError("REQUEST_TOO_LARGE")
    try:
        text = bytes(raw).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BoundedJSONError("INVALID_JSON") from exc
    return _Parser(text).parse()


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.i = 0
        self.n = len(text)
        self.nodes = 0

    def parse(self) -> object:
        self._skip()
        if self.i >= self.n:
            raise BoundedJSONError("INVALID_JSON")
        value = self._value(1)
        self._skip()
        if self.i != self.n:
            raise BoundedJSONError("INVALID_JSON")
        return value

    def _value(self, depth: int) -> object:
        if depth > MAX_DEPTH:
            raise BoundedJSONError("INVALID_JSON")
        self._count()
        ch = self._peek()
        if ch == "{":
            return self._object(depth)
        if ch == "[":
            return self._array(depth)
        if ch == '"':
            return self._string()
        if ch == "-" or ch.isdigit():
            return self._number()
        if self._take("true"):
            return True
        if self._take("false"):
            return False
        if self._take("null"):
            return None
        raise BoundedJSONError("INVALID_JSON")

    def _object(self, depth: int) -> dict[str, object]:
        self._eat("{")
        self._skip()
        if self._peek() == "}":
            self.i += 1
            return {}
        result: dict[str, object] = {}
        while True:
            self._skip()
            if self._peek() != '"':
                raise BoundedJSONError("INVALID_JSON")
            key = self._string()
            if key in result:
                raise BoundedJSONError("INVALID_JSON")
            self._skip()
            self._eat(":")
            self._skip()
            result[key] = self._value(depth + 1)
            self._skip()
            ch = self._peek()
            if ch == ",":
                self.i += 1
                continue
            if ch == "}":
                self.i += 1
                return result
            raise BoundedJSONError("INVALID_JSON")

    def _array(self, depth: int) -> list[object]:
        self._eat("[")
        self._skip()
        if self._peek() == "]":
            self.i += 1
            return []
        items: list[object] = []
        while True:
            self._skip()
            items.append(self._value(depth + 1))
            self._skip()
            ch = self._peek()
            if ch == ",":
                self.i += 1
                continue
            if ch == "]":
                self.i += 1
                return items
            raise BoundedJSONError("INVALID_JSON")

    def _string(self) -> str:
        self._eat('"')
        out: list[str] = []
        while self.i < self.n:
            ch = self.text[self.i]
            self.i += 1
            if ch == '"':
                return "".join(out)
            if ch == "\\":
                out.append(self._escape())
                continue
            if ord(ch) < 0x20:
                raise BoundedJSONError("INVALID_JSON")
            out.append(ch)
        raise BoundedJSONError("INVALID_JSON")

    def _escape(self) -> str:
        if self.i >= self.n:
            raise BoundedJSONError("INVALID_JSON")
        ch = self.text[self.i]
        self.i += 1
        mapping = {
            '"': '"',
            "\\": "\\",
            "/": "/",
            "b": "\b",
            "f": "\f",
            "n": "\n",
            "r": "\r",
            "t": "\t",
        }
        if ch in mapping:
            return mapping[ch]
        if ch != "u":
            raise BoundedJSONError("INVALID_JSON")
        if self.i + 4 > self.n:
            raise BoundedJSONError("INVALID_JSON")
        hex_digits = self.text[self.i : self.i + 4]
        self.i += 4
        try:
            code = int(hex_digits, 16)
        except ValueError as exc:
            raise BoundedJSONError("INVALID_JSON") from exc
        if 0xD800 <= code <= 0xDFFF:
            raise BoundedJSONError("INVALID_JSON")
        return chr(code)

    def _number(self) -> int | float:
        start = self.i
        if self._peek() == "-":
            self.i += 1
        if self.i >= self.n or not self.text[self.i].isdigit():
            raise BoundedJSONError("INVALID_JSON")
        if self.text[self.i] == "0":
            self.i += 1
        else:
            while self.i < self.n and self.text[self.i].isdigit():
                self.i += 1
        is_float = False
        if self.i < self.n and self.text[self.i] == ".":
            is_float = True
            self.i += 1
            if self.i >= self.n or not self.text[self.i].isdigit():
                raise BoundedJSONError("INVALID_JSON")
            while self.i < self.n and self.text[self.i].isdigit():
                self.i += 1
        if self.i < self.n and self.text[self.i] in "eE":
            is_float = True
            self.i += 1
            if self.i < self.n and self.text[self.i] in "+-":
                self.i += 1
            if self.i >= self.n or not self.text[self.i].isdigit():
                raise BoundedJSONError("INVALID_JSON")
            while self.i < self.n and self.text[self.i].isdigit():
                self.i += 1
        token = self.text[start : self.i]
        if len(token) > MAX_NUMERIC_TOKEN:
            raise BoundedJSONError("INVALID_JSON")
        try:
            return float(token) if is_float else int(token)
        except ValueError as exc:
            raise BoundedJSONError("INVALID_JSON") from exc

    def _count(self) -> None:
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise BoundedJSONError("INVALID_JSON")

    def _skip(self) -> None:
        while self.i < self.n and self.text[self.i] in " \t\r\n":
            self.i += 1

    def _peek(self) -> str:
        if self.i >= self.n:
            raise BoundedJSONError("INVALID_JSON")
        return self.text[self.i]

    def _eat(self, expected: str) -> None:
        if self.i >= self.n or self.text[self.i] != expected:
            raise BoundedJSONError("INVALID_JSON")
        self.i += 1

    def _take(self, literal: str) -> bool:
        end = self.i + len(literal)
        if self.text[self.i : end] == literal:
            after = self.text[end : end + 1]
            if after and after.isalnum():
                return False
            self.i = end
            return True
        return False
