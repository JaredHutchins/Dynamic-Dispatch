"""Plane's description HTML as plain text.

Plane sends a story's description as editor HTML, with a class and a data-id on
nearly every element. The agent gets the text the way it was written instead:
paragraphs, line breaks, lists, checklists and code blocks keep their shape,
and every tag and attribute is dropped.
"""

import re
from html.parser import HTMLParser

PARAGRAPHS = {"p", "div", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"}
HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


def html_to_text(html):
    """Readable plain text for this HTML. Empty HTML gives an empty string."""
    parser = _Parser()
    parser.feed(html or "")
    parser.close()
    return parser.text()


class _Parser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines = []
        self.buf = []
        # One entry per open list: its kind, the next number, where its markers
        # go, and where the text of its current item goes.
        self.lists = []
        self.marker = None
        self.pre = 0
        self.gap = False

    def text(self):
        while self.lines and not self.lines[-1]:
            self.lines.pop()
        return "\n".join(self.lines)

    def close(self):
        super().close()
        self._flush()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "br":
            if self.pre:
                self.buf.append("\n")
            elif not self._flush() and self.lines and self.lines[-1]:
                self.lines.append("")
        elif tag == "pre":
            self._block()
            self.pre += 1
        elif tag in PARAGRAPHS:
            self._block()
            if tag in HEADINGS:
                self.buf.append("#" * HEADINGS[tag] + " ")
        elif tag in ("ul", "ol"):
            self._flush()
            if not self.lists:
                self.gap = True
            kind = "task" if attrs.get("data-type") == "taskList" else tag
            indent = self.lists[-1]["item"] if self.lists else ""
            self.lists.append({"kind": kind, "n": _start(attrs), "indent": indent, "item": indent})
        elif tag == "li":
            self._flush()
            if not self.lists:
                self.lists.append({"kind": "ul", "n": 1, "indent": "", "item": ""})
            current = self.lists[-1]
            if "data-checked" in attrs or current["kind"] == "task":
                marker = "- [x] " if attrs.get("data-checked") == "true" else "- [ ] "
            elif current["kind"] == "ol":
                marker = f"{current['n']}. "
                current["n"] += 1
            else:
                marker = "- "
            self.marker = current["indent"] + marker
            current["item"] = current["indent"] + " " * len(marker)

    def handle_endtag(self, tag):
        if tag == "pre":
            self._flush()
            self.pre = max(self.pre - 1, 0)
            if not self.lists:
                self.gap = True
        elif tag in PARAGRAPHS:
            self._flush()
            if not self.lists:
                self.gap = True
        elif tag in ("ul", "ol"):
            self._flush()
            if self.lists:
                self.lists.pop()
            if not self.lists:
                self.gap = True
        elif tag == "li":
            self._flush()
            if self.marker:
                # An empty item still shows, so an unticked box isn't lost.
                self._emit("")

    def handle_data(self, data):
        self.buf.append(data)

    def _block(self):
        self._flush()
        # Plane wraps every list item's text in a paragraph; only top-level
        # paragraphs are spaced apart.
        if not self.lists:
            self.gap = True

    def _flush(self):
        """Ends the current line. Returns whether anything was written."""
        raw = "".join(self.buf)
        self.buf = []
        if self.pre:
            # A newline straight after <pre> isn't part of the code.
            if raw.startswith("\n"):
                raw = raw[1:]
            raw = raw.rstrip("\n")
            if not raw:
                return False
            for line in raw.split("\n"):
                self._emit(line, exact=True)
            return True
        text = re.sub(r"\s+", " ", raw).strip()
        if not text:
            return False
        self._emit(text)
        return True

    def _emit(self, text, exact=False):
        if self.gap and self.lines and self.lines[-1]:
            self.lines.append("")
        self.gap = False
        if self.marker is not None:
            prefix, self.marker = self.marker, None
        else:
            prefix = self.lists[-1]["item"] if self.lists else ""
        line = prefix + text
        self.lines.append(line if exact and text else line.rstrip())


def _start(attrs):
    try:
        return int(attrs.get("start") or 1)
    except ValueError:
        return 1
