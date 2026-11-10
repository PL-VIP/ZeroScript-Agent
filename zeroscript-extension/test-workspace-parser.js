const fs = require("fs");
const ZSParse = new Function(
  fs.readFileSync(__dirname + "/core/workspace-parser.js", "utf8") + "; return ZSParse;"
)();

const ok = (name, condition) => {
  console.log((condition ? "PASS" : "FAIL") + "  " + name);
  if (!condition) process.exitCode = 1;
};

const read = ZSParse.parseToolCalls('{"command":"read_file","params":{"path":"src/a.js","start_line":2}}');
ok("JSON command", read.length === 1 && read[0].tool === "read_file" && read[0].arguments.start_line === 2);

const patch = ZSParse.parseToolCalls([
  "```",
  "###PATCH###",
  "--- a/a.txt",
  "+++ b/a.txt",
  "@@ -1,1 +1,1 @@",
  "-old",
  "+new",
  "###END_PATCH###",
  "```",
].join("\n"));
ok("raw patch block", patch.length === 1 && patch[0].tool === "apply_patch" && patch[0].arguments.patch.includes("+++ b/a.txt"));

ok("open patch detected", ZSParse.hasOpenToolBlock("###PATCH###\n--- a/a") === true);
ok("closed patch not open", ZSParse.hasOpenToolBlock("###PATCH###\nx\n###END_PATCH###") === false);
ok("injected feedback", ZSParse.isInjectedFeedback("Output of 'read_file':\n{}") === true);
ok("patch tool label", ZSParse.toolNameFromText("###PATCH###") === "apply_patch");
