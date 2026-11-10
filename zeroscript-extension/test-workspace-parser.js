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

const dsml = [
  '<｜｜DSML｜｜ calls>',
  '<｜｜DSML｜｜ invoke name="read_file">',
  '<｜｜DSML｜｜ parameter name="path">src/lib/mod-jar-generator.ts</｜｜DSML｜｜ parameter>',
  '<｜｜DSML｜｜ parameter name="start_line">90</｜｜DSML｜｜ parameter>',
  '<｜｜DSML｜｜ parameter name="end_line">300</｜｜DSML｜｜ parameter>',
  '</｜｜DSML｜｜ invoke>',
  '<｜｜DSML｜｜ invoke name="read_file">',
  '<｜｜DSML｜｜ parameter name="path">src/components/mod-submission-form.tsx</｜｜DSML｜｜ parameter>',
  '<｜｜DSML｜｜ parameter name="start_line">1｜｜DSML｜｜ parameter>',
  '<｜｜DSML｜｜ parameter name="end_line">60｜｜DSML｜｜ parameter>',
  '</｜｜DSML｜｜ invoke>',
  '</｜｜DSML｜｜ calls>',
].join("\n");
const dsmlCalls = ZSParse.parseToolCalls(dsml);
ok("DSML is an executable signature", ZSParse.hasToolSignature(dsml) === true);
ok("multiple DSML reads become batch_read", dsmlCalls.length === 1 && dsmlCalls[0].tool === "batch_read");
ok("DSML paths parsed", dsmlCalls[0].arguments.calls[0].arguments.path === "src/lib/mod-jar-generator.ts");
ok("DSML numeric parameters parsed", dsmlCalls[0].arguments.calls[0].arguments.start_line === 90);
ok("malformed DSML parameter closer tolerated", dsmlCalls[0].arguments.calls[1].arguments.start_line === 1);
ok("DSML tool label", ZSParse.toolNameFromText(dsml) === "read_file");

const incompleteDsml = [
  '<｜｜DSML｜｜ invoke name="read_file">',
  '<｜｜DSML｜｜ parameter name="path">src/incomplete.ts</｜｜DSML｜｜ parameter>',
].join("\n");
ok("incomplete DSML waits for close", ZSParse.parseToolCalls(incompleteDsml).length === 0);
ok("incomplete DSML is reported as open", ZSParse.hasOpenToolBlock(incompleteDsml) === true);
