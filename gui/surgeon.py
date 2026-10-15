"""Comment-preserving editors for the repo's configuration files.

vars/main.yml is ~1,300 lines in which the comments ARE the documentation.
Loading it with a YAML library and dumping it back would destroy all of that,
so the GUI never does. Instead:

  * YamlSurgeon edits ONE top-level key at a time by rewriting only the lines
    that hold that key's value. Everything else - comments, blank lines, key
    order, quoting style of untouched keys - is left byte-for-byte alone.
  * IniSurgeon does the same for ansible.cfg (key = value inside [sections]).

Every edit is verified: the new text is parsed again and must equal the old
parse with exactly the requested keys changed. If it does not, the edit is
rejected (SurgeryError) and nothing is written.
"""

import configparser
import json
import re

import yaml

KEY_RE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_\-]*)\s*:(?P<rest>.*)$")


class SurgeryError(Exception):
    """Raised when an edit cannot be applied safely."""


# ---------------------------------------------------------------------------
# YAML scalar / block rendering
# ---------------------------------------------------------------------------
def _split_inline_comment(rest):
    """Split 'value  # comment' -> ('value', '  # comment'), quote-aware."""
    in_s = in_d = False
    prev = " "
    for i, ch in enumerate(rest):
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s and prev != "\\":
            in_d = not in_d
        elif ch == "#" and not in_s and not in_d and prev in " \t":
            j = i
            while j > 0 and rest[j - 1] in " \t":
                j -= 1
            return rest[:j], rest[j:]
        prev = ch
    return rest, ""


