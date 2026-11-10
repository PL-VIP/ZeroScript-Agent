// SPDX-License-Identifier: GPL-3.0-or-later
// Pure parser for Workspace Agent JSON commands and raw unified patches.
// eslint-disable-next-line no-unused-vars
const ZSParse = (() => {
  "use strict";

  const START_M = "###MCP_TOOL###";
  const END_M = "###END_MCP_TOOL###";
  const PATCH_START_RE = /###\s*PATCH\s*###/i;
  const PATCH_END_RE = /###\s*END[_\- ]?PATCH\s*###/i;
  const LUA_START_RE = /$a/;
  const LUA_END_RE = /$a/;
  const CMD_KEY_RE = /"(?:command|tool)"\s*:\s*"/;
  const DSML_RE = /<[\s\/]*[|｜][\s|｜]*DSML[\s|｜]*[|｜]/i;

  function matchBrace(text, start) {
    let depth = 0, inString = false, escaped = false;
    for (let i = start; i < text.length; i++) {
      const char = text[i];
      if (inString) {
        if (escaped) escaped = false;
        else if (char === "\\") escaped = true;
        else if (char === '"') inString = false;
      } else if (char === '"') inString = true;
      else if (char === "{") depth++;
      else if (char === "}" && --depth === 0) return i;
    }
    return -1;
  }

  function parseLoose(raw) {
    try { return JSON.parse(raw); } catch {}
    let out = "", inString = false, escaped = false;
    for (const char of raw) {
      if (inString) {
        if (escaped) { escaped = false; out += char; continue; }
        if (char === "\\") { escaped = true; out += char; continue; }
        if (char === '"') { inString = false; out += char; continue; }
        if (char === "\n") { out += "\\n"; continue; }
        if (char === "\r") { out += "\\r"; continue; }
        if (char === "\t") { out += "\\t"; continue; }
      } else if (char === '"') inString = true;
      out += char;
    }
    return JSON.parse(out);
  }

  function normalizeCall(value) {
    if (!value || typeof value !== "object") return null;
    const tool = value.command != null ? value.command : (value.tool != null ? value.tool : value.name);
    let args = value.params != null ? value.params : (value.arguments != null ? value.arguments : value.args);
    if (typeof tool !== "string" || !tool) return null;
    if (!args || typeof args !== "object") args = {};
    return { tool, arguments: args };
  }

  function extractJson(raw) {
    raw = String(raw || "").trim().replace(/^```(?:json)?\s*/i, "").replace(/```\s*$/, "");
    const start = raw.indexOf("{");
    if (start < 0) return null;
    const end = matchBrace(raw, start);
    if (end < 0) return null;
    try { return parseLoose(raw.slice(start, end + 1)); } catch { return null; }
  }

  function extractToolAnywhere(text) {
    for (const key of ['"command"', '"tool"']) {
      let cursor = 0;
      while (cursor < text.length) {
        const keyAt = text.indexOf(key, cursor);
        if (keyAt < 0) break;
        const start = text.lastIndexOf("{", keyAt);
        const end = start >= 0 ? matchBrace(text, start) : -1;
        if (end >= 0) {
          try {
            const call = normalizeCall(parseLoose(text.slice(start, end + 1)));
            if (call) return call;
          } catch {}
        }
        cursor = keyAt + key.length;
      }
    }
    return null;
  }

  function patchCall(text) {
    const startMatch = PATCH_START_RE.exec(text);
    if (!startMatch) return null;
    const tail = text.slice(startMatch.index + startMatch[0].length);
    const endMatch = PATCH_END_RE.exec(tail);
    if (!endMatch) return null;
    return { tool: "apply_patch", arguments: { patch: tail.slice(0, endMatch.index).trim() } };
  }

  function dsmlPrimitive(raw) {
    const value = String(raw || "").trim();
    if (/^-?(?:\d+\.?\d*|\.\d+)$/.test(value)) return Number(value);
    if (/^(?:true|false)$/i.test(value)) return value.toLowerCase() === "true";
    if (/^null$/i.test(value)) return null;
    if ((value.startsWith("{") && value.endsWith("}")) ||
        (value.startsWith("[") && value.endsWith("]"))) {
      try { return JSON.parse(value); } catch {}
    }
    return value;
  }

  // DeepSeek sometimes emits complete calls in its native DSML syntax instead
  // of the JSON dialect requested by the prompt. Parse both ASCII/full-width
  // bars and the doubled-bar rendering seen in the browser. The parameter close
  // is intentionally tolerant: live output sometimes omits the leading "</".
  function parseDsmlCalls(text) {
    const source = String(text || "").replace(/｜/g, "|");
    const marker = String.raw`\|+\s*DSML\s*\|+`;
    const invokeOpen = new RegExp(String.raw`<\s*${marker}\s*invoke\s+name\s*=\s*["']([^"']+)["'][^>]*>`, "gi");
    const invokeCloseSource = String.raw`<\s*\/\s*${marker}\s*invoke\s*>`;
    const parameterOpenSource = String.raw`<\s*${marker}\s*parameter\s+name\s*=\s*["']([^"']+)["'][^>]*>`;
    const parameterCloseSource = String.raw`(?:<\s*\/\s*)?${marker}\s*parameter\s*>`;
    const calls = [];
    let invoke;
    while ((invoke = invokeOpen.exec(source))) {
      const bodyStart = invokeOpen.lastIndex;
      const close = new RegExp(invokeCloseSource, "gi");
      close.lastIndex = bodyStart;
      const closeMatch = close.exec(source);
      // Never execute a still-streaming/incomplete invoke. hasOpenToolBlock()
      // keeps the response watcher waiting until its close marker arrives.
      if (!closeMatch) break;
      const bodyEnd = closeMatch.index;
      const body = source.slice(bodyStart, bodyEnd);
      const args = {};
      const parameterOpen = new RegExp(parameterOpenSource, "gi");
      let parameter;
      while ((parameter = parameterOpen.exec(body))) {
        const valueStart = parameterOpen.lastIndex;
        const parameterClose = new RegExp(parameterCloseSource, "gi");
        parameterClose.lastIndex = valueStart;
        const parameterEnd = parameterClose.exec(body);
        if (!parameterEnd) break;
        args[parameter[1]] = dsmlPrimitive(body.slice(valueStart, parameterEnd.index));
        parameterOpen.lastIndex = parameterClose.lastIndex;
      }
      calls.push({ tool: invoke[1], arguments: args });
      invokeOpen.lastIndex = close.lastIndex;
    }
    return calls;
  }

  function parseToolCalls(text) {
    const patch = patchCall(text);
    if (patch) return [patch];
    if (DSML_RE.test(text)) {
      const calls = parseDsmlCalls(text);
      const safeBatch = new Set([
        "workspace_info", "list_files", "read_file", "search_files",
        "search_text", "stat_path", "git_status", "git_diff",
      ]);
      if (calls.length > 1 && calls.every((call) => safeBatch.has(call.tool))) {
        return [{ tool: "batch_read", arguments: { calls } }];
      }
      if (calls.length) return calls;
    }
    const call = extractToolAnywhere(text);
    return call ? [call] : [];
  }

  function hasOpenToolBlock(text) {
    if (!text) return false;
    const startMatch = PATCH_START_RE.exec(text);
    if (startMatch && !PATCH_END_RE.test(text.slice(startMatch.index + startMatch[0].length))) return true;
    if (DSML_RE.test(text)) {
      const source = String(text).replace(/｜/g, "|");
      const opens = source.match(/<\s*\|+\s*DSML\s*\|+\s*invoke\s+name\s*=/gi) || [];
      const closes = source.match(/<\s*\/\s*\|+\s*DSML\s*\|+\s*invoke\s*>/gi) || [];
      if (opens.length > closes.length) return true;
    }
    for (const key of ['"command"', '"tool"']) {
      const at = text.indexOf(key);
      if (at >= 0) {
        const start = text.lastIndexOf("{", at);
        if (start >= 0 && matchBrace(text, start) < 0) return true;
      }
    }
    return false;
  }

  function hasToolSignature(text) {
    return PATCH_START_RE.test(text) || CMD_KEY_RE.test(text) || DSML_RE.test(text) || text.includes(START_M);
  }

  function hasCommandShape(text) {
    return hasToolSignature(text) || DSML_RE.test(text);
  }

  function salvageCutOff(text) {
    for (const key of ['"command"', '"tool"']) {
      const at = text.indexOf(key);
      if (at < 0) continue;
      const start = text.lastIndexOf("{", at);
      if (start < 0 || matchBrace(text, start) >= 0) continue;
      let inString = false, escaped = false;
      const stack = [];
      for (let i = start; i < text.length; i++) {
        const char = text[i];
        if (inString) {
          if (escaped) escaped = false;
          else if (char === "\\") escaped = true;
          else if (char === '"') inString = false;
        } else if (char === '"') inString = true;
        else if (char === "{") stack.push("}");
        else if (char === "[") stack.push("]");
        else if (char === "}" || char === "]") {
          if (stack.pop() !== char) return null;
        }
      }
      if (inString || !stack.length || stack.length > 2) return null;
      try { return normalizeCall(parseLoose(text.slice(start).trimEnd() + stack.reverse().join(""))); } catch { return null; }
    }
    return null;
  }

  function toolNameFromText(text) {
    const match = String(text || "").match(/"(?:command|tool)"\s*:\s*"([^"]*)/);
    if (match && match[1].trim()) return match[1].trim();
    const dsml = String(text || "").replace(/｜/g, "|").match(/<\s*\|+\s*DSML\s*\|+\s*invoke\s+name\s*=\s*["']([^"']+)/i);
    if (dsml && dsml[1]) return dsml[1];
    if (PATCH_START_RE.test(text)) return "apply_patch";
    return "command";
  }

  function isInjectedFeedback(text) {
    return /^\s*Output of '/.test(text) || /^\s*ERROR\b/.test(text) || /^\s*\(System note:/.test(text);
  }

  function findLuaStart() { return { pos: -1, len: 0, dm: "" }; }
  function findLuaEnd() { return -1; }

  return {
    START_M, END_M, LUA_START_RE, LUA_END_RE, PATCH_START_RE, PATCH_END_RE, CMD_KEY_RE, DSML_RE,
    findLuaStart, findLuaEnd, matchBrace, extractJson, normalizeCall, parseDsmlCalls, hasToolSignature,
    hasOpenToolBlock, parseToolCalls, salvageCutOff, toolNameFromText, isInjectedFeedback, hasCommandShape,
  };
})();

