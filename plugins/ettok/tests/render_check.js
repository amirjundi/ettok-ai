// Mounts both dashboard bundles for real, in a DOM, with effects flushed.
//
// `node --check` proves a file parses; it says nothing about whether a page
// references a variable that does not exist, misuses a hook, or crashes once
// its data arrives. Those reach a user as a blank tab, and this project has
// already shipped one of them (a panel calling bare `fetch`, painting
// "HTTP 401" where its content belonged).
//
// Effects are the point. A static render only proves the empty state paints;
// everything interesting here -- the goal table, the session list, the context
// meter -- appears only after a fetch resolves.
//
// Run by test_dashboard.py when Node, React and jsdom are all present.

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { JSDOM } = require("jsdom");

const ROOT = path.join(__dirname, "..", "..");
const failures = [];

const dom = new JSDOM("<!doctype html><html><body></body></html>", { pretendToBeVisual: true });
// React reads these off the global scope at import time, so they are installed
// before `require("react-dom/client")` below.
global.window = dom.window;
global.document = dom.window.document;
global.navigator = dom.window.navigator;
global.HTMLElement = dom.window.HTMLElement;
global.Element = dom.window.Element;
global.Node = dom.window.Node;
global.IS_REACT_ACT_ENVIRONMENT = true;

const React = require("react");
const { createRoot } = require("react-dom/client");
const { act } = require("react");

const SESSION = { id: "api-1", title: "Sinjar sweep", message_count: 12,
                  source: "api_server", last_activity_at: Date.now() / 1000 };
// A conversation from another channel. The sidebar filtered these out entirely
// until now, so a single-session fixture could not have caught it.
const TG_SESSION = { id: "tg-7", title: "Report from Bashiqa", message_count: 4,
                     source: "telegram", display_name: "Nineb",
                     last_activity_at: Date.now() / 1000 };
const GOAL = { id: "j1", name: "ettok-goal: Watch Sinjar pages",
               schedule: { kind: "interval", minutes: 360, display: "every 360m" },
               next_run_at: new Date().toISOString() };

const RESPONSES = {
  "/api/model/info": {
    model: "deepseek-flash", provider: "deepseek", effective_context_length: 1000000,
    capabilities: { supports_vision: true, supports_reasoning: true },
  },
  "/api/sessions": { sessions: [SESSION, TG_SESSION] },
  "/api/cron/jobs": {
    jobs: [GOAL, { id: "j2", name: "ettok-scan", schedule: { expr: "0 9 * * *" }, paused: true }],
  },
  "/api/plugins/ettok/status": {
    paired: false, platform_url: "http://localhost:8099",
    queue: { pending: 2, delivered: 3, failed_permanent: 0 },
    accounts: [{ account_id: "duhok-01", state: "healthy", last_success_at: null,
                 last_block_at: null, block_reason: null, cooldown_until: null }],
    runs: [], evidence_pending: 0,
    alerts: [{ level: "critical", text: "This agent is not paired with a platform." }],
  },
  "/api/plugins/ettok/knowledge": { available: false, reason: "not paired with a platform" },
  "/api/plugins/ettok/reports": { available: false, reason: "not paired" },
  "/api/plugins/ettok/chat/health": { available: true, url: "http://127.0.0.1:8642", status: 200 },
};

function lookup(url) {
  const key = Object.keys(RESPONSES).find((k) => String(url).split("?")[0] === k);
  return key ? RESPONSES[key] : {};
}

const registered = {};

function loadBundle(file) {
  const sandbox = {
    window: Object.assign(dom.window, {
      __HERMES_PLUGIN_SDK__: {
        React,
        fetchJSON: (url) => Promise.resolve(lookup(url)),
        authedFetch: () => Promise.resolve({
          body: { getReader: () => ({ read: () => Promise.resolve({ done: true }) }) },
        }),
        hooks: {}, components: {},
      },
      __HERMES_PLUGINS__: {
        register(name, component) { registered[name] = component; },
        registerSlot() {},
      },
    }),
    document: dom.window.document,
    setInterval: () => 0,
    clearInterval: () => {},
    setTimeout: (fn) => { if (typeof fn === "function") fn(); return 0; },
    console, Date, Math, JSON, Promise, Error, RegExp, encodeURIComponent,
    TextDecoder: function () {}, FileReader: function () {},
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(file, "utf8"), sandbox);
}

loadBundle(path.join(ROOT, "ettok", "dashboard", "dist", "index.js"));
loadBundle(path.join(ROOT, "ettok-chat", "dashboard", "dist", "index.js"));

