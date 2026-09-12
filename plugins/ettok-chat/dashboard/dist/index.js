(function () {
  "use strict";
  // Ettok AI chat -- replaces the dashboard's built-in Chat page.
  //
  // That page is an xterm terminal streamed over a PTY. It is the right thing
  // for an operator and the wrong thing for everyone else: it renders ANSI
  // escape codes rather than markdown, and a research team handed a terminal
  // will not use it. The manifest declares `tab.override: "/chat"`, which the
  // host honours by not mounting the terminal host at all.
  //
  // It is the same agent underneath. This talks to the OpenAI-compatible
  // endpoint the gateway already serves, so the agent loop, its tools -- shell,
  // browser, files, the Ettok tools -- and its session handling are all
  // upstream's. Only the presentation is ours.
  //
  // Requests go through the *ettok* plugin's backend rather than a second one of
  // our own: it already owns the agent's local state, and the proxy exists to
  // keep the fetch same-origin, not to hold logic.
  //
  // Hand-written against the host SDK, so this ships with no build step. The
  // markdown renderer is small for the same reason -- the dashboard admits no
  // CDN, and a parser dependency would mean a toolchain for a plugin that has
  // none.

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const React = SDK.React;
  const h = React.createElement;
  const { useState, useEffect, useCallback, useRef } = React;

  const API = "/api/plugins/ettok";

  // ---- markdown -------------------------------------------------------

  function escapeHtml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function inlineMd(s) {
    // Escape first, then add markup, so model output containing < or & cannot
    // put anything into the page that we did not write.
    let out = escapeHtml(s);
    out = out.replace(/`([^`]+)`/g, '<code class="ettok-code">$1</code>');
    out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
    out = out.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    return out;
  }

  function renderMarkdown(text) {
    const lines = String(text || "").split("\n");
    const out = [];
    let inCode = false;
    let code = [];
    let list = null;

    function flushList() {
      if (list) { out.push('<ul class="ettok-ul">' + list.join("") + "</ul>"); list = null; }
    }
    function flushCode() {
      out.push('<pre class="ettok-pre"><code>' + escapeHtml(code.join("\n")) + "</code></pre>");
      code = [];
    }

    for (const line of lines) {
      if (/^\s*```/.test(line)) {
        if (inCode) { flushCode(); inCode = false; } else { flushList(); inCode = true; }
        continue;
      }
      if (inCode) { code.push(line); continue; }

      const heading = line.match(/^(#{1,4})\s+(.*)$/);
      if (heading) {
        flushList();
        out.push('<div class="ettok-mdh">' + inlineMd(heading[2]) + "</div>");
        continue;
      }
      const bullet = line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)$/);
      if (bullet) { (list = list || []).push("<li>" + inlineMd(bullet[1]) + "</li>"); continue; }
      if (!line.trim()) { flushList(); continue; }
      flushList();
      out.push('<p class="ettok-p">' + inlineMd(line) + "</p>");
    }
    // An unclosed fence is the normal state while a reply is still streaming.
    if (inCode && code.length) flushCode();
    flushList();
    return out.join("");
  }

  // ---- the wire -------------------------------------------------------

  function parseFrame(frame) {
    // One SSE frame is a block of "field: value" lines. The event name matters:
    // tool activity arrives as `event: hermes.tool.progress`, and a parser that
    // reads only `data:` would quietly file a tool call as an empty completion.
    // Returns null for frames with nothing to render ([DONE], keepalives).
    let event = "message";
    let data = "";
    for (const line of frame.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data += line.slice(5).trim();
    }
    if (!data || data === "[DONE]") return null;
    try {
      return { event: event, data: JSON.parse(data) };
    } catch (e) {
      return null;
    }
  }

  // ---- styles ---------------------------------------------------------

  const MD_CSS = '.ettok-p{margin:0 0 9px}'
    + '.ettok-mdh{margin:13px 0 6px;font-weight:600}'
    + '.ettok-ul{margin:0 0 9px;padding-left:22px}.ettok-ul li{margin:3px 0}'
    + '.ettok-code{background:rgba(128,128,128,.16);padding:1px 5px;border-radius:3px;'
    + 'font-family:ui-monospace,Menlo,monospace;font-size:.9em}'
    + '.ettok-pre{background:rgba(128,128,128,.12);padding:12px 14px;border-radius:5px;'
    + 'overflow-x:auto;margin:0 0 10px}'
    + '.ettok-pre code{font-family:ui-monospace,Menlo,monospace;font-size:12.5px;line-height:1.5}';

  function useMarkdownStyles() {
    useEffect(function () {
      if (document.getElementById("ettok-md-css")) return;
      const el = document.createElement("style");
      el.id = "ettok-md-css";
      el.textContent = MD_CSS;
      document.head.appendChild(el);
    }, []);
  }

  const C = {
    page: { display: "flex", flexDirection: "column", height: "100%", maxWidth: "900px",
            margin: "0 auto", padding: "20px 24px", gap: "12px", boxSizing: "border-box" },
    head: { display: "flex", alignItems: "baseline", gap: "10px" },
    h1: { fontSize: "18px", fontWeight: 600, margin: 0 },
    sub: { fontSize: "12px", opacity: 0.55, margin: 0 },
    log: { flex: 1, overflowY: "auto", display: "flex", flexDirection: "column", gap: "14px",
           paddingRight: "6px" },
    who: { fontSize: "11px", textTransform: "uppercase", letterSpacing: "0.07em",
           opacity: 0.55, fontWeight: 600, marginBottom: "3px" },
    body: { fontSize: "14px", lineHeight: 1.62 },
    userBody: { fontSize: "14px", lineHeight: 1.62, background: "rgba(128,128,128,0.10)",
                padding: "10px 14px", borderRadius: "6px" },
    tools: { display: "flex", flexDirection: "column", gap: "2px", fontSize: "12px",
             fontFamily: "ui-monospace,Menlo,monospace", opacity: 0.75, margin: "0 0 8px" },
    row: { display: "flex", gap: "8px", alignItems: "flex-end" },
    input: { flex: 1, resize: "none", minHeight: "44px", maxHeight: "180px", padding: "11px 13px",
             borderRadius: "6px", border: "1px solid rgba(128,128,128,0.3)", background: "transparent",
             color: "inherit", font: "inherit", fontSize: "14px" },
    send: { padding: "11px 20px", borderRadius: "6px", border: "none", cursor: "pointer",
            fontWeight: 600, background: "rgb(90,130,190)", color: "#fff" },
    warn: { background: "rgba(200,140,40,0.12)", borderLeft: "3px solid rgb(180,120,30)",
            color: "rgb(180,120,30)", padding: "10px 14px", borderRadius: "0 4px 4px 0",
            fontSize: "13px" },
    muted: { opacity: 0.6, fontSize: "13px" },
  };

  // ---- page -----------------------------------------------------------

  function Markdown(props) {
    return h("div", {
      style: props.role === "user" ? C.userBody : C.body,
      dangerouslySetInnerHTML: { __html: renderMarkdown(props.text) },
    });
  }

  function EttokChatPage() {
    useMarkdownStyles();
    const [messages, setMessages] = useState([]);
    const [draft, setDraft] = useState("");
    const [busy, setBusy] = useState(false);
    const [health, setHealth] = useState(null);
    const logRef = useRef(null);
    const sessionId = useRef("ettok-dash-" + Math.random().toString(36).slice(2, 10));

    useEffect(function () {
      // Checked up front so the page can say "start the gateway" rather than
      // failing on the first message, which is when a user decides it is broken.
      // SDK.fetchJSON attaches the dashboard session auth; a bare fetch 401s.
      SDK.fetchJSON(API + "/chat/health")
        .then(setHealth)
        .catch(function (e) { setHealth({ available: false, reason: String(e) }); });
    }, []);

    useEffect(function () {
      // Follow the tail while a reply streams in.
      if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
    }, [messages]);

    const send = useCallback(function () {
      const text = draft.trim();
      if (!text || busy) return;

      const history = messages.concat([{ role: "user", content: text }]);
      setMessages(history.concat([{ role: "assistant", content: "" }]));
      setDraft("");
      setBusy(true);

      // authedFetch rather than fetchJSON: this reply is a stream, and we need the
      // Response to read from, not parsed JSON.
      SDK.authedFetch(API + "/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          messages: history.map(function (m) { return { role: m.role, content: m.content }; }),
          session_id: sessionId.current,
        }),
      }).then(function (res) {
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let acc = "";
        let tools = [];

        function pump() {
          return reader.read().then(function (r) {
            if (r.done) { setBusy(false); return; }
            buffer += decoder.decode(r.value, { stream: true });
            const parts = buffer.split("\n\n");
            buffer = parts.pop();

            for (const part of parts) {
              const parsed = parseFrame(part);
              if (!parsed) continue;
              const obj = parsed.data;

              if (parsed.event === "hermes.tool.progress") {
                // What the agent is doing on this machine, while it does it.
                // Dropping these is what makes a working agent look hung.
                if (obj.status === "running") {
                  tools = tools.concat([{ id: obj.toolCallId, emoji: obj.emoji,
                                          label: obj.label || obj.tool, done: false }]);
                } else {
                  tools = tools.map(function (t) {
                    return t.id === obj.toolCallId ? Object.assign({}, t, { done: true }) : t;
                  });
                }
              } else if (obj.error) {
                acc += "\n\n**" + obj.error + "**" + (obj.hint ? "\n\n" + obj.hint : "");
              } else {
                const choice = (obj.choices || [])[0] || {};
                const delta = choice.delta || choice.message || {};
                if (delta.content) acc += delta.content;
              }
              setMessages(history.concat([{ role: "assistant", content: acc, tools: tools }]));
            }
            return pump();
          });
        }
        return pump();
      }).catch(function (e) {
        setMessages(history.concat([{
          role: "assistant",
          content: "**Could not reach the agent.** " + (e.message || String(e)),
        }]));
        setBusy(false);
      });
    }, [draft, busy, messages]);

    return h("div", { style: C.page },
      h("div", { style: C.head },
        h("h1", { style: C.h1 }, "Ettok AI"),
        h("p", { style: C.sub }, "The agent on this machine, with its tools.")),

      (health && !health.available)
        ? h("div", { style: C.warn },
            "The agent gateway is not running, so there is nothing here to talk to. "
            + "Start it with `ettok gateway run`."
            + (health.reason ? "  (" + health.reason + ")" : ""))
        : null,

      h("div", { style: C.log, ref: logRef },
        messages.length === 0
          ? h("div", { style: C.muted },
              "Ask the agent about a case, a finding, or why something was or was not "
              + "flagged. It can also run commands and use the browser on this machine, "
              + "and will show you when it does.")
          : messages.map(function (m, i) {
              return h("div", { key: i },
                h("div", { style: C.who }, m.role === "user" ? "You" : "Ettok AI"),
                (m.tools && m.tools.length)
                  ? h("div", { style: C.tools }, m.tools.map(function (t, j) {
                      return h("div", { key: j, style: { opacity: t.done ? 0.5 : 1 } },
                        (t.emoji ? t.emoji + " " : "") + t.label + (t.done ? "" : " …"));
                    }))
                  : null,
                m.content
                  ? h(Markdown, { text: m.content, role: m.role })
                  : (m.tools && m.tools.length)
                      ? null
                      : h("div", { style: C.muted }, "thinking…"));
            })),

      h("div", { style: C.row },
        h("textarea", {
          style: C.input,
          value: draft,
          placeholder: "Ask the agent…  (Enter to send, Shift+Enter for a new line)",
          onChange: function (e) { setDraft(e.target.value); },
          onKeyDown: function (e) {
            if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
          },
        }),
        h("button", {
          style: Object.assign({}, C.send,
            (busy || !draft.trim()) ? { opacity: 0.45, cursor: "default" } : {}),
          onClick: send,
          disabled: busy || !draft.trim(),
        }, busy ? "…" : "Send")));
  }

  window.__HERMES_PLUGINS__.register("ettok-chat", EttokChatPage);
})();
