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
let checks = 0;
function check(name, got, want) {
  checks += 1;
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

// ---- a post opened in a modal, with its comments as siblings -------------
// A share link opens the post in a dialog over whatever page was underneath,
// and an expanded thread re-renders its comments as siblings of the post rather
// than inside it. Both halves of that were live defects: the first put the
// collection on the feed post behind the dialog, the second reported a
// 499-comment thread as empty.
const modal = `
  <div role="article" id="feedpost">
    <div dir="auto">An unrelated feed post</div>
    <div role="article"><div dir="auto">a comment on the feed post</div></div>
  </div>
  <div role="dialog" id="empty-wrapper"></div>
  <div role="dialog" id="post-modal">
    <div role="article" id="modalpost">
      <div dir="auto">Post opened from a share link</div>
    </div>
    <div role="article" id="m1">
      <a role="link" href="/people/aida"><span>Aida</span></a>
      <div dir="auto">first comment</div>
    </div>
    <div role="article" id="m2"><div dir="auto">they are devil worshippers</div></div>
    <div role="article" id="m3"><div dir="auto">third comment</div></div>
  </div>`;

out = run(modal);

check("a modal's post is read, not the feed post behind it",
  out.parent_post_text, "Post opened from a share link");

check("comments rendered beside the post are still read",
  out.comments.map((c) => c.text).sort(),
  ["first comment", "they are devil worshippers", "third comment"].sort());

check("the feed post's own comment stays out of it",
  out.comments.some((c) => c.text === "a comment on the feed post"), false);

check("an empty dialog wrapper does not become the post",
  out.error, undefined);

// ---- the conversation a reply sits in -----------------------------------
//
// `is_reply` was a boolean, which is the one fact about a reply that does not
// help. "Exactly" under a call for expulsion and "Exactly" under a defence of
// somebody's rights are the same word; only the comment directly above tells
// them apart, and judging the second as an attack accuses a person of the
// opposite of what they said.
const thread = `
  <div role="article" id="post">
    <div dir="auto">Marking the anniversary at Sinjar</div>
    <div role="article" id="c1">
      <a role="link" href="/people/ali"><span>Ali</span></a>
      <div dir="auto">they should all be driven out</div>
      <div role="article" id="c2">
        <a role="link" href="/people/sara"><span>Sara</span></a>
        <div dir="auto">Exactly.</div>
      </div>
    </div>
    <div role="article" id="c3"><div dir="auto">a top level comment</div></div>
  </div>`;

out = run(thread);
const reply = out.comments.find((c) => c.text === "Exactly.");
const top = out.comments.find((c) => c.text === "a top level comment");

check("a reply carries the comment it answers",
  reply.reply_to_text, "they should all be driven out");

check("and who wrote that comment",
  reply.reply_to_author, "Ali");

check("a top level comment has no parent comment",
  [top.reply_to_text, top.is_reply, top.reply_depth], ["", false, 0]);

check("the parent's own body excludes the reply nested inside it",
  out.comments.find((c) => c.text === "they should all be driven out") !== undefined,
  true);

check("depth is recorded",
  reply.reply_depth, 1);

// Three deep, so there is something above the direct parent to carry.
const deeper = `
  <div role="article" id="post">
    <div dir="auto">Marking the anniversary at Sinjar</div>
    <div role="article" id="d1">
      <div dir="auto">grandparent comment</div>
      <div role="article" id="d2">
        <div dir="auto">parent comment</div>
        <div role="article" id="d3"><div dir="auto">Exactly.</div></div>
      </div>
    </div>
  </div>`;

out = run(deeper);
const deep = out.comments.find((c) => c.text === "Exactly.");

check("the direct parent is the nearest one, not the outermost",
  deep.reply_to_text, "parent comment");

check("what is above it is kept in order, nearest first",
  deep.ancestors.map((a) => a.text), ["grandparent comment"]);

check("depth counts the whole chain",
  deep.reply_depth, 2);

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
console.log(`extraction: ${checks} checks passed`);
