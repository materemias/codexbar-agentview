"""omp-stretch markup in an agent's reply, drawn as its short forms.

omp-stretch (https://github.com/materemias/omp-stretch) has the model write
`{{short|long}}`: the reader sees `short` and can open `long` in its place. A
preview has nothing to open, so `short_forms` draws every stretch the way
omp-stretch draws a closed one. This is a port of `parse` in omp-stretch's
stretch-core.ts, cut to what a closed view needs; keep it in step with it:

- Code spans and fenced blocks are copied as written, so braces in code never
  open a stretch.
- A stretch that has a one-line short form with a word in it shows that short
  form. A short form of only punctuation or nothing (`ago{{.|…}}`) takes on the
  words of its sentence before it; with none to take on, the stretch is drawn
  open. A multi-line short form is drawn open too.
- A copy of the short form written right before its stretch
  (`**Gist.** {{Gist.|…}}`) shows once.
- An empty long form leaves its short form as plain text.
- A sentence written without its short form (`Done. {{It took a minute.}}`)
  is a long form: it opens from the words before it, which then show alone.
  Placeholders such as `{{name}}` stay as written.
- A stretch cut off before its closing braces, as in an aborted reply, is
  drawn open with the words its short form took on, as omp-stretch draws a
  finished reply; with no long form yet it stays as written.

The long form's own additions (`keepShort`, `prependLead`) apply only to a
closed stretch, whose short form shows and hides them, so they are left out;
a cut-off stretch takes its borrowed words back, as `parse` does.
"""
from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field

# Letters and digits: Python's `\w` without `_`, JavaScript's [\p{L}\p{N}].
_ALNUM = r"[^\W_]"

_WORDLESS = re.compile(r"[ \t.!?,;:…]*")
_BARE_LONG_START = re.compile(r"[^\W_]|[(\"'“‘*_`]")
_BARE_LONG_END = re.compile(r"[.!?…][)\"'”’*_]*\Z")
_MARK = re.compile(r"[-*+>]|[0-9]+[.)]|#+")
# JavaScript's `.`, which stops at \r and the Unicode line separators as well as \n.
_DOT = r"[^\n\r\u2028\u2029]"
_EMPHASIS = re.compile(rf"(\*\*|__|~~|\*|_)(?=\S)({_DOT}*?\S)\1")
_UNPAIRED_REPEAT = re.compile(rf"[*~]|(?<!{_ALNUM})_|_(?!{_ALNUM})")
_UNPAIRED_LEAD = re.compile(rf"\*|~~|(?<!{_ALNUM})_|_(?!{_ALNUM})")
_ENDS_SENTENCE = re.compile(r"[.!?:;][*_~)\"'”’]*\Z")
_STOPS_LEAD = re.compile(r"[`\[\]<>|]")
_FENCE_OPEN = re.compile(r"[ \t]*(`{3,}|~{3,})")
_FENCE_CLOSE = re.compile(rf"([ \t]*)(`+|~+)(?:(\}}\}}){_DOT}*|[ \t]*)")
_FENCE_LINE = re.compile(r"[ \t]*(`{3,}|~{3,})[ \t]*")
_LINK = re.compile(
    r"!?\[([^\]\n]+)\]\((?:(?:[^()\s]|\([^()\s]*\))+"
    r"(?:[ \t]+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^()\n]*\)))?|[^)\s]+)\)"
)
_NARROW_LINK = re.compile(r"!?\[([^\]\n]+)\]\([^)\s]+\)")
_CODE_SPAN = re.compile(r"(`+)(?!`)[\s\S]*?(?<!`)\1(?!`)")
_TICKS = re.compile(r"`+")
# Where plain text may stop being plain: a code span, braces, a bar or a new line.
_PLAIN_STOP = re.compile(r"[`{}|\n]")
_BLOCK_START = re.compile(r"(?:[-*+>][ \t]|\d+[.)][ \t]|#{1,6}[ \t]|`{3,}|~{3,})")


@dataclass(eq=False)
class _Stretch:
    short: str = ""
    # None while no `|` was written: then there is nothing to open.
    long: list | None = None
    is_closed: bool = False


@dataclass(eq=False)
class _Frame:
    stretch: _Stretch
    # The words before a wordless short form that it took on, and the short form as written.
    lead: tuple[str, str] | None = None
    parent: _Frame | None = field(default=None, repr=False)


def short_forms(text: str) -> str:
    """`text` with every omp-stretch stretch drawn as its short form."""
    if "{{" not in text:
        return text
    return _draw(_parse(text))


def reply_preview(text: str) -> str:
    """A reply as a preview shows it: short forms where omp-stretch is installed,
    since only there is `{{…|…}}` its markup. Elsewhere it may be a template
    (`${{ github.ref }}`, `{{ .Values.x | quote }}`) and stays as written."""
    if "{{" not in text or not _omp_stretch_installed():
        return text
    return short_forms(text)


