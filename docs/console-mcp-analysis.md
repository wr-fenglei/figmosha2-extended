# figma-console-mcp 实现分析与 figmosha 移植

分析对象：southleft/figma-console-mcp **v1.40.9**（commit `7f0d8bf`），本地 stdio 模式（`src/local.ts`）实际暴露的 **121 个工具**（与 `tools/list` 实测输出一一对应，见 `tools/data/console-mcp-tools-list.json`）。
完整的逐工具数据（分类、定义 file:line、插件命令及其 code.js 行号、REST 端点）：`tools/data/console_mcp_tools.json` / `.csv`，由 `tools/analyze_console_mcp.py` 从源码静态生成。

## 1. 工具定义在哪、长什么样

- 所有工具都用 MCP TS SDK 的 `server.tool(name, description, zodShape, handler)` 四参数形式注册（没有 `registerTool`，没有手写 JSON Schema）。`zodShape` 是一个 `{ 参数名: z.string().describe(...) ... }` 对象，SDK 把它转成 JSON Schema（`tools/list` 里看到的 `inputSchema`，`.default()` 会变成 `"default"`）。handler 是 `async (args) => ({ content: [{ type: "text", text: JSON.stringify(...) }], isError? })`。
- 注册位置：
  - `src/local.ts` 的 `LocalModeServer.registerTools()`（第 447 行起）直接注册 16 个（控制台日志、截图、状态、选区、设计系统摘要、组件搜索等），然后依次调用各模块的 `registerXxxTools(server, getFigmaAPI, getCurrentUrl, ..., getDesktopConnector)`（3095–3260 行、3795 行）。
  - 模块文件（数量）：`write-tools.ts` 31、`slides-tools.ts` 17、`figjam-tools.ts` 10、`figma-tools.ts` 9、`design-system-extraction-tools.ts` 7、`version-tools.ts` 6、`slot-tools.ts` 5、`annotation-tools.ts` 3、`comment-tools.ts` 3、`library-tools.ts` 3、`deep-component-tools.ts` 2、`design-code-tools.ts` 2、`tokens-tools.ts` 2、`accessibility-tools.ts` / `design-system-tools.ts` / `diagnose-tool.ts` / `multi-file-tools.ts` 各 1、`apps/design-system-dashboard/server.ts` 1（`figma_audit_design_system_report`）。
  - `src/index.ts` 是 Cloudflare 云模式入口，只重复注册了 7 个 local.ts 已有的工具外加 `figma_pair_plugin`，本地模式不使用，分析时排除。
- handler 通常有三层：
  1. **TS handler**（如 `write-tools.ts:1780 figma_set_fills`）：参数校验、错误包装；
  2. **连接器方法**（`src/core/websocket-connector.ts`，如 `setNodeFills()` → `wsServer.sendCommand('SET_NODE_FILLS', {nodeId, fills})`），少数会改名参数（`addComponentProperty` 把 `type` 改为 `propertyType`，`setTextContent` 把 characters 放进 `text`）；
  3. **插件端命令处理器**（`figma-desktop-bridge/code.js` 里 `else if (msg.type === 'SET_NODE_FILLS') {...}`），这才是真正调 Plugin API 的代码，也是可移植的部分。
- REST 走 `src/core/figma-api.ts` 的 `FigmaAPI` 类（`request()` → `https://api.figma.com/v1` + `X-Figma-Token`），方法如 `getFile → /files/:key`、`getImages → /images/:key`、`getComments → /files/:key/comments`、`getFileVersions → /files/:key/versions`。

## 2. 传输分类（逐工具读 handler 判定）

判定规则（`tools/analyze_console_mcp.py`）：按括号配对切出每个 `server.tool(...)` 调用体，找连接器方法调用 / `sendCommand` / `executeCodeViaUI`（→ 插件），`api.getXxx()` / `getFigmaAPI` / `api.figma.com`（→ REST）。两者都有记为 HYBRID，都没有记为 LOCAL。13 个工具的通道藏在外部辅助函数里（如 `ensureDesignSystemCache()`、`computeDiff()`、`handleExport()`），已逐个读源码后写进 `OVERRIDES` 表并附说明。

| 分类 | 数量 | 含义 | 例子 |
|---|---|---|---|
| **PLUGIN** | **82** | 只走 Desktop Bridge 插件 | `figma_set_fills`、`figma_create_child`、`figma_create_variable`、`figma_lint_design`、全部 FigJam / Slides / Slot 工具 |
| **REST** | **16** | 只走 api.figma.com，要 token、耗 REST 额度 | `figma_get_file_data`、`figma_get_styles`、评论 3 个、版本历史 6 个、`figma_check_design_parity` |
| **HYBRID** | **8** | 两者都用（多为先插件、失败退 REST，或 REST 数据加插件补充） | `figma_get_variables`、`figma_get_component`、`figma_take_screenshot`、`figma_search_components`、`figma_get_design_system_kit` |
| **LOCAL** | **15** | 不碰 Figma：MCP 服务端缓冲区、已连接文件列表、本地代码库、axe-core | 控制台日志 3 个、`figma_navigate`、`figma_list_open_files`、`figma_ds_*` 7 个、`figma_scan_code_accessibility` |

