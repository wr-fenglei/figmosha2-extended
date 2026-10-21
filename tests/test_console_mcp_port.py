"""End-to-end tests for the commands generated from figma-console-mcp.

Chain under test:  figmosha.py <cmd>  ->  real bridge.py (subprocess)  ->
fake Figma desktop (tests/fake_figma/run_plugin.js: the REAL plugin/code.js
in a Node vm, against an in-memory mock of the Plugin API).

The mock is not Figma. These tests prove that argument parsing, JSON
embedding, the ported console-mcp handler code, helper extraction and
result unwrapping all work together; they do not prove that a handler does
the right thing against the real Plugin API.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import figmosha_ext  # noqa: E402


def _node_ok():
    node = shutil.which("node")
    if not node:
        return None
    for flags in ([], ["--experimental-websocket"]):
        r = subprocess.run([node, *flags, "-e", "process.exit(typeof WebSocket==='undefined'?1:0)"],
                           capture_output=True)
        if r.returncode == 0:
            return [node, *flags]
    return None


NODE = _node_ok()
needs_node = pytest.mark.skipif(NODE is None, reason="needs Node with a global WebSocket")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def figma():
    if NODE is None:
        pytest.skip("needs Node with a global WebSocket")
    port = _free_port()
    env = dict(os.environ, FIGMOSHA_NO_UPDATE_CHECK="1", PYTHONUNBUFFERED="1")
    bridge = subprocess.Popen([sys.executable, str(ROOT / "bridge.py"), "--port", str(port)],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    plugin = None
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
                break
            except OSError:
                time.sleep(0.05)
        plugin = subprocess.Popen([*NODE, str(ROOT / "tests/fake_figma/run_plugin.js"),
                                   "--port", str(port)],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        line = plugin.stdout.readline()
        assert line.startswith("READY"), line
        yield Runner(port)
    finally:
        for p in (plugin, bridge):
            if p:
                p.terminate()
                p.wait(timeout=5)


class Runner:
    def __init__(self, port):
        self.port = port

    def cli(self, *args, ok=True):
        r = subprocess.run([sys.executable, str(ROOT / "figmosha.py"), *args, "--raw"],
                           capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, FIGMOSHA_PORT=str(self.port)))
        resp = json.loads(r.stdout) if r.stdout.strip() else {}
        if ok:
            assert r.returncode == 0 and resp.get("ok"), (r.stdout, r.stderr)
        return resp

    def value(self, *args):
        return self.cli(*args)["value"]

    def js(self, code):
        return self.value("exec", code)


# ─── offline: generation and parameter handling ───────────────────────────

def test_generated_commands_load_and_do_not_shadow_native():
    import figmosha
    native = {"exec", "status", "targets", "clear", "doctor", "sel", "tree", "find", "text",
              "variant", "clone", "rm", "import-component", "icomp"}
    names = figmosha_ext.names()
    assert len(figmosha_ext.commands()) >= 60
    assert not names & native
    assert names <= figmosha.KNOWN_CMDS
    ap = figmosha.build_parser()
    args = ap.parse_args(["fill", "1:2", "#ff0000,#00ff0080"])
    assert args._ext_command == "set-fills"


def test_every_template_has_exactly_one_params_token():
    token = figmosha_ext.load()["params_token"]
    for c in figmosha_ext.commands():
        assert c["js"].count(token) == 1, c["name"]


def test_params_are_json_embedded_not_concatenated():
    import figmosha
    evil = 'x"); figma.root.remove(); ("`${1}`</script>\u2028'
    args = figmosha.build_parser().parse_args(["rename-node", "1:2", evil])
    cmd = figmosha_ext._by_name()["rename-node"]
    js = figmosha_ext.render(cmd, figmosha_ext.build_params(cmd, args))
    first = js.split("\n", 1)[0]
    # The whole params object is one JS string literal handed to JSON.parse.
    literal = first[first.index("JSON.parse(") + len("JSON.parse("):first.rindex("));")]
    assert json.loads(json.loads(literal)) == {"nodeId": "1:2", "newName": evil}


def test_schema_defaults_and_renames_applied():
    import figmosha
    ap = figmosha.build_parser()
    cmd = figmosha_ext._by_name()["resize-node"]
    p = figmosha_ext.build_params(cmd, ap.parse_args(["resize", "1:2", "10", "20"]))
    assert p == {"nodeId": "1:2", "width": 10.0, "height": 20.0, "withConstraints": True}
    cmd = figmosha_ext._by_name()["add-component-property"]
    p = figmosha_ext.build_params(cmd, ap.parse_args(
        ["add-component-property", "1:2", "Label", "TEXT", "--default-value", '"Hi"']))
    assert p["propertyType"] == "TEXT" and "type" not in p


def test_regeneration_is_deterministic(tmp_path):
    src = ROOT.parent / "figma-console-mcp"
    if not (src / "figma-desktop-bridge" / "code.js").exists():
        pytest.skip("figma-console-mcp checkout not next to the repo")
    out = tmp_path / "commands.json"
    r = subprocess.run([sys.executable, str(ROOT / "tools/convert_console_mcp.py"),
                        "--src", str(src), "--out", str(out)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(out.read_text()) == figmosha_ext.load()


# ─── end to end through bridge + fake plugin ──────────────────────────────

@needs_node
def test_create_frame(figma):
    v = figma.value("create", "page", "FRAME", "--properties",
                    json.dumps({"name": "Card", "width": 320, "height": 200, "x": 10, "y": 20}))
    n = v["node"]
    assert (n["type"], n["name"], n["width"], n["height"], n["x"], n["y"]) == \
        ("FRAME", "Card", 320, 200, 10, 20)
    assert figma.js(f"return (await figma.getNodeByIdAsync('{n['id']}')).parent.type") == "PAGE"
    figma.frame = n["id"]


@needs_node
def test_set_fills_hex_and_json(figma):
    fid = figma.frame
    figma.value("fill", fid, "#ff0000")
    fills = figma.js(f"return (await figma.getNodeByIdAsync('{fid}')).fills")
    assert fills == [{"type": "SOLID", "color": {"r": 1, "g": 0, "b": 0}, "opacity": 1}]
    figma.value("set-fills", fid, json.dumps([{"type": "SOLID", "color": "#0000ff", "opacity": 0.5}]))
    fills = figma.js(f"return (await figma.getNodeByIdAsync('{fid}')).fills")
    # Faithful to upstream: SET_NODE_FILLS takes opacity from hexToFigmaRGB's
    # alpha, which is always 1 for a 6-digit hex, so `opacity` is ignored
    # whenever `color` is given (an upstream console-mcp bug, kept as is).
    assert fills[0]["color"] == {"r": 0, "g": 0, "b": 1} and fills[0]["opacity"] == 1
    # 8-digit hex carries alpha
    figma.value("fill", fid, "#00ff0080")
    assert abs(figma.js(f"return (await figma.getNodeByIdAsync('{fid}')).fills[0].opacity") - 128 / 255) < 1e-9


@needs_node
def test_set_strokes(figma):
    figma.value("stroke", figma.frame, "#333333", "--stroke-weight", "2")
    s = figma.js(f"const n = await figma.getNodeByIdAsync('{figma.frame}'); return [n.strokes, n.strokeWeight]")
    assert s[1] == 2 and abs(s[0][0]["color"]["r"] - 0x33 / 255) < 1e-9


@needs_node
def test_resize_move_rename(figma):
    fid = figma.frame
    assert figma.value("resize", fid, "400", "250")["node"]["width"] == 400
    assert figma.value("move", fid, "100", "50")["node"]["x"] == 100
    assert figma.value("rename", fid, "Profile card")["node"] == {
        "id": fid, "name": "Profile card", "oldName": "Card"}
    st = figma.js(f"const n = await figma.getNodeByIdAsync('{fid}'); return [n.width, n.height, n.x, n.y, n.name]")
    assert st == [400, 250, 100, 50, "Profile card"]


@needs_node
def test_rename_with_hostile_string(figma):
    evil = 'a"); figma.root.remove(); ("`${x}`\n</script>'
    figma.value("rename", figma.frame, evil)
    assert figma.js(f"return (await figma.getNodeByIdAsync('{figma.frame}')).name") == evil
    assert figma.js("return figma.root.children.length") == 1
    figma.value("rename", figma.frame, "Profile card")


@needs_node
def test_text_child_and_set_text(figma):
    v = figma.value("create", figma.frame, "TEXT", "--properties", json.dumps({"text": "Hello"}))
    tid = v["node"]["id"]
    assert figma.js(f"return (await figma.getNodeByIdAsync('{tid}')).characters") == "Hello"
    out = figma.value("set-text", tid, "Hi there", "--font-style", "SemiBold", "--font-size", "18")
    assert out["node"]["characters"] == "Hi there"
    st = figma.js(f"const n = await figma.getNodeByIdAsync('{tid}'); return [n.fontName, n.fontSize]")
    # "SemiBold" is auto-corrected to Inter's "Semi Bold" by loadFontWithFallback
    assert st == [{"family": "Inter", "style": "Semi Bold"}, 18]
    figma.text = tid


@needs_node
def test_set_text_reports_font_failure(figma):
    r = figma.cli("set-text", figma.text, "x", "--font-family", "NoSuchFont", ok=False)
    assert r["ok"] is False and "Could not load font" in r["error"]


@needs_node
def test_set_text_rejects_non_text(figma):
    r = figma.cli("set-text", figma.frame, "x", ok=False)
    assert "must be a TEXT node" in r["error"]


@needs_node
def test_read_tree_and_selection(figma):
    tree = figma.value("tree", figma.frame)
    assert "Profile card [FRAME]" in tree and "[TEXT]" in tree
    figma.js(f"figma.currentPage.selection = [await figma.getNodeByIdAsync('{figma.frame}')]; return 1")
    sel = figma.value("get-selection")
    assert sel["count"] == 1 and sel["selection"][0]["id"] == figma.frame
    sel = figma.value("selection", "--verbose")
    assert sel["selection"][0]["childCount"] == 1
    # the `sel` alias works for generated commands too
    assert figma.value("rename", "sel", "Profile card")["node"]["id"] == figma.frame


@needs_node
def test_clone_and_delete(figma):
    c = figma.value("clone-node", figma.frame)["node"]
    assert c["id"] != figma.frame and c["name"] == "Profile card"
    kids = figma.js(f"return (await figma.getNodeByIdAsync('{c['id']}')).children.length")
    assert kids == 1
    assert figma.value("delete-node", c["id"])["deleted"]["id"] == c["id"]
    assert figma.js(f"return await figma.getNodeByIdAsync('{c['id']}')") is None


@needs_node
def test_missing_node_is_an_error(figma):
    r = figma.cli("fill", "9:999", "#fff", ok=False)
    assert r["ok"] is False and "Node not found: 9:999" in r["error"]


@needs_node
def test_variables_lifecycle(figma):
    col = figma.value("create-variable-collection", "Brand", "--initial-mode-name", "Light",
                      "--additional-modes", '["Dark"]')["collection"]
    assert [m["name"] for m in col["modes"]] == ["Light", "Dark"]
    light, dark = (m["modeId"] for m in col["modes"])
    var = figma.value("create-variable", "color/primary", col["id"], "COLOR",
                      "--values-by-mode", json.dumps({light: "#3366ff", dark: "#99bbff"}),
                      "--description", "Primary brand colour")["variable"]
    assert var["resolvedType"] == "COLOR" and var["description"] == "Primary brand colour"
    assert abs(var["valuesByMode"][light]["b"] - 1) < 1e-9
    upd = figma.value("update-variable", var["id"], "--mode-id", light, "--value", '"#000000"',
                      "--description", "changed")
    assert upd["variable"]["valuesByMode"][light] == {"r": 0, "g": 0, "b": 0, "a": 1}
    assert figma.value("rename-variable", var["id"], "color/brand")["variable"]["name"] == "color/brand"
    # bind the variable to a fill through set-fills' variableId
    figma.value("set-fills", figma.frame, json.dumps([{"type": "SOLID", "variableId": var["id"]}]))
    bound = figma.js(f"return (await figma.getNodeByIdAsync('{figma.frame}')).fills[0].boundVariables")
    assert bound == {"color": {"type": "VARIABLE_ALIAS", "id": var["id"]}}
    figma.value("delete-variable", var["id"])
    assert figma.js(f"return await figma.variables.getVariableByIdAsync('{var['id']}')") is None


@needs_node
def test_update_variable_needs_something_to_do(figma):
    r = figma.cli("update-variable", "VariableID:1:1", ok=False)
    assert "nothing to do" in r["error"]


@needs_node
def test_capture_screenshot_uses_export_stub(figma):
    v = figma.value("capture-screenshot", "--node-id", figma.frame)
    blob = json.dumps(v)
    assert "iVBORw0KGgo" in blob  # base64 PNG signature from the mock's exportAsync


@needs_node
def test_set_description_on_component(figma):
    cid = figma.js("const c = figma.createComponent(); c.name = 'Button'; return c.id")
    v = figma.value("set-description", cid, "Primary action")
    assert v["node"]["description"] == "Primary action"


@needs_node
def test_lint_design_runs_the_ported_1200_line_handler(figma):
    v = figma.value("lint-design", "--node-id", figma.frame)
    rules = {c["rule"] for c in v["data"]["categories"]}
    assert v["data"]["rootNodeId"] == figma.frame and "hardcoded-color" in rules


@needs_node
def test_set_image_fill_decodes_base64_in_sandbox(figma):
    import base64
    data = base64.b64encode(bytes(range(40))).decode()
    v = figma.value("set-image-fill", json.dumps([figma.frame]), data, "--scale-mode", "FIT")
    assert v["updatedCount"] == 1
    fill = figma.js(f"return (await figma.getNodeByIdAsync('{figma.frame}')).fills[0]")
    assert fill["type"] == "IMAGE" and fill["scaleMode"] == "FIT" and fill["imageHash"] == "img40"


@needs_node
def test_figjam_command_refuses_in_design_file(figma):
    r = figma.cli("figjam-create-sticky", "hello", ok=False)
    assert "only available in FigJam" in r["error"]


@needs_node
def test_native_commands_still_work(figma):
    assert figma.value("exec", "return 1 + 1") == 2
    tid = figma.text
    figma.cli("text", tid, "native")
    assert figma.js(f"return (await figma.getNodeByIdAsync('{tid}')).characters") == "native"
