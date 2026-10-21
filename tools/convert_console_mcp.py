#!/usr/bin/env python3
"""Convert figma-console-mcp's plugin-side tools into figmosha CLI commands.

Reads
  * the tools/list JSON (names, descriptions, JSON Schemas of all 121 tools)
  * the figma-console-mcp source (classification, see analyze_console_mcp.py)
  * figma-desktop-bridge/code.js (the plugin's per-command handlers)
and writes figmosha_ext/commands.json, which figmosha_ext/__init__.py turns
into argparse subcommands at runtime (`python figmosha.py set-fills ...`).

How a command is produced
  PORTED     the tool's TS handler forwards to exactly one named plugin
             command (SET_NODE_FILLS, CREATE_VARIABLE ...). The converter
             cuts that command's `else if (msg.type === 'X') {...}` block out
             of code.js, adds the top-level helpers it references
             (hexToFigmaRGB, loadFontsForNode ...), and wraps it so it runs
             through figmosha's /exec: `msg` is rebuilt from the CLI params
             (JSON-embedded, never string-concatenated), figma.ui.postMessage
             is redirected to a local catcher, and the X_RESULT payload is
             returned (or thrown when success === false).
  COMPOSITE  a tool that calls two named commands conditionally
             (figma_update_variable): both ported blocks, run in order.
  HAND       a small hand-written template, kept in HAND_TEMPLATES below.
  MANUAL     the TS side builds Plugin API code itself (executeCodeViaUI with
             a TS template literal) or post-processes heavily; listed in
             the report as "needs manual template", not generated.
  COVERED    already provided by a native figmosha command (exec, status ...).
REST / HYBRID / LOCAL tools are never generated.

Stdlib only.  Usage:
  python tools/convert_console_mcp.py [--src ../figma-console-mcp] \
      [--list tools/data/console-mcp-tools-list.json] [--out figmosha_ext/commands.json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from analyze_console_mcp import (DEFAULT_LIST, DEFAULT_SRC, classify,  # noqa: E402
                                 line_of, match_bracket)

REPO = os.path.dirname(HERE)
DEFAULT_OUT = os.path.join(REPO, "figmosha_ext", "commands.json")
PARAMS_TOKEN = "/*__FIGMOSHA_PARAMS__*/null"

# figmosha's own subcommands; generated names must never shadow them.
NATIVE = {"exec", "status", "targets", "clear", "doctor", "sel", "tree", "find", "text",
          "variant", "clone", "rm", "import-component", "icomp"}

# ---------------------------------------------------------------------------
# Mapping tables (edit these, then re-run)
# ---------------------------------------------------------------------------
# tool -> CLI name. Default: drop the "figma_" prefix, snake -> kebab.
NAME_OVERRIDES: dict[str, str] = {}
# tool -> extra short aliases
ALIASES = {
    "figma_set_fills": ["fill"],
    "figma_set_strokes": ["stroke"],
    "figma_resize_node": ["resize"],
    "figma_move_node": ["move"],
    "figma_rename_node": ["rename"],
    "figma_create_child": ["create"],
    "figma_delete_node": ["delete-node"],
    "figma_get_selection": ["selection"],
}
# tool -> {schema property: msg field}, where the TS/UI layer renames a param
# on its way to the plugin (verified in websocket-connector.ts / ui.html).
RENAMES = {
    "figma_get_annotations": {"include_children": "includeChildren"},
    "figma_add_component_property": {"type": "propertyType"},
    # Upstream sends `fillColor` but the ADD_SHAPE_TO_SLIDE handler reads
    # `msg.color`, so the fill is silently dropped in console-mcp itself.
    "figma_add_shape_to_slide": {"fillColor": "color"},
}
# tool -> JS run after `msg` is built, before the handler (replaces work the
# plugin's ui.html did, e.g. base64 -> bytes, which the sandbox can do too).
PRE_JS = {
    "figma_set_image_fill": (
        "msg.nodeIds = Array.isArray(msg.nodeIds) ? msg.nodeIds : [msg.nodeIds];"
        "msg.imageBytes = Array.from(figma.base64Decode(msg.imageData));"
        "if (!msg.scaleMode) msg.scaleMode = 'FILL';"
    ),
}
# tool.param -> CLI sugar. "paints": a bare "#hex[,#hex...]" becomes
# [{"type":"SOLID","color":"#hex"}, ...]; a JSON array is passed through.
SUGAR = {
    ("figma_set_fills", "fills"): "paints",
    ("figma_set_strokes", "strokes"): "paints",
}
# Tools that call two named commands, each only when its params are present.
COMPOSITE = {
    "figma_update_variable": [
        ("UPDATE_VARIABLE", "msg.modeId !== undefined && msg.value !== undefined"),
        ("SET_VARIABLE_DESCRIPTION", "msg.description !== undefined"),
    ],
}
# Plugin tools that figmosha already covers natively (or that only make sense
# for console-mcp's own MCP/WebSocket plumbing).
COVERED = {
    "figma_execute": "figmosha exec",
    "figma_execute_across_files": "figmosha exec -T <file>, once per file",
    "figma_get_status": "figmosha status / doctor",
    "figma_reconnect": "figmosha doctor (the bridge reconnects by itself)",
    "figma_reload_plugin": "RELOAD_UI reloads console-mcp's own plugin UI; re-Run Figmosha Bridge instead",
}
# Small hand-written templates: the TS side reads an MCP-side cache or builds
# code in TypeScript, so there is no plugin handler to port.
HAND_TEMPLATES = {
    "figma_get_selection": {
        "source": "hand-written (console-mcp reads its selection cache + executeCodeViaUI; see src/local.ts)",
        "js": r"""