PLUGIN 的 82 个里再细分：
- **63 个**走一个命名的插件命令（`SET_NODE_FILLS`、`CREATE_VARIABLE` ……），`code.js` 里有独立 handler；
- **约 17 个**走 `EXECUTE_CODE`：TS 里用模板字符串拼好 Plugin API 代码再送进插件 eval（`figma_batch_create_variables`、`figma_setup_design_tokens`、`figma_arrange_component_set`、`figma_get_library_variables` 等），插件里没有对应的独立 handler；
- 另有 `figma_export_tokens` / `figma_import_tokens` 走插件 UI 缓存的变量数据（`GET_VARIABLES_DATA`）加大量 TS 端逻辑。

## 3. 插件端怎么接收和执行

**figma-console-mcp（Desktop Bridge）**
1. MCP 服务端（Node）是 WebSocket **服务端**，端口 9223–9232；插件 `ui.html` 是**客户端**，逐个端口扫描连接，握手收到 `SERVER_HELLO`。
2. 请求格式 `{id, method, params}`。`ui.html` 里有一张 74 项的 `methodMap`（`'SET_NODE_FILLS': params => window.setNodeFills(...)`），每项调用 `window.sendPluginCommand(type, params)` → `parent.postMessage({pluginMessage: {type, requestId, ...params}})` 进入沙箱。少数在 UI 层预处理（`SET_IMAGE_FILL` 在 iframe 里用 `atob` 把 base64 转成字节数组 `imageBytes`；`GET_VARIABLES_DATA` 直接返回 UI 缓存）。
3. 沙箱 `code.js` 只有一个 `figma.ui.onmessage`，内部是一条 **74 个分支的 `if / else if (msg.type === ...)` 链**（第 489–7221 行，约 6,700 行），每个分支 `try { ...Plugin API... ; figma.ui.postMessage({type: 'X_RESULT', requestId, success: true, ...}) } catch { postMessage({... success: false, error}) }`。
4. 只有 `EXECUTE_CODE` 一个分支是通用执行：`eval("(async function() {\n" + code + "\n})()")`，与超时 `Promise.race`，再做 `resultAnalysis`（空数组 / null / undefined 时给警告），附带 `fileContext`。注释写明 Figma 沙箱限制 AsyncFunction 构造器，所以用 eval。
5. `ui.html` 收到 `X_RESULT` 后按 requestId 找回 promise，再 `ws.send({id, result})` 回服务端。

**figmosha2**
1. 反过来：`bridge.py`（aiohttp）是服务端，`127.0.0.1:8787`，插件 UI 连 `ws://localhost:8787/plugin`，发 `hello`（文件名、docSig）；HTTP 侧 `POST /exec {code, target?, timeout}`。
2. 只有**一种**消息 `exec`：沙箱里 `new Function('figma','print','h', 'return (async () => { CODE })();')`，注入 `print`（日志批量流式回传）和 `h`（22 个辅助函数，含协作式取消 `h.ck()`）。结果只序列化一次（`valueJson` 原样拼进 WebSocket 帧）。
3. 每个文件一条队列（文档级锁）、多文件路由（`-T`）、超时后的“遗留脚本”互锁，都在 bridge 里。

**对比**：console-mcp 是“**74 个预制命令 + 1 个通用 eval**”，逻辑写死在插件里，改一个命令要重新导入插件；figmosha 是“**只有通用 exec**”，所有逻辑由调用方发来。所以 console-mcp 插件里那 63 个命令处理器本身就是一份现成的、经过实战的 Plugin API 代码库，把它们从 if/else 链里切出来，套上 `msg` 参数，就能原样在 figmosha 的 exec 里跑——这正是转换脚本做的事。

## 4. 转换脚本

