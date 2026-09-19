"""Bounded, data-only Lua reader. No Lua VM, eval, imports, or file execution.

Accepts literal tables, finite numbers, strings, booleans and nil. Terrain reads
may additionally preserve whitelisted constant names and _(literal) translation
wrappers as data. Executable expressions, function calls and duplicate keys fail.
"""
from __future__ import annotations

import re
import math


class LuaDataError(ValueError):
    pass


_TOKEN = re.compile(
    r"\s+|--\[(=*)\[.*?\]\1\]|--[^\r\n]*|"
    r"(?P<long>\[(?P<eq>=*)\[.*?\](?P=eq)\])|"
    r"(?P<string>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')|"
    r"(?P<number>(?:0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?))|"
    r"(?P<name>[A-Za-z_][A-Za-z_0-9]*)|(?P<punct>[{}\[\]=,;()+-])",
    re.S,
)


def _decode_string(token: str) -> str:
    result = []
    i = 1
    escapes = {"n": "\n", "r": "\r", "t": "\t", "a": "\a", "b": "\b", "f": "\f", "v": "\v"}
    while i < len(token) - 1:
        c = token[i]
        if c != "\\":
            result.append(c)
            i += 1
            continue
        i += 1
        c = token[i]
        if c.isdigit():
            end = i + 1
            while end < min(i + 3, len(token) - 1) and token[end].isdigit():
                end += 1
            code = int(token[i:end])
            if code > 255:
                raise LuaDataError("Invalid Lua decimal string escape")
            result.append(chr(code))
            i = end
        elif c == "z":
            i += 1
            while i < len(token) - 1 and token[i].isspace():
                i += 1
        else:
            result.append(escapes.get(c, c))
            i += 1
    return "".join(result)


class LuaDataParser:
    def __init__(self, text: str, *, constants: bool = False, max_bytes: int = 16_000_000,
                 max_tokens: int = 1_000_000, max_depth: int = 80):
        if len(text.encode("utf-8")) > max_bytes:
            raise LuaDataError("Lua data exceeds byte limit")
        self.tokens = []
        pos = 0
        while pos < len(text):
            match = _TOKEN.match(text, pos)
            if not match:
                raise LuaDataError(f"Executable or invalid Lua token at offset {pos}")
            raw = match.group()
            pos = match.end()
            if raw.isspace() or raw.startswith("--"):
                continue
            self.tokens.append((match.lastgroup, raw))
            if len(self.tokens) > max_tokens:
                raise LuaDataError("Lua token limit exceeded")
        self.i = 0
        self.constants = constants
        self.max_depth = max_depth

    def peek(self, n: int = 0) -> str | None:
        return self.tokens[self.i + n][1] if self.i + n < len(self.tokens) else None

    def take(self, expected=None):
        if self.i >= len(self.tokens):
            raise LuaDataError("Unexpected end of Lua data")
        kind, raw = self.tokens[self.i]
        if expected is not None and raw != expected:
            raise LuaDataError(f"Expected {expected!r}, got {raw!r}")
        self.i += 1
        return kind, raw

    def value(self, depth=0):
        if depth > self.max_depth:
            raise LuaDataError("Lua nesting limit exceeded")
        kind, raw = self.take()
        if raw == "{":
            result = {}
            index = 1
            while self.peek() != "}":
                if self.peek() is None:
                    raise LuaDataError("Unclosed Lua table")
                if self.peek() == "[":
                    self.take("[")
                    key = self.value(depth + 1)
                    self.take("]")
                    self.take("=")
                elif self.peek(1) == "=":
                    key = self.take()[1]
                    self.take("=")
                else:
                    key = index
                    index += 1
                if not isinstance(key, (str, int, float)) or isinstance(key, bool):
                    raise LuaDataError("Invalid table key")
                if key in result:
                    raise LuaDataError(f"Duplicate Lua key {key!r}")
                result[key] = self.value(depth + 1)
                if self.peek() in (",", ";"):
                    self.take()
                elif self.peek() != "}":
                    raise LuaDataError("Expected table separator")
            self.take("}")
            return result
        if raw in ("+", "-"):
            value = self.value(depth + 1)
            if not isinstance(value, (float, int)) or isinstance(value, bool):
                raise LuaDataError("Unary sign must precede a number")
            return value if raw == "+" else -value
        if kind == "number":
            number = int(raw, 16) if raw.lower().startswith("0x") else float(raw)
            if not math.isfinite(number):
                raise LuaDataError("Nonfinite Lua number")
            return int(number) if number == int(number) else number
        if kind == "string":
            return _decode_string(raw)
        if kind in ("long", "eq") or raw.startswith("[[") or re.match(r"\[=+\[", raw):
            opening = re.match(r"\[(=*)\[", raw).group()
            value = raw[len(opening):-len(opening)]
            return value[1:] if value.startswith("\n") else value
        if raw in ("true", "false", "nil"):
            return {"true": True, "false": False, "nil": None}[raw]
        if self.constants and raw == "_":
            self.take("(")
            value = self.value(depth + 1)
            self.take(")")
            if not isinstance(value, str):
                raise LuaDataError("Translation wrapper requires a literal string")
            return value
        if self.constants and re.fullmatch(r"(?:BEACON_TYPE_[A-Z_0-9]+|MODULATIONTYPE_[A-Z]+|VHF_HI|VHF_LOW|UHF|HF)", raw):
            return raw
        raise LuaDataError(f"Unsupported Lua expression {raw!r}")


def parse_assignment(text: str, name: str = "mission") -> dict:
    parser = LuaDataParser(text.lstrip("\ufeff"))
    parser.take(name)
    parser.take("=")
    value = parser.value()
    if parser.peek() == ";":
        parser.take()
    if parser.peek() is not None or not isinstance(value, dict):
        raise LuaDataError("Only one literal table assignment is permitted")
    return value


def extract_table(text: str, name: str, *, constants: bool = True) -> dict:
    """Extract a literal terrain table; ignore but NEVER run shipped preambles."""
    match = re.search(r"(?m)^\s*" + re.escape(name) + r"\s*=\s*(\{)", text)
    if not match:
        raise LuaDataError(f"No literal {name} table found")
    # Files in this schema finish at the table. Reject unexpected trailing code.
    parser = LuaDataParser(text[match.start(1):], constants=constants)
    result = parser.value()
    if parser.peek() == ";":
        parser.take()
    if parser.peek() is not None:
        raise LuaDataError(f"Unexpected data after {name} table")
    return result


def array(value) -> list:
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        return []
    return [value[k] for k in sorted(k for k in value if isinstance(k, int))]
