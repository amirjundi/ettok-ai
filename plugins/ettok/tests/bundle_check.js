// Exercises the two parsers in the ettok-chat dashboard bundle under Node: the markdown
// renderer and the SSE frame reader.
//
// Run by test_dashboard.py when Node is available. It is a separate file rather
// than an inline string so the code under test is read the same way a browser
// reads it -- a copy of a parser in a test would pass while the shipped one
// rotted.

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const bundle = fs.readFileSync(
  path.join(__dirname, "..", "..", "ettok-chat", "dashboard", "dist", "index.js"), "utf8");

// The bundle is an IIFE that keeps its functions private. Rather than unwrap it
// -- which would change what is being tested -- hand out the renderer from
// inside, with a host SDK stubbed well enough that the early return is not hit.
const body = bundle.replace(
  /\}\)\(\);\s*$/,
  "  globalThis.__md = renderMarkdown;\n  globalThis.__frame = parseFrame;\n})();\n");

const sandbox = {
  window: {
    __HERMES_PLUGIN_SDK__: { React: { createElement() {}, useState() {}, useEffect() {}, useCallback() {}, useRef() {} } },
    __HERMES_PLUGINS__: { register() {} },
  },
  document: { getElementById: () => null, createElement: () => ({}), head: { appendChild() {} } },
  fetch: () => Promise.resolve(),
  setInterval: () => 0,
  clearInterval: () => {},
  Date,
  Math,
  JSON,
  TextDecoder: function () {},
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(body, sandbox);

const md = sandbox.__md;
const frame = sandbox.__frame;
const failures = [];

function check(name, condition, got) {
  if (!condition) failures.push(name + (got === undefined ? "" : "  got: " + got));
}

// The one that matters: model output is escaped before any markup is added, so
// nothing it emits can become live markup in the dashboard.
let out = md("<img src=x onerror=alert(1)>");
check("escapes raw html", !out.includes("<img"), out);
check("keeps the text", out.includes("&lt;img"), out);

out = md("**<script>** and `<b>`");
check("escapes inside bold", !out.includes("<script>"), out);
check("escapes inside code", out.includes("&lt;b&gt;"), out);
check("bold still renders", out.includes("<strong>"), out);

out = md("# Heading\n\n- one\n- two\n\ntext");
check("heading", out.includes("ettok-mdh"), out);
check("list", (out.match(/<li>/g) || []).length === 2, out);
check("paragraph", out.includes("ettok-p"), out);

out = md("```python\nx = 1\n```");
check("fenced code", out.includes("<pre") && out.includes("x = 1"), out);

// Half a code fence is the normal state while a reply is still streaming; it
// must render, not vanish.
out = md("here:\n```\npartial");
check("unterminated fence renders", out.includes("partial"), out);

out = md("see [the site](https://example.com) and javascript:bad");
check("http link", out.includes('href="https://example.com"'), out);
check("no javascript: href", !out.includes('href="javascript:'), out);

// ---- SSE frames ----------------------------------------------------------
//
// The gateway streams two kinds of frame over one connection. Reading only the
// data line files a tool call as an empty completion chunk, which is how a busy
// agent comes to look hung.

let f = frame('data: {"choices":[{"delta":{"content":"hi"}}]}');
check("plain data frame", f && f.event === "message", JSON.stringify(f));
check("content survives", f && f.data.choices[0].delta.content === "hi", JSON.stringify(f));

f = frame('event: hermes.tool.progress\ndata: {"tool":"shell","status":"running"}');
check("named event read", f && f.event === "hermes.tool.progress", JSON.stringify(f));
check("tool payload read", f && f.data.tool === "shell", JSON.stringify(f));

check("DONE ignored", frame("data: [DONE]") === null);
check("keepalive ignored", frame(": ping") === null);
check("garbage ignored", frame("data: {not json") === null);

if (failures.length) {
  console.error("FAIL\n" + failures.join("\n"));
  process.exit(1);
}
console.log("ok");