@functools.cache
def _omp_stretch_installed() -> bool:
    import codexbar_companions  # Imported here, so only a reply with braces pays for it.
    return codexbar_companions.omp_stretch_installed()


def _draw(parts: list) -> str:
    """Each stretch as omp-stretch draws it closed in a finished reply: a stretch the
    reader can open shows its short form, any other is drawn open."""
    out = []
    for part in parts:
        if isinstance(part, str):
            out.append(part)
        elif _is_linkable(part):
            out.append(part.short)
        elif part.long is None:
            out.append("{{" + part.short + ("}}" if part.is_closed else ""))
        elif not part.long:
            out.append(part.short)
        else:
            out.append(_end_line(_draw(part.long)))
    return "".join(out)


def _is_linkable(stretch: _Stretch) -> bool:
    return (stretch.is_closed and stretch.long is not None
            and "\n" not in stretch.short and not _is_wordless(stretch.short))


def _is_wordless(short: str) -> bool:
    return _WORDLESS.fullmatch(short) is not None


def _is_bare_long(text: str) -> bool:
    return (_BARE_LONG_START.match(text) is not None and _BARE_LONG_END.search(text) is not None
            and len(re.split(r"\s+", text)) >= 3)


def _code_span_end(text: str, i: int) -> int:
    """Past the next run of exactly as many backticks; without one the backticks are text."""
    run = _TICKS.match(text, i)
    ticks = run.group() if run else "`"
    closing = re.compile(rf"(?<!`){ticks}(?!`)").search(text, i + len(ticks))
    return i + len(ticks) if closing is None else closing.end()


def _fence_end(text: str, start: int) -> int | None:
    """Where a fenced code block opening at `start` ends, or None when none opens there."""
    def line_end(at: int) -> int:
        end = text.find("\n", at)
        return len(text) if end == -1 else end

    opening = _FENCE_OPEN.match(text, start, line_end(start))
    if opening is None:
        return None
    fence = opening.group(1)
    at = line_end(start) + 1
    while at < len(text):
        close = _FENCE_CLOSE.fullmatch(text[at:line_end(at)])
        if close and len(close.group(2)) >= len(fence) and close.group(2)[0] == fence[0]:
            # A long form may close right after its fence: those braces stay outside the code.
            if close.group(3):
                return at + len(close.group(1)) + len(close.group(2))
            return min(line_end(at) + 1, len(text))
        at = line_end(at) + 1
    return len(text)


def _parse(text: str) -> list:
    root: list = []
    stack: list[_Frame] = []

    def write(chunk: str) -> None:
        frame = stack[-1] if stack else None
        if frame is not None and frame.stretch.long is None:
            frame.stretch.short += chunk
            return
        segments = frame.stretch.long if frame is not None else root
        if segments and isinstance(segments[-1], str):
            segments[-1] += chunk
        else:
            segments.append(chunk)

    i = 0
    while i < len(text):
        frame = stack[-1] if stack else None
        in_short = frame is not None and frame.stretch.long is None

        if i == 0 or text[i - 1] == "\n":
            fence_stop = _fence_end(text, i)
            if fence_stop is not None:
                write(text[i:fence_stop])
                i = fence_stop
                continue
        if text[i] == "`":
            stop = _code_span_end(text, i)
            write(text[i:stop])
            i = stop
            continue
        if text.startswith("{{", i) and not in_short:
            stretch = _Stretch()
            (frame.stretch.long if frame is not None else root).append(stretch)
            stack.append(_Frame(stretch, parent=frame))
            i += 2
            continue
        if in_short and text[i] == "|":
            _open_long(frame, root)
            i += 1
            continue
        if frame is not None and text.startswith("}}", i):
            if in_short and _is_bare_long(frame.stretch.short):
                written = frame.stretch.short
                frame.stretch.short = ""
                _open_long(frame, root)
                frame.stretch.long.append(written)
            stretch, lead = frame.stretch, frame.lead
            stack.pop()
            i += 2
            # An empty long form has nothing to open: its short form is plain text.
            if stretch.long is not None and all(
                    isinstance(part, str) and part.strip() == "" for part in stretch.long):
                (stack[-1].stretch.long if stack else root).pop()
                write(stretch.short if lead is None else lead[0] + lead[1])
                continue
            stretch.is_closed = True
            continue
        # A run of plain text goes in whole; a new line goes alone, so the next line can open a fence.
        if text[i] == "\n":
            stop = i + 1
        else:
            found = _PLAIN_STOP.search(text, i + 1)
            stop = len(text) if found is None else found.start()
        write(text[i:stop])
        i = stop
    # A reply cut off inside a stretch: its long form is drawn open, so it needs its borrowed words.
    for frame in stack:
        if frame.lead is not None and frame.stretch.long is not None:
            _prepend_lead(frame.stretch.long, frame.lead[0])
    return root


