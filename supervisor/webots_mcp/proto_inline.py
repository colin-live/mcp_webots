"""Expand a PROTO instance into the plain nodes of its body (text only, no Webots API).

Why: the Supervisor cannot see DEF names that live inside a PROTO body
(``getFromDef`` only knows DEFs of the world file), so a robot added as
``DEF ARM Arm2Dof {}`` hides its links, motors and TCP.  Writing the body into
the world instead turns every inner ``DEF`` into an ordinary, visible DEF.

Supported: PROTOs whose body is a single node, ``field IS name`` references and
plain field defaults.  Not supported (reported as ``InlineError``): template
code (``%< ... >%``) and bodies that are not a single node.
"""
import os
import re

KEYWORDS = ("field", "unconnectedField", "hiddenField", "deprecatedField", "vrmlField")


class InlineError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def match_close(text, open_idx):
    """Index of the bracket closing text[open_idx] (strings and # comments skipped)."""
    pairs = {"{": "}", "[": "]"}
    stack = [pairs[text[open_idx]]]
    i, in_str = open_idx + 1, False
    while i < len(text):
        c = text[i]
        if in_str:
            if c == "\\":
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "#":
            nl = text.find("\n", i)
            i = len(text) if nl < 0 else nl
            continue
        elif c in pairs:
            stack.append(pairs[c])
        elif c in "}]":
            if c != stack[-1]:
                raise InlineError("InlineUnsupported", "unbalanced brackets in the PROTO file")
            stack.pop()
            if not stack:
                return i
        i += 1
    raise InlineError("InlineUnsupported", "unbalanced brackets in the PROTO file")


def _strip_comments(text):
    out, in_str, i = [], False, 0
    while i < len(text):
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < len(text):
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif c == "#":
            nl = text.find("\n", i)
            i = len(text) if nl < 0 else nl
            continue
        else:
            out.append(c)
        i += 1
    return "".join(out)


def parse_proto(text):
    """-> dict(name, fields{name: default text}, body, externprotos[urls])."""
    clean = _strip_comments(text)
    if "%<" in clean:
        raise InlineError(
            "InlineUnsupported",
            "This PROTO uses template code (%< ... >%) which cannot be expanded as text.",
        )
    m = re.search(r"(?m)^\s*PROTO\s+([A-Za-z_]\w*)\s*\[", clean)
    if not m:
        raise InlineError("InlineUnsupported", "No `PROTO Name [ ... ]` declaration found.")
    name = m.group(1)
    h_open = m.end() - 1
    h_close = match_close(clean, h_open)
    header = clean[h_open + 1:h_close]

    brace = clean.find("{", h_close)
    if brace < 0 or clean[h_close + 1:brace].strip():
        raise InlineError("InlineUnsupported", "Cannot find the PROTO body.")
    b_close = match_close(clean, brace)
    body = clean[brace + 1:b_close].strip("\n")
    if not re.match(r"\s*(?:DEF\s+\w+\s+)?[A-Z]\w*\s*\{", body):
        raise InlineError("InlineUnsupported", "The PROTO body must be a single node.")
    node_end = match_close(body, body.index("{", re.match(r"\s*(?:DEF\s+\w+\s+)?[A-Z]\w*\s*", body).end() - 1))
    if body[node_end + 1:].strip():
        raise InlineError("InlineUnsupported", "The PROTO body must be a single node.")

    externs = re.findall(r'(?m)^\s*EXTERNPROTO\s+"([^"]+)"', clean[:m.start()])
    return {"name": name, "fields": _parse_header(header), "body": body, "externprotos": externs}


def _parse_header(header):
    marks, depth, in_str, i = [], 0, False, 0
    while i < len(header):
        c = header[i]
        if in_str:
            if c == "\\":
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
        elif depth == 0 and (i == 0 or not (header[i - 1].isalnum() or header[i - 1] == "_")):
            for kw in KEYWORDS:
                if header.startswith(kw, i) and not (header[i + len(kw):i + len(kw) + 1].isalnum()):
                    marks.append((i, kw))
                    i += len(kw) - 1
                    break
        i += 1
    fields = {}
    for n, (start, kw) in enumerate(marks):
        end = marks[n + 1][0] if n + 1 < len(marks) else len(header)
        entry = header[start + len(kw):end].strip()
        m = re.match(r"([A-Za-z0-9]+)\s*", entry)
        if not m:
            continue
        rest = entry[m.end():]
        if rest.startswith("{"):  # restriction list: SFString{"a", "b"}
            rest = rest[match_close(rest, 0) + 1:].lstrip()
        m2 = re.match(r"([A-Za-z_]\w*)\s*(.*)$", rest, re.S)
        if not m2:
            continue
        if kw != "unconnectedField":
            fields[m2.group(1)] = m2.group(2).strip()
    return fields


def expand_body(proto, extra_defs_check=None):
    """Replace every `field IS name` in the body with the field's default."""
    fields = proto["fields"]
    missing = []

    def sub(m):
        key = m.group(2)
        if key not in fields:
            missing.append(key)
            return m.group(0)
        return f"{m.group(1)} {fields[key]}"

    body = re.sub(r"\b([A-Za-z_]\w*)\s+IS\s+([A-Za-z_]\w*)", sub, proto["body"])
    if missing:
        raise InlineError("InlineUnsupported", f"The PROTO body references unknown fields: {sorted(set(missing))}")
    if re.search(r"\bIS\b", _strip_strings(body)):
        raise InlineError("InlineUnsupported", "The PROTO body uses `IS` in a form that cannot be expanded.")
    return body


def _strip_strings(text):
    return re.sub(r'"(?:\\.|[^"\\])*"', '""', text)


def inner_defs(body):
    return re.findall(r"\bDEF\s+([A-Za-z_]\w*)", _strip_strings(body))


def _split_top_level(node_inner):
    """Split a node's field list into [(name, text)]; fields start on their own line."""
    segments, current, depth, in_str = [], None, 0, False
    for line in node_inner.split("\n"):
        starts = depth == 0 and not in_str and re.match(r"\s*([a-z]\w*)\b", line)
        if starts:
            current = [starts.group(1), line]
            segments.append(current)
        elif current is not None:
            current[1] += "\n" + line
        for c in line:
            if in_str:
                if c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c in "[{":
                depth += 1
            elif c in "]}":
                depth -= 1
    return [(n, t) for n, t in segments]


def build_node(body, def_name, overrides):
    """DEF def_name Type { <body fields with overrides applied> }."""
    head = re.match(r"\s*(?:DEF\s+\w+\s+)?([A-Z]\w*)\s*", body)
    node_type = head.group(1)
    open_idx = body.index("{", head.end() - 1)
    close_idx = match_close(body, open_idx)
    segments = _split_top_level(body[open_idx + 1:close_idx])
    names = [n for n, _ in segments]
    for key, value in overrides.items():
        line = f"  {key} {value}"
        if key in names:
            segments[names.index(key)] = (key, line)
        else:
            segments.append((key, line))
            names.append(key)
    inner = "\n".join(t.rstrip() for _, t in segments)
    return f"DEF {def_name} {node_type} {{\n{inner}\n}}\n"


def rebase_externprotos(urls, proto_dir, worlds_dir):
    """EXTERNPROTO urls from the PROTO file, valid from the world file's directory."""
    out = []
    for url in urls:
        if re.match(r"^(webots://|https?://)", url):
            out.append(url)
        else:
            out.append(os.path.relpath(os.path.join(proto_dir, url), worlds_dir).replace("\\", "/"))
    return out