- 位置：`tools/convert_console_mcp.py`（只用标准库），分析部分在 `tools/analyze_console_mcp.py`。
- 输出：`figmosha_ext/commands.json`（生成物，提交入库，figmosha 运行时不需要 console-mcp 源码）；运行时 `figmosha_ext/__init__.py`（手写，约 200 行）；`figmosha.py` 加了约 20 行钩子（`KNOWN_CMDS` 并入新命令名、`build_parser()` 注册子命令、`main()` 分派）。`figmosha_ext` 缺失或损坏时核心 CLI 不受影响。
- 生成方式：
  - **PORTED（61）**：从 `code.js` 切出该命令的整个分支块，自动带上它引用的顶层辅助函数（依赖闭包，如 `hexToFigmaRGB`、`loadFontsForNode`、`loadFontWithFallback`、`serializeVariable`、`__editorType`），把 `figma.ui.postMessage(` 换成本地收集器，最后取 `X_RESULT`：成功返回其余字段，`success:false` 时抛错（figmosha 会带上堆栈与 hint）。
  - **COMPOSITE（1）**：`update-variable` 按参数有无依次跑 `UPDATE_VARIABLE` 和 `SET_VARIABLE_DESCRIPTION` 两个处理器。
  - **HAND（1）**：`get-selection`（console-mcp 读的是服务端缓存，没有插件处理器可搬）。
  - **MANUAL（14）**：TS 端拼代码（EXECUTE_CODE）或纯辅助路径，未生成，只列出。
  - **COVERED（5）**：`figma_execute`→`exec`，`figma_execute_across_files`→`exec -T`，`figma_get_status`/`figma_reconnect`→`status`/`doctor`，`figma_reload_plugin`（只对 console-mcp 自己的 UI 有意义）。
  - REST / HYBRID / LOCAL 共 39 个不生成。
- 参数：由 JSON Schema 生成 argparse。必填参数按 schema 顺序做位置参数；可选参数做 `--kebab-flag`；`number`/`integer`/`boolean` 转类型（布尔用 `--x/--no-x`）；`enum` → `choices`；数组 / 对象 → JSON 字符串；联合类型 → 先试 JSON 再当字符串；schema 的 `default` 会像 MCP SDK 一样补上（如 `resize` 的 `withConstraints=true`）。与通用参数重名的（`timeout`）改为 `--param-timeout`。
- 安全：参数整体 `json.dumps` 两次，作为一个 JS 字符串字面量交给 `JSON.parse`，从不拼进代码（测试里用含 `"); figma.root.remove();`、反引号、`${}`、换行、`</script>` 的名字验证过）。
- 映射表（脚本顶部，可改后重跑）：`NAME_OVERRIDES`、`ALIASES`（`fill`、`stroke`、`resize`、`move`、`rename`、`create`、`delete-node`、`selection`）、`RENAMES`（参数改名）、`PRE_JS`（替代 ui.html 的预处理，如 base64 → 字节）、`SUGAR`（`fill <id> '#ff0000'` 或 `'#f00,#00ff0080'` 简写）、`COMPOSITE`、`COVERED`、`HAND_TEMPLATES`。
- 节点参数支持 figmosha 的 `page` / `sel` 别名（`fill sel '#fff'`、`create page FRAME`）。`--print-js` 只打印将要执行的代码。

用法：
```bash
python tools/analyze_console_mcp.py --src ../figma-console-mcp     # 重新分类 → tools/data/console_mcp_tools.{json,csv}
python tools/convert_console_mcp.py --src ../figma-console-mcp     # 重新生成 figmosha_ext/commands.json

python figmosha.py create page FRAME --properties '{"name":"Card","width":320,"height":200}'
python figmosha.py fill 1:4 '#ff0000'
python figmosha.py resize 1:4 400 250 && python figmosha.py move 1:4 100 50
python figmosha.py set-text 1:5 "你好" --font-style "Semi Bold" --font-size 18
python figmosha.py create-variable-collection Brand --initial-mode-name Light --additional-modes '["Dark"]'
python figmosha.py create-variable color/primary VariableCollectionId:1:1 COLOR --values-by-mode '{"1:0":"#3366ff"}'
python figmosha.py lint-design --node-id 1:4
python figmosha.py set-fills --help        # 帮助文本来自 MCP 工具描述
```

## 5. 测试（模拟插件，不是真 Figma）

`tests/fake_figma/run_plugin.js` 在 Node `vm` 里加载**真实的** `plugin/code.js`，并扮演 `ui.html`（WebSocket 连真实 `bridge.py`）；`tests/fake_figma/mock_figma.js` 是内存版 Plugin API（节点树、fills/strokes、resize、clone/remove、文本与字体加载检查、变量与集合、`exportAsync` 返回 1×1 PNG、`base64Encode/Decode`、`createImage`）。`tests/test_console_mcp_port.py` 共 24 个用例，全部通过，覆盖：建 frame、hex/JSON/8 位 hex 填充、描边、resize/move/rename、恶意字符串改名、建文本与 set-text（字体名 "SemiBold" 自动纠正为 "Semi Bold"、未知字体报错、非文本节点报错）、tree 与 get-selection（含 `sel` 别名）、clone/delete、节点不存在报错、变量全流程（建集合、建 COLOR 变量、更新、改名、绑定到填充、删除）、截图、组件描述、`lint-design`（1,200 行的处理器原样跑通）、`set-image-fill`（沙箱内 base64 解码）、FigJam 命令在设计文件中被拒、原生命令不受影响；另有离线用例检查生成物可确定性重建、参数默认值与改名、JSON 嵌入。所有 63 个模板都通过了 JS 语法检查。