def _prepend_lead(long: list, lead: str) -> None:
    first = long[0] if long else None
    if not isinstance(first, str):
        long.insert(0, lead)
    elif _BLOCK_START.match(first.lstrip()):
        long[0] = f"{lead.rstrip()}\n\n{first.lstrip()}"
    else:
        long[0] = lead + first


def _open_long(frame: _Frame, root: list) -> None:
    """The `|`: the short form is complete and the long form starts."""
    container = frame.parent.stretch.long if frame.parent is not None else root
    written = frame.stretch.short
    lead = _take_lead_in(container, frame.stretch) if _is_wordless(written) else None
    if lead is None:
        _drop_repeat(container, written, frame.parent is None)
    else:
        frame.lead = (lead, written)
    frame.stretch.long = []


def _drop_repeat(segments: list, short: str, is_root: bool) -> None:
    """The gist written before its stretch as well (`**Gist.** {{Gist.|…}}`) shows once:
    the copy in front goes, when it starts its line or a sentence, holds no backtick and
    leaves no emphasis mark unpaired."""
    if len(segments) < 2 or not isinstance(segments[-2], str):
        return
    before = segments[-2]
    want = _words_of(short)
    if not want:
        return
    line_start = before.rfind("\n") + 1
    if line_start == 0 and not (is_root and len(segments) == 2):
        return
    words = list(re.finditer(r"\S+", before[line_start:]))
    left = len(want)
    k = len(words) - 1
    while k >= 0 and left > 0:
        word = _bare(words[k].group())
        if word != "":
            if word != want[left - 1]:
                return
            left -= 1
        k -= 1
    if left > 0:
        return
    cut = line_start + words[k + 1].start()
    if "`" in before[cut:]:
        return
    unpaired = _EMPHASIS.sub(r"\2", before[cut:])
    if _UNPAIRED_REPEAT.search(unpaired):
        return
    lead = before[line_start:cut].strip()
    if lead != "" and not _MARK.fullmatch(lead) and not re.search(r"[.!?:;]\Z", lead):
        return
    segments[-2] = before[:cut]


def _take_lead_in(segments: list, stretch: _Stretch) -> str | None:
    """A wordless short form (`ago{{.|…}}`, `Done. {{|…}}`) takes on the words before it,
    back to the start of their sentence on that line: they leave `segments` and become its
    short form. Returns the words taken, with the space after them, or None."""
    if len(segments) < 2 or not isinstance(segments[-2], str):
        return None
    before = segments[-2]
    line_start = before.rfind("\n") + 1
    words = list(re.finditer(r"\S+", before[line_start:]))
    k = len(words) - 1
    while k >= 0:
        word = words[k].group()
        if _STOPS_LEAD.search(word) or _MARK.fullmatch(word):
            break
        if k < len(words) - 1 and _ENDS_SENTENCE.search(word):
            break
        k -= 1
    # The lead-in starts on a word, so a comma or dash in front of it stays where it was.
    start = k + 1
    while start < len(words) and not re.search(_ALNUM, words[start].group()):
        start += 1
    if start == len(words):
        return None
    cut = line_start + words[start].start()
    lead = before[cut:]
    if _UNPAIRED_LEAD.search(_EMPHASIS.sub(r"\2", lead)):
        return None
    segments[-2] = before[:cut]
    taken = lead.rstrip()
    stretch.short = taken if _ENDS_SENTENCE.search(taken) else taken + stretch.short.strip()
    return lead


def _end_line(text: str) -> str:
    """A long form that closed on its fence's line gets a line break, so text after it is not on the fence line."""
    return text + "\n" if _FENCE_LINE.fullmatch(text[text.rfind("\n") + 1:]) else text


def _links_in(text: str) -> list[re.Match]:
    """Markdown links, each matched narrowly where it touches inline code."""
    code = [(span.start(), span.end()) for span in _CODE_SPAN.finditer(text)]
    links = []
    at = 0
    while (link := _LINK.search(text, at)) is not None:
        if any(link.start() < to and link.end() > start for start, to in code):
            narrow = _NARROW_LINK.match(text, link.start())
            if narrow is None:
                at = link.start() + 1
                continue
            link = narrow
        links.append(link)
        at = link.end()
    return links


def _words_of(short: str) -> list[str]:
    """The short form's words, bare; a link reads as its label."""
    text, at = "", 0
    for link in _links_in(short):
        text += short[at:link.start()] + link.group(1)
        at = link.end()
    return [word for word in map(_bare, re.split(r"\s+", text + short[at:])) if word != ""]


def _bare(word: str) -> str:
    """A word without surrounding punctuation or markdown marks, lowercased."""
    start, end = 0, len(word)
    while start < end and not word[start].isalnum():
        start += 1
    while end > start and not word[end - 1].isalnum():
        end -= 1
    return word[start:end].lower()
