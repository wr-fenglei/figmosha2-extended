"""Runtime for the commands generated from figma-console-mcp.

figmosha_ext/commands.json is written by tools/convert_console_mcp.py. Each
entry carries a Plugin API JS template (a ported figma-desktop-bridge handler)
and a parameter spec derived from the tool's JSON Schema. This module turns
the specs into argparse subcommands and, when one is invoked, embeds the
parameters as JSON into the template and sends it through figmosha's /exec.

Parameters reach the JS only as a JSON string literal handed to JSON.parse,
so no CLI value is ever spliced into code.
"""
from __future__ import annotations

import argparse
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
COMMANDS_PATH = os.path.join(HERE, "commands.json")

_DATA = None


def load():
    global _DATA
    if _DATA is None:
        try:
            with open(COMMANDS_PATH, encoding="utf-8") as fh:
                _DATA = json.load(fh)
        except FileNotFoundError:
            _DATA = {"commands": [], "params_token": ""}
    return _DATA


def commands():
    return load()["commands"]


def names():
    """Every name and alias, for figmosha's KNOWN_CMDS."""
    out = set()
    for c in commands():
        out.add(c["name"])
        out.update(c.get("aliases", []))
    return out


def _by_name():
    m = {}
    for c in commands():
        m[c["name"]] = c
        for a in c.get("aliases", []):
            m[a] = c
    return m


# ─── value parsing ─────────────────────────────────────────────────────────

def _bool(s):
    v = str(s).strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise argparse.ArgumentTypeError(f"expected true/false, got {s!r}")


def _json(s):
    try:
        return json.loads(s)
    except json.JSONDecodeError as e:
        raise argparse.ArgumentTypeError(f"not valid JSON ({e.msg}): {s!r}")


def _json_or_string(s):
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return s


def _paints(s):
    """'#ff0000' or '#f00,#00ff0080' -> SOLID paints; JSON arrays pass through."""
    t = s.strip()
    if t.startswith("[") or t.startswith("{"):
        v = _json(t)
        return v if isinstance(v, list) else [v]
    return [{"type": "SOLID", "color": c.strip()} for c in t.split(",") if c.strip()]


TYPES = {
    "string": str,
    "number": float,
    "integer": int,
    "boolean": _bool,
    "json": _json,
    "json_or_string": _json_or_string,
}


def _short_help(text, limit=220):
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + " …"
    return text.replace("%", "%%")


def add_subparsers(sub, add_common_flags):
    """Register every generated command on figmosha's subparsers object."""
    for c in commands():
        desc = c["description"].replace("%", "%%")
        epilog = (f"Ported from figma-console-mcp {c['tool']} "
                  f"[{c['template'].lower()}] — " + "; ".join(c["source"])).replace("%", "%%")
        p = sub.add_parser(c["name"], aliases=c.get("aliases", []),
                           help=_short_help(c["description"], 90),
                           description=desc, epilog=epilog)
        add_common_flags(p)
        p.set_defaults(_ext_command=c["name"])
        for spec in c["params"]:
            typ = _paints if spec.get("sugar") == "paints" else TYPES[spec["kind"]]
            kw = {"type": typ, "help": _short_help(spec["help"])}
            if "enum" in spec:
                kw["choices"] = spec["enum"]
            dest = "p__" + spec["name"]
            if spec["positional"]:
                kw["metavar"] = spec["name"]
                p.add_argument(dest, **kw)
            else:
                if spec["kind"] == "boolean":
                    kw.pop("type")
                    kw["action"] = argparse.BooleanOptionalAction
                kw.setdefault("metavar", spec["name"].upper()) if "action" not in kw else None
                p.add_argument(spec["flag"], dest=dest, default=None, **kw)


def build_params(command, args):
    """CLI namespace -> the `msg` fields the plugin handler reads."""
    params = {}
    for spec in command["params"]:
        v = getattr(args, "p__" + spec["name"], None)
        if v is None and "default" in spec:
            v = spec["default"]  # zod defaults, applied as the MCP SDK would
        if v is None:
            continue
        if "item_enum" in spec and isinstance(v, list):
            bad = [x for x in v if x not in spec["item_enum"]]
            if bad:
                raise SystemExit(f"figmosha: {spec['name']}: invalid value(s) {bad}; "
                                 f"choose from {spec['item_enum']}")
        params[spec["msg_field"]] = v
    return params


ALIASES = ("page", "sel")
_NODE_FIELD = re.compile(r"(?:^|[a-z])(?:[Nn]ode|[Pp]arent|[Ss]lide|[Ii]nstance|[Cc]omponent)Id$")


def alias_fields(command, params):
    """Node-id params given as figmosha's `page` / `sel` aliases."""
    return [spec["msg_field"] for spec in command["params"]
            if _NODE_FIELD.search(spec["name"]) and params.get(spec["msg_field"]) in ALIASES]


def render(command, params):
    """The JS sent to /exec: the template with params embedded as JSON."""
    token = load()["params_token"]
    literal = json.dumps(json.dumps(params, ensure_ascii=False), ensure_ascii=False)
    js = command["js"]
    if js.count(token) != 1:
        raise RuntimeError(f"template for {command['name']} lacks the params token")
    js = js.replace(token, literal, 1)
    fields = alias_fields(command, params)
    if fields:
        # `page` / `sel` resolve through figmosha's own h.resolve, right after
        # `msg` is built (the first line of every template).
        first, rest = js.split("\n", 1)
        lines = [f"{{ const __n = await h.resolve(msg[{json.dumps(f)}]); "
                 f"if (!__n) throw new Error('nothing for ' + msg[{json.dumps(f)}]); "
                 f"msg[{json.dumps(f)}] = __n.id; }}" for f in fields]
        js = first + "\n" + "\n".join(lines) + "\n" + rest
    return js


def is_ext(args):
    return getattr(args, "_ext_command", None) is not None


def run(args, exec_fn, emit_fn):
    command = _by_name()[args._ext_command]
    params = build_params(command, args)
    if getattr(args, "print_js", False):
        print(render(command, params))
        return 0
    return emit_fn(exec_fn(render(command, params), args.timeout)[1], raw=args.raw)
