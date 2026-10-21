// A small in-memory stand-in for the Figma Plugin API, enough to run
// figmosha's plugin/code.js and the handlers ported from figma-console-mcp.
// It is NOT Figma: geometry, constraints, auto layout, fonts, rendering and
// most validation are faked or missing. Tests built on it prove the
// plumbing (CLI -> bridge -> plugin -> template -> result), not real
// Plugin API behaviour.
'use strict';

function createMockFigma(opts) {
  opts = opts || {};
  let nextId = 2;
  const nodes = new Map();
  const variables = new Map();
  const collections = new Map();
  const mixed = Symbol('figma.mixed');
  const loadedFonts = new Set();
  const availableFonts = new Set(opts.fonts || ['Inter||Regular', 'Inter||Bold', 'Inter||Semi Bold']);
  const newId = () => `1:${nextId++}`;

  class BaseNode {
    constructor(type, name) {
      this.id = newId();
      this.type = type;
      this.name = name || type.charAt(0) + type.slice(1).toLowerCase();
      this.parent = null;
      this.removed = false;
      this._plugin = {};
      nodes.set(this.id, this);
    }
    getPluginData(k) { return this._plugin[k] || ''; }
    setPluginData(k, v) { this._plugin[k] = String(v); }
    remove() {
      if (this.parent) {
        const i = this.parent.children.indexOf(this);
        if (i >= 0) this.parent.children.splice(i, 1);
      }
      const drop = (n) => { n.removed = true; nodes.delete(n.id); (n.children || []).forEach(drop); };
      drop(this);
      this.parent = null;
    }
  }

  function addChildren(cls) {
    cls.prototype.appendChild = function (child) {
      if (child.parent) {
        const i = child.parent.children.indexOf(child);
        if (i >= 0) child.parent.children.splice(i, 1);
      }
      this.children.push(child);
      child.parent = this;
    };
    cls.prototype.insertChild = function (index, child) {
      if (child.parent) {
        const i = child.parent.children.indexOf(child);
        if (i >= 0) child.parent.children.splice(i, 1);
      }
      this.children.splice(index, 0, child);
      child.parent = this;
    };
    cls.prototype.findAll = function (pred) {
      const out = [];
      const walk = (n) => { for (const c of n.children || []) { if (!pred || pred(c)) out.push(c); walk(c); } };
      walk(this);
      return out;
    };
    cls.prototype.findOne = function (pred) { return this.findAll(pred)[0] || null; };
    cls.prototype.findChildren = function (pred) { return this.children.filter(pred || (() => true)); };
    cls.prototype.findChild = function (pred) { return this.children.find(pred) || null; };
    cls.prototype.findAllWithCriteria = function (c) {
      return this.findAll((n) => !c || !c.types || c.types.includes(n.type));
    };
  }

  class SceneNode extends BaseNode {
    constructor(type, name) {
      super(type, name);
      this.x = 0; this.y = 0; this.width = 100; this.height = 100;
      this.visible = true; this.locked = false; this.opacity = 1; this.rotation = 0;
      this.fills = type === 'FRAME' ? [{ type: 'SOLID', color: { r: 1, g: 1, b: 1 }, opacity: 1 }] : [];
      this.strokes = []; this.strokeWeight = 1; this.effects = [];
      this.boundVariables = {};
      this.description = '';
    }
    resize(w, h) {
      if (!(w >= 0.01) || !(h >= 0.01)) throw new Error('resize: width and height must be >= 0.01');
      this.width = w; this.height = h;
    }
    resizeWithoutConstraints(w, h) { this.resize(w, h); this._noConstraints = true; }
    get absoluteBoundingBox() { return { x: this.x, y: this.y, width: this.width, height: this.height }; }
    get absoluteTransform() { return [[1, 0, this.x], [0, 1, this.y]]; }
    clone() {
      const c = new this.constructor(this.type, this.name);
      for (const k of ['x', 'y', 'width', 'height', 'visible', 'opacity', 'strokeWeight', 'cornerRadius',
                       'characters', 'fontSize', 'layoutMode', 'itemSpacing', 'description']) {
        if (k in this) c[k] = this[k];
      }
      c.fills = JSON.parse(JSON.stringify(this.fills));
      c.strokes = JSON.parse(JSON.stringify(this.strokes));
      if (this.type === 'TEXT') c._font = this._font;
      if (this.children) for (const ch of this.children) c.appendChild(ch.clone());
      if (this.parent) this.parent.insertChild(this.parent.children.indexOf(this) + 1, c);
      return c;
    }
    async exportAsync(settings) {
      const fmt = (settings && settings.format) || 'PNG';
      // 1x1 transparent PNG (or a tiny fake for other formats)
      const png = [137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 13, 73, 72, 68, 82, 0, 0, 0, 1, 0, 0, 0, 1, 8, 6, 0, 0, 0, 31, 21, 196, 137, 0, 0, 0, 10, 73, 68, 65, 84, 120, 156, 99, 0, 1, 0, 0, 5, 0, 1, 13, 10, 45, 180, 0, 0, 0, 0, 73, 69, 78, 68, 174, 66, 96, 130];
      if (fmt === 'SVG_STRING') return '<svg xmlns="http://www.w3.org/2000/svg"/>';
      return Uint8Array.from(fmt === 'PNG' ? png : [37, 80, 68, 70]);
    }
    setBoundVariable(field, v) { this.boundVariables[field] = v ? { type: 'VARIABLE_ALIAS', id: v.id } : undefined; }
  }

  class FrameNode extends SceneNode {
    constructor(type, name) {
      super(type || 'FRAME', name || 'Frame');
      this.children = [];
      this.layoutMode = 'NONE'; this.itemSpacing = 0; this.cornerRadius = 0;
      this.paddingLeft = this.paddingRight = this.paddingTop = this.paddingBottom = 0;
      this.clipsContent = true;
    }
  }
  addChildren(FrameNode);

  class TextNode extends SceneNode {
    constructor() {
      super('TEXT', 'Text');
      this._chars = '';
      this._font = { family: 'Inter', style: 'Regular' };
      this.fontSize = 12;
      this.fills = [{ type: 'SOLID', color: { r: 0, g: 0, b: 0 }, opacity: 1 }];
    }
    _requireFont(f) {
      if (!loadedFonts.has(f.family + '||' + f.style)) {
        throw new Error(`in set_characters: Cannot write to node with unloaded font "${f.family} ${f.style}". Please call figma.loadFontAsync({ family: "${f.family}", style: "${f.style}" }) and await the returned promise first.`);
      }
    }
    get characters() { return this._chars; }
    set characters(v) { this._requireFont(this._font); this._chars = String(v); }
    get fontName() { return this._font; }
    set fontName(f) { this._requireFont(f); this._font = { family: f.family, style: f.style }; }
    getRangeAllFontNames() { return [this._font]; }
  }

  class PageNode extends BaseNode {
    constructor(name) { super('PAGE', name || 'Page 1'); this.children = []; this.selection = []; this.backgrounds = []; }
  }
  addChildren(PageNode);

  class DocumentNode extends BaseNode {
    constructor(name) { super('DOCUMENT', name); this.children = []; }
  }
  addChildren(DocumentNode);
  const root = new DocumentNode(opts.fileName || 'Mock File');
  nodes.delete(root.id);
  root.id = '0:0';
  nodes.set('0:0', root);
  const page = new PageNode('Page 1');
  root.appendChild(page);

  const make = (Cls, type) => () => { const n = new Cls(type); figma.currentPage.appendChild(n); return n; };

  // ── variables ────────────────────────────────────────────────────────
  let varSeq = 1;
  class Variable {
    constructor(name, collection, type) {
      this.id = `VariableID:${varSeq}:${varSeq++}`;
      this.key = 'k' + this.id.replace(/\W/g, '');
      this.name = name; this.resolvedType = type;
      this.variableCollectionId = collection.id;
      this.valuesByMode = {}; this.scopes = ['ALL_SCOPES']; this.codeSyntax = {};
      this.description = ''; this.hiddenFromPublishing = false;
      for (const m of collection.modes) this.valuesByMode[m.modeId] = defaultValue(type);
      collection.variableIds.push(this.id);
      variables.set(this.id, this);
    }
    setValueForMode(modeId, value) {
      const c = collections.get(this.variableCollectionId);
      if (!c.modes.some((m) => m.modeId === modeId)) throw new Error('Invalid modeId: ' + modeId);
      this.valuesByMode[modeId] = value;
    }
    remove() {
      const c = collections.get(this.variableCollectionId);
      if (c) c.variableIds = c.variableIds.filter((i) => i !== this.id);
      variables.delete(this.id);
    }
  }
  function defaultValue(t) { return t === 'COLOR' ? { r: 0, g: 0, b: 0, a: 1 } : t === 'FLOAT' ? 0 : t === 'BOOLEAN' ? false : ''; }
  let colSeq = 1, modeSeq = 1;
  class VariableCollection {
    constructor(name) {
      this.id = `VariableCollectionId:${colSeq}:${colSeq++}`;
      this.key = 'c' + this.id.replace(/\W/g, '');
      this.name = name; this.modes = []; this.variableIds = []; this.remote = false;
      this.defaultModeId = this.addMode('Mode 1');
      collections.set(this.id, this);
    }
    addMode(name) { const id = `${modeSeq++}:0`; this.modes.push({ modeId: id, name }); return id; }
    renameMode(id, name) { const m = this.modes.find((x) => x.modeId === id); if (!m) throw new Error('mode not found'); m.name = name; }
    removeMode(id) { this.modes = this.modes.filter((m) => m.modeId !== id); }
    remove() { this.variableIds.forEach((i) => variables.delete(i)); collections.delete(this.id); }
  }

  const uiListeners = [];
  const figma = {
    mixed,
    editorType: opts.editorType || 'figma',
    fileKey: null,
    apiVersion: '1.0.0',
    root,
    get currentPage() { return page; },
    set currentPage(p) { throw new Error('Cannot set currentPage in dynamic-page mode; use setCurrentPageAsync'); },
    async setCurrentPageAsync() {},
    async loadAllPagesAsync() {},
    showUI() {},
    openExternal() {},
    notify() {},
    ui: {
      postMessage(m) { for (const fn of uiListeners) fn(m); },
      onmessage: null,
      on() {},
      resize() {},
      _listen(fn) { uiListeners.push(fn); },
    },
    viewport: { center: { x: 0, y: 0 }, zoom: 1, scrollAndZoomIntoView() {} },
    clientStorage: { _s: {}, async getAsync(k) { return this._s[k]; }, async setAsync(k, v) { this._s[k] = v; } },
    async getNodeByIdAsync(id) { const n = nodes.get(id); return n && !n.removed ? n : null; },
    getNodeById(id) { return nodes.get(id) || null; },
    createFrame: make(FrameNode, 'FRAME'),
    createComponent: make(FrameNode, 'COMPONENT'),
    createRectangle: make(SceneNode, 'RECTANGLE'),
    createEllipse: make(SceneNode, 'ELLIPSE'),
    createLine: make(SceneNode, 'LINE'),
    createPolygon: make(SceneNode, 'POLYGON'),
    createStar: make(SceneNode, 'STAR'),
    createVector: make(SceneNode, 'VECTOR'),
    createText: () => { const n = new TextNode(); figma.currentPage.appendChild(n); return n; },
    async loadFontAsync(f) {
      const k = f.family + '||' + f.style;
      if (!availableFonts.has(k)) throw new Error(`The font "${f.family} ${f.style}" could not be loaded`);
      loadedFonts.add(k);
    },
    async listAvailableFontsAsync() { return [...availableFonts].map((k) => { const [family, style] = k.split('||'); return { fontName: { family, style } }; }); },
    createImage(bytes) { return { hash: 'img' + bytes.length, async getBytesAsync() { return bytes; } }; },
    base64Encode(bytes) { return Buffer.from(bytes).toString('base64'); },
    base64Decode(s) { return Uint8Array.from(Buffer.from(s, 'base64')); },
    group(ns, parent) { const g = new FrameNode('GROUP', 'Group'); parent.appendChild(g); ns.forEach((n) => g.appendChild(n)); return g; },
    variables: {
      createVariableCollection: (name) => new VariableCollection(name),
      createVariable: (name, collection, type) => {
        if (typeof collection === 'string') collection = collections.get(collection);
        if (!collection) throw new Error('createVariable: collection not found');
        if (!['COLOR', 'FLOAT', 'STRING', 'BOOLEAN'].includes(type)) throw new Error('createVariable: bad resolvedType ' + type);
        return new Variable(name, collection, type);
      },
      async getVariableByIdAsync(id) { return variables.get(id) || null; },
      async getVariableCollectionByIdAsync(id) { return collections.get(id) || null; },
      async getLocalVariablesAsync(t) { return [...variables.values()].filter((v) => !t || v.resolvedType === t); },
      async getLocalVariableCollectionsAsync() { return [...collections.values()]; },
      setBoundVariableForPaint(paint, field, v) { return Object.assign({}, paint, { boundVariables: { [field]: { type: 'VARIABLE_ALIAS', id: v.id } } }); },
      async importVariableByKeyAsync() { throw new Error('mock: no team libraries'); },
    },
    async importComponentByKeyAsync() { throw new Error('mock: no team libraries'); },
    async getLocalTextStylesAsync() { return []; },
    async getLocalPaintStylesAsync() { return []; },
    async getLocalEffectStylesAsync() { return []; },
    on() {}, off() {},
    _nodes: nodes, _variables: variables, _collections: collections,
  };
  return figma;
}

module.exports = { createMockFigma };
