// Run the collector's own extraction JavaScript against a saved page.
//
// Collection has never run against a live social platform, so the Facebook
// selectors are configuration written against markup nobody has confirmed. That
// is the single largest unknown in this project, and it cannot be closed by
// reasoning about it -- only by putting real markup in front of the real
// extractor.
//
// This is how that happens without a live session, an account, or a login: save
// a post page from the browser (Ctrl+S, "Webpage, Complete", or paste
// document.documentElement.outerHTML into a file) and run it through here. The
// expression and the selectors are handed in by selector_check.py, which reads
// both out of base.py, so this can never test a copy that has drifted from what
// collection actually runs.
//
// jsdom is not a browser. It parses markup and answers querySelectorAll, which
// is all the extractor uses; it does not run the site's own scripts, so a page
// whose comments are injected after load has to be saved after they appear.

const fs = require("fs");
const { JSDOM } = require("jsdom");

const [, , htmlPath, payloadPath] = process.argv;
if (!htmlPath || !payloadPath) {
  console.error("usage: selector_check.js <page.html> <payload.json>");
  process.exit(2);
}

const html = fs.readFileSync(htmlPath, "utf8");
const payload = JSON.parse(fs.readFileSync(payloadPath, "utf8"));

// A saved page usually carries the URL it came from; without one, location
// stays at the jsdom default and parent_post_url is reported as unresolved
// rather than silently wrong.
const dom = new JSDOM(html, {
  url: payload.url || "https://www.facebook.com/",
  runScripts: "outside-only",
});

// jsdom implements textContent but not innerText, and the extractor reads
// innerText everywhere. Without this every comment comes back undefined and is
// skipped, which would look exactly like "the selectors matched nothing" -- the
// checker would then blame the selectors for a gap in the harness.
//
// This is the one place the check differs from a browser, and it differs in a
// direction worth knowing: innerText respects rendering (it skips hidden
// elements and collapses whitespace the way the page looks), textContent does
// not. So text here can include markup a reader would never see, and a comment
// that is present but hidden is counted. It cannot produce a false "nothing
// found", which is the failure this tool exists to detect.
Object.defineProperty(dom.window.HTMLElement.prototype, "innerText", {
  get() {
    return this.textContent;
  },
  configurable: true,
});

const result = { ok: false };
try {
  result.extracted = JSON.parse(dom.window.eval(payload.expression));
  result.ok = true;
} catch (error) {
  result.error = String(error && error.message ? error.message : error);
}

// Counted per selector as well as overall: "no comments found" and "comments
// found but no author links" are different failures needing different fixes,
// and a single total hides which one happened.
if (result.ok) {
  const sel = payload.selectors;
  const doc = dom.window.document;
  result.selector_hits = {};
  for (const [name, expression] of Object.entries(sel)) {
    try {
      result.selector_hits[name] = doc.querySelectorAll(expression).length;
    } catch (error) {
      result.selector_hits[name] = `invalid selector: ${error.message}`;
    }
  }
}

process.stdout.write(JSON.stringify(result));