async function mount(name) {
  const host = dom.window.document.createElement("div");
  dom.window.document.body.appendChild(host);
  const root = createRoot(host);
  await act(async () => { root.render(React.createElement(registered[name])); });
  // Let the fetch promises and the renders they trigger settle.
  for (let i = 0; i < 5; i++) {
    await act(async () => { await Promise.resolve(); });
  }
  return host.innerHTML;
}

// Mount, then click the first button whose text contains `label`, and return
// what the page looks like afterwards. Opening a conversation is a render path
// of its own and nothing here reached it before.
async function mountAndClick(name, label) {
  const host = dom.window.document.createElement("div");
  dom.window.document.body.appendChild(host);
  const root = createRoot(host);
  await act(async () => { root.render(React.createElement(registered[name])); });
  for (let i = 0; i < 5; i++) await act(async () => { await Promise.resolve(); });

  const target = Array.from(host.querySelectorAll("button"))
    .find((b) => (b.textContent || "").indexOf(label) !== -1);
  if (!target) return { html: host.innerHTML, clicked: false };

  await act(async () => {
    target.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
  });
  for (let i = 0; i < 5; i++) await act(async () => { await Promise.resolve(); });
  return { html: host.innerHTML, clicked: true };
}

function expect(name, html, needle, what) {
  if (html.indexOf(needle) === -1) failures.push(name + ": " + what);
}

(async () => {
  for (const name of ["ettok", "ettok-chat"]) {
    if (!registered[name]) { failures.push(name + ": never registered a component"); continue; }
    let html;
    try {
      html = await mount(name);
    } catch (e) {
      failures.push(name + ": threw while mounting — " + (e && e.message));
      continue;
    }
    if (!html || html.length < 60) {
      failures.push(name + ": rendered almost nothing (" + (html || "").length + " chars)");
      continue;
    }

    if (name === "ettok") {
      expect(name, html, "Ettok AI", "no heading");
      expect(name, html, "not paired", "the critical alert did not reach the page");
      // The goal panel is the tab's only control surface; it must survive the
      // fetch, not just the empty state.
      expect(name, html, "Goals", "no goals section");
      expect(name, html, "Watch Sinjar pages", "goal row missing after load");
      expect(name, html, "every 360m", "schedule label did not resolve");
      expect(name, html, "run now", "goal row actions missing");
    } else {
      expect(name, html, "textarea", "no composer");
      expect(name, html, "New chat", "no new-chat control");
      expect(name, html, "Sinjar sweep", "session list empty after load");
      expect(name, html, "deepseek", "model not shown in the header");
      expect(name, html, "1.0M", "context meter did not render the limit");
      expect(name, html, "effort", "reasoning control missing on a model that supports it");
      // Every channel, grouped and labelled -- not just the one you are typing in.
      expect(name, html, "Report from Bashiqa", "a Telegram session never reached the sidebar");
      expect(name, html, "Telegram", "sessions are not grouped by channel");
      expect(name, html, "Dashboard", "the dashboard's own group heading is missing");
      expect(name, html, "Nineb", "the sidebar does not say who the conversation was with");
    }
  }

  // Opening someone else's conversation: the page must survive it and say why
  // it cannot be replied to. This is the branch that shipped broken.
  try {
    const opened = await mountAndClick("ettok-chat", "Report from Bashiqa");
    if (!opened.clicked) {
      failures.push("ettok-chat: no clickable Telegram session in the sidebar");
    } else if (!opened.html || opened.html.length < 60) {
      failures.push("ettok-chat: opening a Telegram session blanked the page");
    } else {
      expect("ettok-chat", opened.html, "read only",
             "an other-channel session did not say it is read-only");
      expect("ettok-chat", opened.html, "Start a dashboard chat",
             "no way out of a read-only conversation");
      if (opened.html.indexOf("<textarea") !== -1) {
        failures.push("ettok-chat: the composer is still offered on a channel "
                      + "this page cannot reply to");
      }
    }
  } catch (e) {
    failures.push("ettok-chat: threw while opening a Telegram session — "
                  + (e && e.message));
  }

  // And the dashboard's own conversation must still be answerable.
  try {
    const opened = await mountAndClick("ettok-chat", "Sinjar sweep");
    if (opened.clicked && opened.html.indexOf("<textarea") === -1) {
      failures.push("ettok-chat: opening a dashboard session removed the composer");
    }
  } catch (e) {
    failures.push("ettok-chat: threw while opening its own session — "
                  + (e && e.message));
  }

  if (failures.length) {
    console.error("FAIL\n" + failures.join("\n"));
    process.exit(1);
  }
  console.log("ok — both pages mount and render their loaded state");
})();