**局限**：模拟对象不验证布局、约束、auto layout、渲染、字体可用性、权限等真实行为；它证明的是 CLI → bridge → 插件 → 模板 → 结果这条管线和移植代码的逻辑，不能代替在 Figma 桌面版里的验证。Slides / FigJam / 组件 / Slot / 注释类命令只做了语法检查，没有专门的模拟。

## 6. 发现的问题与建议

- **上游 bug 1**：`figma_add_shape_to_slide` 的 TS 端传 `fillColor`，插件处理器读 `msg.color`，填充色在 console-mcp 里被静默丢弃。移植版通过 `RENAMES` 修正为 `color`。
- **上游 bug 2**：`SET_NODE_FILLS` 用 `hexToFigmaRGB` 的 alpha（6 位 hex 恒为 1）作 opacity，`opacity` 参数在给了 `color` 时永远无效。移植版保持原样（测试里有注释），需要半透明请用 8 位 hex。
- 6 个移植命令的 TS handler 超过 90 行（`instantiate-component`、`add-component-property`、`capture-screenshot`、`add-text-to-slide`、`update-variable`、`get-selection`），TS 端的额外校验 / 结果整形没有移植，行为可能与 MCP 版略有差异。
- `capture-screenshot` 返回 base64 走 figmosha 的 16 MB 上限；大图建议直接写 `exportAsync` 脚本。
- 14 个 MANUAL 工具里值得优先手写的：`figma_batch_create_variables` / `figma_batch_update_variables`（批量变量）、`figma_create_component_set`、`figma_arrange_component_set`、`figma_setup_design_tokens`。它们的代码在 TS 模板字符串里，带 TS 变量插值，自动提取不可靠。
- 上游更新时：重跑两个脚本即可；`test_regeneration_is_deterministic` 会在生成物过期时失败。若 code.js 结构变化（不再是 `msg.type === 'X'` 分支），提取会报 `MANUAL: handler not found` 而不会生成坏模板。
- 许可：console-mcp 是 MIT，`commands.json` 内含其插件代码片段，发布前应在 README / LICENSE 中注明来源与版权。

## 附：121 个工具逐个分类

### PLUGIN（82）

