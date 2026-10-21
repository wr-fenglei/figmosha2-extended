#!/usr/bin/env python3
"""Static analysis of southleft/figma-console-mcp: where each MCP tool is
registered and which transport its handler uses.

Categories
  PLUGIN  goes only through the Desktop Bridge plugin (WebSocket sendCommand,
          executeCodeViaUI, or a connector method that wraps one)
  REST    goes only through FigmaAPI (api.figma.com)
  HYBRID  uses both (usually plugin first, REST as fallback, or REST data
          enriched with plugin data)
  LOCAL   touches neither: MCP-server-side state (console buffer, connected
          file list), local codebase / file system, axe-core, etc.

Stdlib only. Usage:
  python tools/analyze_console_mcp.py --src ../figma-console-mcp \
      --out tools/data/console_mcp_tools
writes <out>.json and <out>.csv
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SRC = os.environ.get("CONSOLE_MCP_SRC",
                             os.path.join(HERE, "..", "..", "figma-console-mcp"))
DEFAULT_LIST = os.path.join(HERE, "data", "console-mcp-tools-list.json")


# --------------------------------------------------------------------------
# A tiny JS/TS lexer: find the bracket that closes the one at s[i], skipping
# strings, template literals (with ${} nesting) and comments. Regex literals
# are not understood, so callers bound the result by the next registration.
# --------------------------------------------------------------------------
def match_bracket(s: str, i: int) -> int:
    depth, n = 0, len(s)
    while i < n:
        c = s[i]
        if c in "\"'":
            q = c
            i += 1
            while i < n and s[i] != q and s[i] != "\n":
                if s[i] == "\\":
                    i += 1
                i += 1
        elif c == "`":
            i += 1
            while i < n and s[i] != "`":
                if s[i] == "\\":
                    i += 1
                elif s[i] == "$" and i + 1 < n and s[i + 1] == "{":
                    i = match_bracket(s, i + 1)
                i += 1
        elif s.startswith("//", i):
            j = s.find("\n", i)
            i = n if j < 0 else j
        elif s.startswith("/*", i):
            j = s.find("*/", i + 2)
            i = n if j < 0 else j + 1
        elif c in "({[":
            depth += 1
        elif c in ")}]":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n - 1


def line_of(s: str, pos: int) -> int:
    return s.count("\n", 0, pos) + 1


# --------------------------------------------------------------------------
# Source indexes
# --------------------------------------------------------------------------
def connector_index(src: str) -> dict:
    """WebSocketConnector method -> {line, commands[], execute_code}."""
    path = os.path.join(src, "src/core/websocket-connector.ts")
    s = open(path, encoding="utf-8").read()
    out = {}
    ms = list(re.finditer(r"^\s+async (\w+)\(", s, re.M))
    for k, m in enumerate(ms):
        end = ms[k + 1].start() if k + 1 < len(ms) else len(s)
        body = s[m.start():end]
        cmds = re.findall(r"sendCommand\(\s*['\"]([A-Z_]+)", body)
        out[m.group(1)] = {
            "line": line_of(s, m.start()),
            "commands": [c for c in dict.fromkeys(cmds) if c != "EXECUTE_CODE"],
            "execute_code": "EXECUTE_CODE" in cmds,
        }
    return out


def rest_index(src: str) -> dict:
    """FigmaAPI method -> endpoint template(s)."""
    s = open(os.path.join(src, "src/core/figma-api.ts"), encoding="utf-8").read()
    out = {}
    ms = list(re.finditer(r"^\s+async (\w+)\(", s, re.M))
    for k, m in enumerate(ms):
        end = ms[k + 1].start() if k + 1 < len(ms) else len(s)
        body = s[m.start():end]
        eps = re.findall(r"`(/[^`?]+)", body)
        calls = re.findall(r"this\.(get\w+|post\w+|delete\w+)\(", body)
        out[m.group(1)] = {"endpoints": list(dict.fromkeys(eps)),
                           "calls": list(dict.fromkeys(calls))}
    # resolve composite methods (getAllVariables -> getLocalVariables ...)
    for v in out.values():
        for c in v["calls"]:
            if c in out:
                v["endpoints"] += [e for e in out[c]["endpoints"] if e not in v["endpoints"]]
    return out


def plugin_handlers(src: str) -> dict:
    """figma-desktop-bridge/code.js: message type -> line of its handler."""
    s = open(os.path.join(src, "figma-desktop-bridge/code.js"), encoding="utf-8").read()
    return {m.group(1): line_of(s, m.start())
            for m in re.finditer(r"msg\.type === '([A-Z_]+)'", s)}


def registrations(src: str):
    """Yield (name, file, line, body) for every MCP tool registration that the
    local (stdio) server makes. src/index.ts is the Cloudflare/cloud entry and
    only duplicates seven local.ts tools, so it is skipped."""
    files = ["src/local.ts"] + sorted(
        os.path.relpath(p, src) for p in glob.glob(os.path.join(src, "src/core/*.ts"))
    ) + sorted(os.path.relpath(p, src) for p in glob.glob(os.path.join(src, "src/apps/*/server.ts")))
    for f in files:
        s = open(os.path.join(src, f), encoding="utf-8").read()
        regs = list(re.finditer(r"\.(tool|registerTool)\(\s*[\"']([a-z_]+)[\"']", s))
        for k, m in enumerate(regs):
            open_paren = m.start() + m.group(0).index("(")
            nxt = regs[k + 1].start() if k + 1 < len(regs) else len(s)
            end = min(match_bracket(s, open_paren) + 1, nxt)
            yield m.group(2), f, line_of(s, m.start(2)), s[m.start():end]


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------
# Handlers that reach Figma only through a helper defined elsewhere, so the
# body scan alone cannot see the transport. Verified by reading the helpers.
OVERRIDES = {
    "figma_get_console_logs": ("LOCAL", "wsServer.getConsoleLogs(): MCP-side buffer of plugin console.* (pushed as CONSOLE_CAPTURE)"),
    "figma_watch_console": ("LOCAL", "polls the MCP-side console buffer"),
    "figma_clear_console": ("LOCAL", "wsServer.clearConsoleLogs()"),
    "figma_navigate": ("LOCAL", "switches the active file among connected plugin sockets (wsServer.getConnectedFiles)"),
    "figma_get_design_changes": ("LOCAL", "wsServer.getDocumentChanges(): buffered documentchange events pushed by the plugin"),
    "figma_get_component_details": ("PLUGIN", "ensureDesignSystemCache() -> connector.getVariables (EXECUTE_CODE) + getLocalComponents (GET_LOCAL_COMPONENTS)"),
    "figma_get_token_values": ("PLUGIN", "ensureDesignSystemCache() -> connector.getVariables + getLocalComponents"),
    "figma_export_tokens": ("PLUGIN", "handleExport() -> connector.getVariablesFromPluginUI (GET_VARIABLES_DATA), then writes token files locally"),
    "figma_diff_versions": ("REST", "computeDiff() -> api.getFileVersions / getFile"),
    "figma_get_changes_since_version": ("REST", "computeDiff() -> api.getFileVersions / getFile"),
    "figma_audit_design_system_report": ("HYBRID", "fetchDesignSystemAuditData: cache -> Desktop Bridge -> REST fallback chain"),
    "figma_diagnose": ("LOCAL", "buildReport(): local environment / connection diagnostics"),
    "figma_get_status": ("PLUGIN", "connection state + GET_FILE_INFO / EXECUTE_CODE probe"),
}

LOCAL_HINTS = re.compile(r"getConsoleLogs|clearConsoleLogs|getDocumentChanges|readFile|writeFile|"
                         r"\bfs\.|axe|jsdom|loadAnalysis|analyzeCodebase|codebase")


def classify(src: str, list_path: str | None = None) -> list[dict]:
    conn = connector_index(src)
    rest = rest_index(src)
    handlers = plugin_handlers(src)
    schemas = {}
    if list_path and os.path.exists(list_path):
        for t in json.load(open(list_path, encoding="utf-8"))["tools"]:
            schemas[t["name"]] = t
    rows = []
    for name, f, line, body in registrations(src):
        cmethods = [c for c in dict.fromkeys(re.findall(r"[cC]onnector\)?\.(\w+)\(", body)) if c in conn]
        direct_cmds = re.findall(r"sendCommand\(\s*['\"]([A-Z_]+)", body)
        if "ensureDesignSystemCache" in body:
            cmethods += [c for c in ("getVariables", "getLocalComponents") if c not in cmethods]
        commands, exec_code = [], False
        for c in cmethods:
            commands += conn[c]["commands"]
            exec_code |= conn[c]["execute_code"]
        for c in direct_cmds:
            if c == "EXECUTE_CODE":
                exec_code = True
            else:
                commands.append(c)
        commands = list(dict.fromkeys(commands))
        rmethods = [m for m in dict.fromkeys(re.findall(r"\bapi\.(\w+)\(", body)) if m in rest]
        endpoints = []
        for m in rmethods:
            endpoints += [e for e in rest[m]["endpoints"] if e not in endpoints]
        uses_plugin = bool(cmethods or direct_cmds) or "getDesktopConnector" in body
        uses_rest = bool(rmethods) or "getFigmaAPI" in body or "api.figma.com" in body
        local = sorted(set(LOCAL_HINTS.findall(body)))
        if uses_plugin and uses_rest:
            cat = "HYBRID"
        elif uses_plugin:
            cat = "PLUGIN"
        elif uses_rest:
            cat = "REST"
        else:
            cat = "LOCAL"
        note = ""
        if name in OVERRIDES:
            cat, note = OVERRIDES[name]
        rows.append({
            "name": name,
            "category": cat,
            "file": f,
            "line": line,
            "handler_lines": body.count("\n") + 1,
            "connector_methods": cmethods,
            "plugin_commands": commands,
            "plugin_handler_lines": {c: handlers.get(c) for c in commands},
            "execute_code": exec_code,
            "rest_methods": rmethods,
            "rest_endpoints": endpoints,
            "local_markers": local,
            "note": note,
            "description": schemas.get(name, {}).get("description", ""),
        })
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default=DEFAULT_SRC, help="figma-console-mcp checkout")
    ap.add_argument("--list", default=DEFAULT_LIST, help="tools/list JSON (for descriptions)")
    ap.add_argument("--out", default=os.path.join(HERE, "data", "console_mcp_tools"))
    a = ap.parse_args(argv)
    rows = classify(a.src, a.list)
    with open(a.out + ".json", "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=1, ensure_ascii=False)
    with open(a.out + ".csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["tool", "category", "handler", "plugin_commands", "execute_code",
                    "connector_methods", "rest_endpoints", "local_markers", "note"])
        for r in rows:
            w.writerow([r["name"], r["category"], f'{r["file"]}:{r["line"]}',
                        " ".join(f"{c}@code.js:{l}" for c, l in r["plugin_handler_lines"].items()),
                        "yes" if r["execute_code"] else "",
                        " ".join(r["connector_methods"]), " ".join(r["rest_endpoints"]),
                        " ".join(r["local_markers"]), r["note"]])
    counts = {}
    for r in rows:
        counts[r["category"]] = counts.get(r["category"], 0) + 1
    print(f"{len(rows)} tools: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