const sel = figma.currentPage.selection;
const out = sel.map(function (n) {
  const o = { id: n.id, name: n.name, type: n.type };
  if ('x' in n) { o.x = n.x; o.y = n.y; }
  if ('width' in n) { o.width = n.width; o.height = n.height; }
  if (msg.verbose) {
    if ('fills' in n && n.fills !== figma.mixed) o.fills = n.fills;
    if (n.type === 'TEXT') o.characters = n.characters;
    if ('children' in n) o.childCount = n.children.length;
  }
  return o;
});
return { selection: out, count: out.length, page: { id: figma.currentPage.id, name: figma.currentPage.name } };
""",
    },
}


# ---------------------------------------------------------------------------
# code.js parsing
# ---------------------------------------------------------------------------
class PluginSource:
    def __init__(self, path: str):
        self.path = path
        self.s = open(path, encoding="utf-8").read()
        m = re.search(r"^figma\.ui\.onmessage = async \(msg\) => \{", self.s, re.M)
        self.on_start = m.start()
        self.on_end = match_bracket(self.s, m.end() - 1)
        self.decls = self._top_level_decls()

    def handler(self, cmd: str):
        """Return (block_inner_text, line) for `msg.type === 'CMD'`."""
        m = re.search(r"msg\.type === '%s'\)\s*\{" % re.escape(cmd), self.s)
        if not m:
            return None, None
        open_b = m.end() - 1
        close_b = match_bracket(self.s, open_b)
        return self.s[open_b + 1:close_b], line_of(self.s, m.start())

    def _top_level_decls(self) -> dict:
        """name -> source text, for top-level function / var declarations
        outside the onmessage handler."""
        out = {}
        for m in re.finditer(r"^(?:async )?function (\w+)\s*\(|^var (\w+)\s*=", self.s, re.M):
            if self.on_start <= m.start() <= self.on_end:
                continue
            name = m.group(1) or m.group(2)
            if name in ("PLUGIN_VERSION",):
                continue
            if m.group(1):
                brace = self.s.index("{", m.end())
                # skip the parameter list: first "{" after the closing ")"
                close_paren = match_bracket(self.s, m.end() - 1)
                brace = self.s.index("{", close_paren)
                end = match_bracket(self.s, brace) + 1
            else:
                end = self._statement_end(m.end())
            out[name] = self.s[m.start():end]
        return out

    def _statement_end(self, i: int) -> int:
        n = len(self.s)
        while i < n:
            c = self.s[i]
            if c in "({[":
                i = match_bracket(self.s, i)
            elif c in "\"'`":
                j = i + 1
                while j < n and self.s[j] != c:
                    j += 2 if self.s[j] == "\\" else 1
                i = j
            elif c == ";" or c == "\n":
                return i + 1
            i += 1
        return n

    def helpers_for(self, text: str) -> list[str]:
        """Top-level declarations `text` needs, dependency-ordered."""
        need, order = set(), []

        def visit(t):
            for name, src in self.decls.items():
                if name in need:
                    continue
                if re.search(r"(?<![\w.$])%s(?![\w$])" % re.escape(name), t):
                    need.add(name)
                    visit(src)
                    order.append(name)
        visit(text)
        return order


def wrap_block(block: str) -> str:
    """Make a code.js handler block runnable inside an exec."""
    return block.replace("figma.ui.postMessage(", "__ui.postMessage(")


PRELUDE = """\
const msg = Object.assign({ requestId: 'figmosha' }, JSON.parse(%s));
const __results = [];
const __ui = { postMessage: function (m) {
  if (m && typeof m.type === 'string' && /_RESULT$/.test(m.type)) __results.push(m);
} };
function __take(cmd) {
  const r = __results.filter(function (m) { return m.type === cmd + '_RESULT'; })[0];
  if (!r) throw new Error(cmd + ': plugin handler produced no result');
  if (r.success === false) throw new Error(r.error || (cmd + ' failed'));
  const out = {};
  for (const k in r) if (k !== 'type' && k !== 'requestId' && k !== 'success') out[k] = r[k];
  return out;
}
""" % PARAMS_TOKEN


def build_ported_js(src: PluginSource, tool: str, cmds: list, conds: list | None = None) -> tuple[str, list]:
    blocks, helpers_text, sources = [], "", []
    for i, cmd in enumerate(cmds):
        block, line = src.handler(cmd)
        if block is None:
            raise KeyError(cmd)
        sources.append(f"figma-desktop-bridge/code.js:{line} ({cmd})")
        body = "msg.type = %s;\nawait (async () => {%s})();\n" % (json.dumps(cmd), wrap_block(block))
        if conds:
            body = "if (%s) {\n%s__out = Object.assign(__out, __take(%s));\n}\n" % (
                conds[i], body, json.dumps(cmd))
        blocks.append(body)
        helpers_text += block
    helpers = src.helpers_for(helpers_text + PRE_JS.get(tool, ""))
    parts = [PRELUDE]
    parts += [wrap_block(src.decls[h]) for h in helpers]
    if tool in PRE_JS:
        parts.append(PRE_JS[tool])
    if conds:
        parts.append("let __out = {};")
        parts += blocks
        parts.append("if (!Object.keys(__out).length) throw new Error('nothing to do: no matching parameters');")
        parts.append("return __out;")
    else:
        parts += blocks
        parts.append("return __take(%s);" % json.dumps(cmds[0]))
    return "\n".join(parts), sources + [f"helpers: {', '.join(helpers) or '-'}"]


# ---------------------------------------------------------------------------
# JSON Schema -> argparse spec
# ---------------------------------------------------------------------------
RESERVED_FLAGS = {"timeout", "raw", "target", "parallel", "agent", "queue-timeout", "host",
                  "port", "help", "file", "stdin"}


def kebab(s: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", s)
    return s.replace("_", "-").lower()


def param_specs(tool: str, schema: dict) -> list[dict]:
    props = schema.get("properties", {})
    required = schema.get("required", [])
    specs = []
    for name, ps in props.items():
        t = ps.get("type")
        if isinstance(t, list) or "anyOf" in ps or "oneOf" in ps or t is None:
            kind = "json_or_string"
        elif t in ("array", "object"):
            kind = "json"
        else:
            kind = t  # string | number | integer | boolean
        req = name in required
        flag = kebab(name)
        if flag in RESERVED_FLAGS:
            flag = "param-" + flag
        spec = {
            "name": name,
            "msg_field": RENAMES.get(tool, {}).get(name, name),
            "kind": kind,
            "required": req,
            # required scalars and required JSON blobs are positional, in
            # schema order; everything optional is a --flag
            "positional": req,
            "flag": "--" + flag,
            "help": (ps.get("description") or "").strip(),
        }
        if "enum" in ps:
            spec["enum"] = ps["enum"]
        elif t == "array" and isinstance(ps.get("items"), dict) and "enum" in ps["items"]:
            spec["item_enum"] = ps["items"]["enum"]
        if "default" in ps:
            spec["default"] = ps["default"]
        sugar = SUGAR.get((tool, name))
        if sugar:
            spec["sugar"] = sugar
        specs.append(spec)
    return specs


def cli_name(tool: str) -> str:
    if tool in NAME_OVERRIDES:
        return NAME_OVERRIDES[tool]
    name = tool[len("figma_"):] if tool.startswith("figma_") else tool
    name = kebab(name)
    if name in NATIVE:
        name = kebab(tool)
    return name


# ---------------------------------------------------------------------------
def convert(src_dir: str, list_path: str) -> dict:
    rows = {r["name"]: r for r in classify(src_dir, list_path)}
    tools = {t["name"]: t for t in json.load(open(list_path, encoding="utf-8"))["tools"]}
    plugin = PluginSource(os.path.join(src_dir, "figma-desktop-bridge", "code.js"))
    commands, skipped = [], []
    used = set(NATIVE)
    for name, row in rows.items():
        cat = row["category"]
        if cat != "PLUGIN":
            skipped.append({"tool": name, "category": cat, "status": "NOT_PLUGIN",
                            "reason": f"{cat} tool (see analysis)"})
            continue
        if name in COVERED:
            skipped.append({"tool": name, "category": cat, "status": "COVERED",
                            "reason": COVERED[name]})
            continue
        cmds = row["plugin_commands"]
        kind = None
        try:
            if name in HAND_TEMPLATES:
                kind, js = "HAND", PRELUDE + HAND_TEMPLATES[name]["js"]
                sources = [HAND_TEMPLATES[name]["source"]]
            elif name in COMPOSITE:
                cc = COMPOSITE[name]
                js, sources = build_ported_js(plugin, name, [c for c, _ in cc], [k for _, k in cc])
                kind = "COMPOSITE"
            elif len(cmds) == 1 and not row["execute_code"]:
                js, sources = build_ported_js(plugin, name, cmds)
                kind = "PORTED"
        except KeyError as e:
            skipped.append({"tool": name, "category": cat, "status": "MANUAL",
                            "reason": f"plugin handler {e} not found in code.js"})
            continue
        if kind is None:
            why = ("TS builds Plugin API code itself (executeCodeViaUI / EXECUTE_CODE template)"
                   if row["execute_code"] else
                   "no single named plugin command (" + (", ".join(cmds) or "helper-only path") + ")")
            skipped.append({"tool": name, "category": cat, "status": "MANUAL", "reason": why})
            continue
        cname = cli_name(name)
        aliases = [a for a in ALIASES.get(name, []) if a not in used and a != cname]
        if cname in used:
            raise SystemExit(f"name clash: {cname} ({name})")
        used.add(cname)
        used.update(aliases)
        schema = tools[name]["inputSchema"]
        commands.append({
            "name": cname,
            "aliases": aliases,
            "tool": name,
            "template": kind,
            "description": tools[name].get("description", ""),
            "params": param_specs(name, schema),
            "source": sources,
            "ts_handler": f'{row["file"]}:{row["line"]}',
            # A long TS handler does more than forward to the plugin (input
            # validation, retries, response shaping); that part is not ported.
            "ts_handler_lines": row["handler_lines"],
            "js": js,
        })
    return {
        "generated_by": "tools/convert_console_mcp.py",
        "upstream": "southleft/figma-console-mcp",
        "upstream_version": _upstream_version(src_dir),
        "params_token": PARAMS_TOKEN,
        "commands": commands,
        "not_generated": skipped,
    }


def _upstream_version(src_dir):
    try:
        return json.load(open(os.path.join(src_dir, "package.json")))["version"]
    except Exception:
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default=DEFAULT_SRC)
    ap.add_argument("--list", default=DEFAULT_LIST)
    ap.add_argument("--out", default=DEFAULT_OUT)
    a = ap.parse_args(argv)
    data = convert(a.src, a.list)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    by = {}
    for c in data["commands"]:
        by[c["template"]] = by.get(c["template"], 0) + 1
    for s in data["not_generated"]:
        by[s["status"]] = by.get(s["status"], 0) + 1
    print(f"wrote {a.out}: {len(data['commands'])} commands; "
          + ", ".join(f"{k} {v}" for k, v in sorted(by.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