| 工具 | 定义位置 | 通道细节 | figmosha 命令 |
|---|---|---|---|
| `figjam_auto_arrange` | src/core/figjam-tools.ts:439 | EXECUTE_CODE（TS 拼代码） | 需手写模板 |
| `figjam_create_code_block` | src/core/figjam-tools.ts:391 | `CREATE_CODE_BLOCK`@code.js:6303 | `figjam-create-code-block` [PORTED] |
| `figjam_create_connector` | src/core/figjam-tools.ts:166 | `CREATE_CONNECTOR`@code.js:6069 | `figjam-create-connector` [PORTED] |
| `figjam_create_section` | src/core/figjam-tools.ts:291 | `CREATE_SECTION`@code.js:6125 | `figjam-create-section` [PORTED] |
| `figjam_create_shape_with_text` | src/core/figjam-tools.ts:227 | `CREATE_SHAPE_WITH_TEXT`@code.js:6174 | `figjam-create-shape-with-text` [PORTED] |
| `figjam_create_stickies` | src/core/figjam-tools.ts:117 | `CREATE_STICKIES`@code.js:6011 | `figjam-create-stickies` [PORTED] |
| `figjam_create_sticky` | src/core/figjam-tools.ts:75 | `CREATE_STICKY`@code.js:5970 | `figjam-create-sticky` [PORTED] |
| `figjam_create_table` | src/core/figjam-tools.ts:333 | `CREATE_TABLE`@code.js:6259 | `figjam-create-table` [PORTED] |
| `figjam_get_board_contents` | src/core/figjam-tools.ts:549 | `GET_BOARD_CONTENTS`@code.js:6348 | `figjam-get-board-contents` [PORTED] |
| `figjam_get_connections` | src/core/figjam-tools.ts:599 | `GET_CONNECTIONS`@code.js:6454 | `figjam-get-connections` [PORTED] |
| `figma_add_component_property` | src/core/write-tools.ts:1412 | `ADD_COMPONENT_PROPERTY`@code.js:2697 | `add-component-property` [PORTED] |
| `figma_add_mode` | src/core/write-tools.ts:542 | `ADD_MODE`@code.js:915 | `add-mode` [PORTED] |
| `figma_add_shape_to_slide` | src/core/slides-tools.ts:636 | `ADD_SHAPE_TO_SLIDE`@code.js:7168 | `add-shape-to-slide` [PORTED] |
| `figma_add_slot_property` | src/core/slot-tools.ts:292 | EXECUTE_CODE（TS 拼代码） | 需手写模板 |
| `figma_add_text_to_slide` | src/core/slides-tools.ts:534 | `ADD_TEXT_TO_SLIDE`@code.js:7104 | `add-text-to-slide` [PORTED] |
| `figma_analyze_component_set` | src/core/deep-component-tools.ts:104 | `ANALYZE_COMPONENT_SET`@code.js:1130 | `analyze-component-set` [PORTED] |
| `figma_append_to_slot` | src/core/slot-tools.ts:144 | `APPEND_TO_SLOT`@code.js:2957 | `append-to-slot` [PORTED] |
| `figma_arrange_component_set` | src/core/write-tools.ts:2739 | EXECUTE_CODE（TS 拼代码） | 需手写模板 |
| `figma_audit_component_accessibility` | src/core/write-tools.ts:3109 | `AUDIT_COMPONENT_ACCESSIBILITY`@code.js:5324 | `audit-component-accessibility` [PORTED] |
| `figma_batch_create_variables` | src/core/write-tools.ts:676 | EXECUTE_CODE（TS 拼代码） | 需手写模板 |
| `figma_batch_update_variables` | src/core/write-tools.ts:821 | EXECUTE_CODE（TS 拼代码） | 需手写模板 |
| `figma_capture_screenshot` | src/core/figma-tools.ts:3836 | `CAPTURE_SCREENSHOT`@code.js:3768 | `capture-screenshot` [PORTED] |
| `figma_clone_node` | src/core/write-tools.ts:1981 | `CLONE_NODE`@code.js:3481 | `clone-node` [PORTED] |
| `figma_create_child` | src/core/write-tools.ts:2201 | `CREATE_CHILD_NODE`@code.js:3655 | `create-child` (create) [PORTED] |
| `figma_create_component_set` | src/core/write-tools.ts:2835 | `CREATE_COMPONENT_SET`@code.js:2135; EXECUTE_CODE（TS 拼代码） | 需手写模板 |
| `figma_create_slide` | src/core/slides-tools.ts:285 | `CREATE_SLIDE`@code.js:6619 | `create-slide` [PORTED] |
| `figma_create_slot` | src/core/slot-tools.ts:33 | `CREATE_SLOT`@code.js:2838 | `create-slot` [PORTED] |
| `figma_create_variable` | src/core/write-tools.ts:224 | `CREATE_VARIABLE`@code.js:663 | `create-variable` [PORTED] |
| `figma_create_variable_collection` | src/core/write-tools.ts:307 | `CREATE_VARIABLE_COLLECTION`@code.js:721 | `create-variable-collection` [PORTED] |
| `figma_delete_component_property` | src/core/write-tools.ts:1602 | `DELETE_COMPONENT_PROPERTY`@code.js:2798 | `delete-component-property` [PORTED] |
| `figma_delete_node` | src/core/write-tools.ts:2031 | `DELETE_NODE`@code.js:3520 | `delete-node` [PORTED] |
| `figma_delete_slide` | src/core/slides-tools.ts:329 | `DELETE_SLIDE`@code.js:6652 | `delete-slide` [PORTED] |
| `figma_delete_variable` | src/core/write-tools.ts:373 | `DELETE_VARIABLE`@code.js:763 | `delete-variable` [PORTED] |
| `figma_delete_variable_collection` | src/core/write-tools.ts:428 | `DELETE_VARIABLE_COLLECTION`@code.js:802 | `delete-variable-collection` [PORTED] |
| `figma_duplicate_slide` | src/core/slides-tools.ts:365 | `DUPLICATE_SLIDE`@code.js:6685 | `duplicate-slide` [PORTED] |
| `figma_edit_component_property` | src/core/write-tools.ts:1523 | `EDIT_COMPONENT_PROPERTY`@code.js:2759 | `edit-component-property` [PORTED] |
| `figma_execute` | src/core/write-tools.ts:20 | EXECUTE_CODE（TS 拼代码） | 已有: figmosha exec |
| `figma_execute_across_files` | src/core/multi-file-tools.ts:16 | EXECUTE_CODE（TS 拼代码） | 已有: figmosha exec -T <file>, once per file |
| `figma_export_tokens` | src/core/tokens-tools.ts:135 | handleExport() -> connector.getVariablesFromPluginUI (GET_VARIABLES_DATA), then writes token files locally | 需手写模板 |
| `figma_focus_slide` | src/core/slides-tools.ts:780 | `FOCUS_SLIDE`@code.js:6937 | `focus-slide` [PORTED] |
| `figma_get_annotation_categories` | src/core/annotation-tools.ts:234 | `GET_ANNOTATION_CATEGORIES`@code.js:2666 | `get-annotation-categories` [PORTED] |
| `figma_get_annotations` | src/core/annotation-tools.ts:97 | `GET_ANNOTATIONS`@code.js:2454 | `get-annotations` [PORTED] |
| `figma_get_component_details` | src/local.ts:2499 | `GET_LOCAL_COMPONENTS`@code.js:1692; EXECUTE_CODE（TS 拼代码）; ensureDesignSystemCache() -> connector.getVariables (EXECUTE_CODE) + getLocalComponents (GET_LOCAL_COMPONENTS) | 需手写模板 |
| `figma_get_component_for_development_deep` | src/core/deep-component-tools.ts:24 | `DEEP_GET_COMPONENT`@code.js:1429 | `get-component-for-development-deep` [PORTED] |
| `figma_get_design_system_summary` | src/local.ts:1963 | `GET_LOCAL_COMPONENTS`@code.js:1692; EXECUTE_CODE（TS 拼代码） | 需手写模板 |
| `figma_get_focused_slide` | src/core/slides-tools.ts:250 | `GET_FOCUSED_SLIDE`@code.js:6908 | `get-focused-slide` [PORTED] |
| `figma_get_library_variables` | src/core/library-tools.ts:474 | EXECUTE_CODE（TS 拼代码） | 需手写模板 |
| `figma_get_selection` | src/local.ts:1526 | EXECUTE_CODE（TS 拼代码） | `get-selection` (selection) [HAND] |
| `figma_get_slide_content` | src/core/slides-tools.ts:117 | `GET_SLIDE_CONTENT`@code.js:6574 | `get-slide-content` [PORTED] |
| `figma_get_slide_grid` | src/core/slides-tools.ts:153 | `GET_SLIDE_GRID`@code.js:6717 | `get-slide-grid` [PORTED] |
| `figma_get_slide_transition` | src/core/slides-tools.ts:184 | `GET_SLIDE_TRANSITION`@code.js:6850 | `get-slide-transition` [PORTED] |
| `figma_get_slots` | src/core/slot-tools.ts:101 | `GET_SLOTS`@code.js:2904 | `get-slots` [PORTED] |
| `figma_get_status` | src/local.ts:1174 | `GET_FILE_INFO`@code.js:3938; EXECUTE_CODE（TS 拼代码）; connection state + GET_FILE_INFO / EXECUTE_CODE probe | 已有: figmosha status / doctor |
| `figma_get_text_styles` | src/core/slides-tools.ts:220 | `GET_TEXT_STYLES`@code.js:7002 | `get-text-styles` [PORTED] |
| `figma_get_token_values` | src/local.ts:2640 | `GET_LOCAL_COMPONENTS`@code.js:1692; EXECUTE_CODE（TS 拼代码）; ensureDesignSystemCache() -> connector.getVariables + getLocalComponents | 需手写模板 |
| `figma_import_library_variable` | src/core/library-tools.ts:665 | EXECUTE_CODE（TS 拼代码） | 需手写模板 |
| `figma_import_tokens` | src/core/tokens-tools.ts:171 | — | 需手写模板 |
| `figma_instantiate_component` | src/core/write-tools.ts:1223 | `INSTANTIATE_COMPONENT`@code.js:1899 | `instantiate-component` [PORTED] |
| `figma_lint_design` | src/core/write-tools.ts:3048 | `LINT_DESIGN`@code.js:4110 | `lint-design` [PORTED] |
| `figma_list_slides` | src/core/slides-tools.ts:78 | `LIST_SLIDES`@code.js:6530 | `list-slides` [PORTED] |
| `figma_move_node` | src/core/write-tools.ts:1728 | `MOVE_NODE`@code.js:3160 | `move-node` (move) [PORTED] |
| `figma_reconnect` | src/local.ts:1414 | `RELOAD_UI`@code.js:3985; EXECUTE_CODE（TS 拼代码） | 已有: figmosha doctor (the bridge reconnects by itself) |
| `figma_reload_plugin` | src/local.ts:862 | `RELOAD_UI`@code.js:3985 | 已有: RELOAD_UI reloads console-mcp's own plugin UI; re-Run Figmosha Bridge instead |
| `figma_rename_mode` | src/core/write-tools.ts:601 | `RENAME_MODE`@code.js:954 | `rename-mode` [PORTED] |
| `figma_rename_node` | src/core/write-tools.ts:2081 | `RENAME_NODE`@code.js:3557 | `rename-node` (rename) [PORTED] |
| `figma_rename_variable` | src/core/write-tools.ts:483 | `RENAME_VARIABLE`@code.js:842 | `rename-variable` [PORTED] |
| `figma_reorder_slides` | src/core/slides-tools.ts:401 | `REORDER_SLIDES`@code.js:6756 | `reorder-slides` [PORTED] |
| `figma_reset_slot` | src/core/slot-tools.ts:234 | `RESET_SLOT`@code.js:3081 | `reset-slot` [PORTED] |
| `figma_resize_node` | src/core/write-tools.ts:1664 | `RESIZE_NODE`@code.js:3117 | `resize-node` (resize) [PORTED] |
| `figma_set_annotations` | src/core/annotation-tools.ts:163 | `SET_ANNOTATIONS`@code.js:2569 | `set-annotations` [PORTED] |
| `figma_set_description` | src/core/write-tools.ts:1348 | `SET_NODE_DESCRIPTION`@code.js:2410 | `set-description` [PORTED] |
| `figma_set_fills` | src/core/write-tools.ts:1780 | `SET_NODE_FILLS`@code.js:3200 | `set-fills` (fill) [PORTED] |
| `figma_set_image_fill` | src/core/write-tools.ts:1855 | `SET_IMAGE_FILL`@code.js:3267 | `set-image-fill` [PORTED] |
| `figma_set_instance_properties` | src/core/figma-tools.ts:3942 | `SET_INSTANCE_PROPERTIES`@code.js:4012 | `set-instance-properties` [PORTED] |
| `figma_set_slide_background` | src/core/slides-tools.ts:701 | `SET_SLIDE_BACKGROUND`@code.js:7042 | `set-slide-background` [PORTED] |
| `figma_set_slide_transition` | src/core/slides-tools.ts:439 | `SET_SLIDE_TRANSITION`@code.js:6809 | `set-slide-transition` [PORTED] |
| `figma_set_slides_view_mode` | src/core/slides-tools.ts:745 | `SET_SLIDES_VIEW_MODE`@code.js:6880 | `set-slides-view-mode` [PORTED] |
| `figma_set_strokes` | src/core/write-tools.ts:1905 | `SET_NODE_STROKES`@code.js:3334 | `set-strokes` (stroke) [PORTED] |
| `figma_set_text` | src/core/write-tools.ts:2132 | `SET_TEXT_CONTENT`@code.js:3593 | `set-text` [PORTED] |
| `figma_setup_design_tokens` | src/core/write-tools.ts:950 | EXECUTE_CODE（TS 拼代码） | 需手写模板 |
| `figma_skip_slide` | src/core/slides-tools.ts:495 | `SKIP_SLIDE`@code.js:6970 | `skip-slide` [PORTED] |
| `figma_update_variable` | src/core/write-tools.ts:118 | `UPDATE_VARIABLE`@code.js:612, `SET_VARIABLE_DESCRIPTION`@code.js:880 | `update-variable` [COMPOSITE] |

