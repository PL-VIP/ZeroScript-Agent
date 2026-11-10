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

  function parseToolCalls(text) {
    const patch = patchCall(text);
    if (patch) return [patch];
    const call = extractToolAnywhere(text);
    return call ? [call] : [];
  }

  function hasOpenToolBlock(text) {
    if (!text) return false;
    const startMatch = PATCH_START_RE.exec(text);
    if (startMatch && !PATCH_END_RE.test(text.slice(startMatch.index + startMatch[0].length))) return true;
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
    return PATCH_START_RE.test(text) || CMD_KEY_RE.test(text) || text.includes(START_M);
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
    findLuaStart, findLuaEnd, matchBrace, extractJson, normalizeCall, hasToolSignature,
    hasOpenToolBlock, parseToolCalls, salvageCutOff, toolNameFromText, isInjectedFeedback, hasCommandShape,
  };
})();