def render_scalar(value):
    """Render a Python scalar as a single-line YAML value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        text = repr(value)
        return text if any(c in text for c in ".eE") else text + ".0"
    if isinstance(value, str):
        # JSON strings are valid YAML double-quoted scalars and handle every
        # escape correctly. Always quoting avoids YAML's implicit typing
        # ("yes" -> bool, "0755" -> octal int, "1e3" -> float, ...).
        return json.dumps(value, ensure_ascii=False)
    raise SurgeryError("not a scalar: %r" % (value,))


class _IndentedDumper(yaml.SafeDumper):
    """SafeDumper that indents nested sequences (matches the repo's style)."""

    def increase_indent(self, flow=False, indentless=False):
        return super(_IndentedDumper, self).increase_indent(flow, False)


def render_value_lines(key, value, indent="  "):
    """Return the lines that represent `key: value` at the top level."""
    if isinstance(value, (list, dict)):
        if not value:
            return ["%s: %s" % (key, "[]" if isinstance(value, list) else "{}")]
        dumped = yaml.dump(value, Dumper=_IndentedDumper, default_flow_style=False,
                           sort_keys=False, allow_unicode=True, width=1000, indent=2)
        body = [indent + line if line else line for line in dumped.rstrip("\n").split("\n")]
        return ["%s:" % key] + body
    return ["%s: %s" % (key, render_scalar(value))]


# ---------------------------------------------------------------------------
# YAML surgeon
# ---------------------------------------------------------------------------
class YamlSurgeon(object):
    """Edit top-level keys of a YAML mapping document without reformatting it."""

    def __init__(self, text):
        self.lines = text.split("\n")
        self._trailing_newline = text.endswith("\n")
        if self._trailing_newline:
            self.lines.pop()
        self.original = self._parse_text(text)

    # -- parsing helpers ----------------------------------------------------
    @staticmethod
    def _parse_text(text):
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise SurgeryError("file is not valid YAML: %s" % exc)
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise SurgeryError("top level of the file is not a mapping")
        return data

    def text(self):
        return "\n".join(self.lines) + ("\n" if self._trailing_newline else "")

    def _is_continuation(self, line):
        """True if `line` belongs to the value of the key above it."""
        if not line.strip():
            return None            # blank: undecided, depends on what follows
        if line[0] in " \t":
            return True            # indented content or indented comment
        if line.startswith("- ") or line == "-":
            return True            # top-level sequence written at column 0
        return False               # column-0 key, comment or document marker

    def find_key(self, key):
        """Return (start, end_exclusive) of the key's lines, or None.

        The range covers the key line and the CONTENT lines of its value. It
        stops after the last non-comment line, so trailing indented example
        comments (``#   - nvme1n1``) stay where they are.
        """
        start = None
        for i, line in enumerate(self.lines):
            m = KEY_RE.match(line)
            if m and m.group("key") == key:
                if start is not None:
                    raise SurgeryError("key %r appears more than once" % key)
                start = i
        if start is None:
            return None
        last_content = start
        i = start + 1
        while i < len(self.lines):
            cont = self._is_continuation(self.lines[i])
            if cont is False:
                break
            if cont and not self.lines[i].lstrip().startswith("#"):
                last_content = i
            i += 1
        return start, last_content + 1

    def find_commented(self, key):
        """Locate a commented-out example such as ``# expected_nvme_count: 9``.

        Returns the index AFTER the contiguous comment run that starts there
        (so a multi-line example block stays intact above the new key).
        """
        pat = re.compile(r"^#\s?%s\s*:" % re.escape(key))
        for i, line in enumerate(self.lines):
            if pat.match(line):
                j = i + 1
                while j < len(self.lines) and self.lines[j].startswith("#") \
                        and not re.match(r"^#\s?[A-Za-z_][A-Za-z0-9_]*\s*:", self.lines[j]):
                    j += 1
                return j
        return None

    def top_level_keys(self):
        return [KEY_RE.match(l).group("key") for l in self.lines if KEY_RE.match(l)]

    # -- edits --------------------------------------------------------------
    def set(self, key, value, neighbors=None):
        """Set `key` to `value`, inserting it if it does not exist yet.

        `neighbors` is an ordered list of related keys; a brand-new key is
        placed after the closest preceding neighbor that exists in the file.
        """
        new_lines = render_value_lines(key, value)
        span = self.find_key(key)
        if span:
            start, end = span
            old_first = self.lines[start]
            if len(new_lines) == 1 and end - start == 1:
                # Scalar -> scalar on one line: keep the inline comment.
                rest = KEY_RE.match(old_first).group("rest")
                _, comment = _split_inline_comment(rest)
                new_lines[0] += comment
            self.lines[start:end] = new_lines
            return "replaced"

        anchor = self.find_commented(key)
        if anchor is None and neighbors:
            try:
                idx = neighbors.index(key)
            except ValueError:
                idx = len(neighbors)
            for nb in reversed(neighbors[:idx]):
                nb_span = self.find_key(nb)
                if nb_span:
                    anchor = nb_span[1]
                    # keep the neighbor's trailing example comments together
                    while anchor < len(self.lines) and self.lines[anchor].startswith((" ", "\t")) \
                            and self.lines[anchor].lstrip().startswith("#"):
                        anchor += 1
                    break
            if anchor is None:
                for nb in neighbors[idx + 1:]:
                    nb_span = self.find_key(nb)
                    if nb_span:
                        anchor = nb_span[0]
                        break
        if anchor is None:
            while self.lines and not self.lines[-1].strip():
                self.lines.pop()
            self.lines += ["", "# Added by the Tier 0 configuration GUI"]
            anchor = len(self.lines)
        self.lines[anchor:anchor] = new_lines
        return "inserted"

    def unset(self, key):
        """Comment the key out (keeps it visible as an example)."""
        span = self.find_key(key)
        if not span:
            return "absent"
        start, end = span
        self.lines[start:end] = ["# " + l if l.strip() else l for l in self.lines[start:end]]
        return "commented"

    # -- verification -------------------------------------------------------
    def verify(self, expected_changes):
        """Re-parse and check that ONLY the expected keys changed.

        `expected_changes` maps key -> new value, or key -> _UNSET to require
        the key to be absent.
        """
        new = self._parse_text(self.text())
        old = self.original
        for key in set(old) | set(new):
            if key in expected_changes:
                want = expected_changes[key]
                if want is UNSET:
                    if key in new:
                        raise SurgeryError("verification failed: %r still set" % key)
                elif new.get(key, UNSET) != want:
                    raise SurgeryError("verification failed: %r parsed back as %r, expected %r"
                                       % (key, new.get(key), want))
            elif old.get(key, UNSET) != new.get(key, UNSET):
                raise SurgeryError("verification failed: unrelated key %r changed" % key)
        return new


class _Unset(object):
    def __repr__(self):
        return "UNSET"


UNSET = _Unset()


def apply_yaml_changes(text, changes, neighbors_by_key=None):
    """Apply {key: value|UNSET} to YAML text; return (new_text, actions)."""
    surgeon = YamlSurgeon(text)
    actions = {}
    for key, value in changes.items():
        if value is UNSET:
            actions[key] = surgeon.unset(key)
        else:
            nb = (neighbors_by_key or {}).get(key)
            actions[key] = surgeon.set(key, value, nb)
    surgeon.verify(changes)
    return surgeon.text(), actions


# ---------------------------------------------------------------------------
# INI surgeon (ansible.cfg)
# ---------------------------------------------------------------------------
SECTION_HDR = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*$")


class IniSurgeon(object):
    def __init__(self, text):
        self.lines = text.split("\n")
        self._trailing_newline = text.endswith("\n")
        if self._trailing_newline:
            self.lines.pop()
        self.original = self._parse(text)

    @staticmethod
    def _parse(text):
        cp = configparser.RawConfigParser(strict=False, interpolation=None)
        cp.optionxform = str
        try:
            cp.read_string(text)
        except configparser.Error as exc:
            raise SurgeryError("not valid INI: %s" % exc)
        return {s: dict(cp.items(s)) for s in cp.sections()}

    def text(self):
        return "\n".join(self.lines) + ("\n" if self._trailing_newline else "")

    def _section_range(self, section):
        start = None
        for i, line in enumerate(self.lines):
            m = SECTION_HDR.match(line)
            if m:
                if start is not None:
                    return start, i
                if m.group("name").strip() == section:
                    start = i
        if start is None:
            return None
        return start, len(self.lines)

    def set(self, section, key, value):
        value = str(value)
        rng = self._section_range(section)
        if rng is None:
            while self.lines and not self.lines[-1].strip():
                self.lines.pop()
            self.lines += ["", "[%s]" % section, "%s = %s" % (key, value)]
            return "section-added"
        start, end = rng
        active = re.compile(r"^%s\s*=" % re.escape(key))
        commented = re.compile(r"^[#;]\s*%s\s*=" % re.escape(key))
        for i in range(start + 1, end):
            if active.match(self.lines[i]):
                self.lines[i] = "%s = %s" % (key, value)
                return "replaced"
        for i in range(start + 1, end):
            if commented.match(self.lines[i]):
                self.lines.insert(i + 1, "%s = %s" % (key, value))
                return "inserted"
        insert_at = end
        while insert_at > start + 1 and not self.lines[insert_at - 1].strip():
            insert_at -= 1
        self.lines.insert(insert_at, "%s = %s" % (key, value))
        return "inserted"

    def unset(self, section, key):
        rng = self._section_range(section)
        if rng is None:
            return "absent"
        active = re.compile(r"^%s\s*=" % re.escape(key))
        for i in range(rng[0] + 1, rng[1]):
            if active.match(self.lines[i]):
                self.lines[i] = "# " + self.lines[i]
                return "commented"
        return "absent"

    def verify(self, changes):
        new = self._parse(self.text())
        for section in set(self.original) | set(new):
            o, n = self.original.get(section, {}), new.get(section, {})
            for key in set(o) | set(n):
                want = changes.get((section, key), None)
                if (section, key) in changes:
                    if want is UNSET:
                        if key in n:
                            raise SurgeryError("verification failed: [%s] %s still set" % (section, key))
                    elif n.get(key) != str(want):
                        raise SurgeryError("verification failed: [%s] %s = %r" % (section, key, n.get(key)))
                elif o.get(key) != n.get(key):
                    raise SurgeryError("verification failed: unrelated [%s] %s changed" % (section, key))
        return new


def apply_ini_changes(text, changes):
    """changes: {(section, key): value|UNSET}."""
    s = IniSurgeon(text)
    actions = {}
    for (section, key), value in changes.items():
        actions["%s.%s" % (section, key)] = (s.unset(section, key) if value is UNSET
                                             else s.set(section, key, value))
    s.verify(changes)
    return s.text(), actions