### HYBRID（8）

| 工具 | 定义位置 | 通道细节 | figmosha 命令 |
|---|---|---|---|
| `figma_audit_design_system_report` | src/apps/design-system-dashboard/server.ts:246 | fetchDesignSystemAuditData: cache -> Desktop Bridge -> REST fallback chain | — |
| `figma_generate_component_doc` | src/core/design-code-tools.ts:4139 | `GET_COMPONENT`@code.js:1043; EXECUTE_CODE（TS 拼代码）; REST `/files/${fileKey}/nodes`, `/files/${fileKey}/components` | — |
| `figma_get_component` | src/core/figma-tools.ts:2601 | `GET_COMPONENT`@code.js:1043; REST `/files/${fileKey}/nodes` | — |
| `figma_get_component_for_development` | src/core/figma-tools.ts:3206 | `GET_ANNOTATIONS`@code.js:2454, `GET_COMPONENT`@code.js:1043; REST `/files/${fileKey}/nodes`, `/images/${fileKey}` | — |
| `figma_get_design_system_kit` | src/core/design-system-tools.ts:582 | REST `/files/${fileKey}/components`, `/files/${fileKey}/component_sets`, `/files/${fileKey}/nodes`, `/images/${fileKey}` | — |
| `figma_get_variables` | src/core/figma-tools.ts:1151 | `GET_VARIABLES_DATA`@code.js:None; EXECUTE_CODE（TS 拼代码）; REST `/files/${fileKey}/variables/local`, `/files/${fileKey}/variables/published`, `/files/${fileKey}/styles` | — |
| `figma_search_components` | src/local.ts:2202 | `GET_LOCAL_COMPONENTS`@code.js:1692; EXECUTE_CODE（TS 拼代码）; REST `/files/${fileKey}/components` | — |
| `figma_take_screenshot` | src/local.ts:556 | `CAPTURE_SCREENSHOT`@code.js:3768; REST `/images/${fileKey}` | — |

