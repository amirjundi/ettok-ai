// Runs the real extraction script against a real DOM.
//
// From the architecture review of 19 September 2026 (ARCH-06). The extractor
// is JavaScript that runs inside a social page, so a Python test can only ever
// assert what a mock returns -- which is why these defects survived a suite
// that was otherwise thorough. This mounts the actual `_EXTRACT_JS` in jsdom
// against fixtures shaped like the pages it runs on.
//
// The two that matter:
//
//   * The post selector matches an article; the comment selector matches
//     articles nested inside it. So `post.innerText` contained every comment
//     on the page, and one commenter's words became the post's subject -- the
//     gate that decides whether context-dependent terms count for everyone
//     else in the thread.
//
//   * Comments were collected with a document-wide query, so on a feed every
//     comment was paired with the first post's context.
//
// Run by test_collector_isolation.py when Node and jsdom are present.

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { JSDOM } = require("jsdom");

const SCRIPT = process.argv[2];
const SELECTORS = JSON.parse(process.argv[3]);

const failures = [];
function check(name, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) failures.push(`${name}\n    got:  ${JSON.stringify(got)}\n    want: ${JSON.stringify(want)}`);
}

function run(html) {
  const dom = new JSDOM(`<body>${html}</body>`, { url: "https://example.invalid/p/1", runScripts: "outside-only" });
  // jsdom implements no layout, so innerText is absent and every rect is zero.
  // The extractor reads both; textContent is the honest stand-in for one, and
  // naturalWidth covers the other (the script already falls back to it because
  // lazy-loaded images measure 0x0 in a real browser too).
  Object.defineProperty(dom.window.HTMLElement.prototype, "innerText", {
    get() { return this.textContent; },
    configurable: true,
  });
  const context = dom.getInternalVMContext();
  return JSON.parse(vm.runInContext(fs.readFileSync(SCRIPT, "utf8"), context));
}

const article = '[role="article"]';

// ---- one post, three comments, one of them a nested reply ----------------
const onePost = `
  <div role="article" id="post">
    <div dir="auto">Sinjar anniversary today</div>
    <div role="article" id="c1">
      <a role="link" href="/people/aida"><span>Aida</span></a>
      <div dir="auto">May they rest in peace</div>
      <a href="/posts/1?comment_id=1">2h</a>
    </div>
    <div role="article" id="c2">
      <a role="link" href="/people/bilal"><span>Bilal</span></a>
      <div dir="auto">they are devil worshippers</div>
      <a href="/posts/1?comment_id=2">1h</a>
      <div role="article" id="c3">
        <a role="link" href="/people/cemal"><span>Cemal</span></a>
        <div dir="auto">agreed</div>
        <a href="/posts/1?comment_id=3">30m</a>
      </div>
    </div>
  </div>`;

let out = run(onePost);

check("the post's own text excludes every comment",
  out.parent_post_text, "Sinjar anniversary today");

check("a comment carries only what its author wrote",
  out.comments.map((c) => c.text).sort(),
  ["May they rest in peace", "agreed", "they are devil worshippers"].sort());

check("the author is still read", out.comments.find((c) => c.text === "agreed").author_name, "Cemal");

check("a nested reply is marked as one",
  out.comments.find((c) => c.text === "agreed").is_reply, true);

check("a top-level comment is not",
  out.comments.find((c) => c.text === "May they rest in peace").is_reply, false);

check("the comment permalink survives",
  out.comments.find((c) => c.text === "agreed").permalink,
  "https://example.invalid/posts/1?comment_id=3");

check("coverage is reported", [out.posts_on_page, out.comments_loaded, out.comments_returned],
  [1, 3, 3]);

check("nothing was truncated",
  [out.comments_truncated, out.parent_post_text_truncated], [false, false]);

// ---- a feed: two posts, each with its own comment ------------------------
const feed = `
  <div role="article" id="p1">
    <div dir="auto">Post about Sinjar</div>
    <div role="article"><div dir="auto">first comment</div></div>
  </div>
  <div role="article" id="p2">
    <div dir="auto">Post about the weather</div>
    <div role="article"><div dir="auto">second comment</div></div>
  </div>`;

out = run(feed);

check("a feed's comments are not pooled under the first post",
  out.comments.map((c) => c.text), ["first comment"]);

check("the parent text is the first post's alone",
  out.parent_post_text, "Post about Sinjar");

check("a feed says how many posts it had", out.posts_on_page, 2);

// ---- a page the selectors do not fit ------------------------------------
out = run(`<div class="something-else">nothing here</div>`);
check("an unmatched layout is an error, not an empty success",
  [out.error !== undefined, out.comments.length], [true, 0]);

// ---- a post with no comments at all -------------------------------------
out = run(`<div role="article"><div dir="auto">Just a post</div></div>`);
check("a post with no comments still reports its text",
  [out.parent_post_text, out.comments.length], ["Just a post", 0]);

if (failures.length) {
  console.error(`${failures.length} failure(s):\n  ${failures.join("\n  ")}`);
  process.exit(1);
}
console.log(`extraction: ${8} checks passed`);