### REST（16）

| 工具 | 定义位置 | 通道细节 | figmosha 命令 |
|---|---|---|---|
| `figma_blame_node` | src/core/version-tools.ts:1034 | — | — |
| `figma_check_design_parity` | src/core/design-code-tools.ts:3919 | REST `/files/${fileKey}/nodes`, `/files/${fileKey}/components` | — |
| `figma_delete_comment` | src/core/comment-tools.ts:262 | REST `/files/${fileKey}/comments/${commentId}` | — |
| `figma_diff_versions` | src/core/version-tools.ts:874 | computeDiff() -> api.getFileVersions / getFile | — |
| `figma_generate_changelog` | src/core/version-tools.ts:939 | — | — |
| `figma_get_changes_since_version` | src/core/version-tools.ts:905 | computeDiff() -> api.getFileVersions / getFile | — |
| `figma_get_comments` | src/core/comment-tools.ts:29 | REST `/files/${fileKey}/comments${query}` | — |
| `figma_get_component_image` | src/core/figma-tools.ts:3053 | REST `/files/${fileKey}/nodes`, `/images/${fileKey}` | — |
| `figma_get_file_at_version` | src/core/version-tools.ts:375 | REST `/files/${fileKey}/nodes`, `/files/${fileKey}` | — |
| `figma_get_file_data` | src/core/figma-tools.ts:883 | REST `/files/${fileKey}`, `/files/${fileKey}/nodes` | — |
| `figma_get_file_for_plugin` | src/core/figma-tools.ts:3623 | REST `/files/${fileKey}`, `/files/${fileKey}/nodes` | — |
| `figma_get_file_versions` | src/core/version-tools.ts:194 | REST `/files/${fileKey}/versions${query}` | — |
| `figma_get_library_component_by_key` | src/core/library-tools.ts:129 | REST `/component_sets/${key}`, `/components/${key}`, `/files/${fileKey}/nodes`, `/files/${fileKey}/components` | — |
| `figma_get_library_components` | src/local.ts:2757 | REST `/files/${fileKey}/components`, `/files/${fileKey}/component_sets` | — |
| `figma_get_styles` | src/core/figma-tools.ts:2868 | REST `/files/${fileKey}/styles` | — |
| `figma_post_comment` | src/core/comment-tools.ts:136 | REST `/files/${fileKey}/comments` | — |

### LOCAL（15）

| 工具 | 定义位置 | 通道细节 | figmosha 命令 |
|---|---|---|---|
| `figma_clear_console` | src/local.ts:943 | wsServer.clearConsoleLogs() | — |
| `figma_diagnose` | src/core/diagnose-tool.ts:122 | buildReport(): local environment / connection diagnostics | — |
| `figma_ds_analyze` | src/core/design-system-extraction-tools.ts:93 | 本地: codebase | — |
| `figma_ds_extract_component` | src/core/design-system-extraction-tools.ts:535 | 本地: loadAnalysis, readFile | — |
| `figma_ds_extract_tokens` | src/core/design-system-extraction-tools.ts:219 | 本地: codebase, loadAnalysis, writeFile | — |
| `figma_ds_scaffold` | src/core/design-system-extraction-tools.ts:408 | 本地: loadAnalysis, readFile | — |
| `figma_ds_setup_storybook` | src/core/design-system-extraction-tools.ts:495 | 本地: loadAnalysis | — |
| `figma_ds_status` | src/core/design-system-extraction-tools.ts:690 | 本地: loadAnalysis | — |
| `figma_ds_verify` | src/core/design-system-extraction-tools.ts:663 | 本地: loadAnalysis | — |
| `figma_get_console_logs` | src/local.ts:450 | wsServer.getConsoleLogs(): MCP-side buffer of plugin console.* (pushed as CONSOLE_CAPTURE) | — |
| `figma_get_design_changes` | src/local.ts:1631 | wsServer.getDocumentChanges(): buffered documentchange events pushed by the plugin | — |
| `figma_list_open_files` | src/local.ts:1715 | — | — |
| `figma_navigate` | src/local.ts:1006 | switches the active file among connected plugin sockets (wsServer.getConnectedFiles) | — |
| `figma_scan_code_accessibility` | src/core/accessibility-tools.ts:328 | 本地: axe, jsdom | — |
| `figma_watch_console` | src/local.ts:797 | polls the MCP-side console buffer | — |
