(function () {
  "use strict";
  // Ettok AI dashboard tab.
  //
  // Hand-written against the host SDK rather than built from JSX, so the plugin
  // ships with no toolchain and no build step -- React comes from the host, and
  // h() below is React.createElement under a shorter name.
  //
  // The panel answers, in order, the questions an operator actually asks of an
  // agent running unattended somewhere else: is it connected, is anything stuck,
  // are the accounts alive, what has it been doing, and what can it not detect.
  // The last one is first on screen when it matters, because the failure this
  // project already lived through looks exactly like success.

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const React = SDK.React;
  const h = React.createElement;
  const { useState, useEffect, useCallback } = React;

  const API = "/api/plugins/ettok";
  const POLL_MS = 15000;

  // ---- small helpers --------------------------------------------------

  function ago(iso) {
    if (!iso) return "never";
    const secs = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (secs < 60) return Math.round(secs) + "s ago";
    if (secs < 3600) return Math.round(secs / 60) + "m ago";
    if (secs < 86400) return Math.round(secs / 3600) + "h ago";
    return Math.round(secs / 86400) + "d ago";
  }

  const S = {
    page: { padding: "24px", maxWidth: "1100px", margin: "0 auto",
            display: "flex", flexDirection: "column", gap: "20px" },
    h1: { fontSize: "22px", fontWeight: 600, margin: 0 },
    sub: { fontSize: "13px", opacity: 0.65, margin: "4px 0 0" },
    grid: { display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(190px,1fr))", gap: "12px" },
    card: { border: "1px solid rgba(128,128,128,0.25)", borderRadius: "6px",
            padding: "14px 16px", display: "flex", flexDirection: "column", gap: "4px" },
    stat: { fontSize: "26px", fontWeight: 600, fontVariantNumeric: "tabular-nums", lineHeight: 1.1 },
    label: { fontSize: "12px", opacity: 0.65 },
    section: { display: "flex", flexDirection: "column", gap: "10px" },
    h2: { fontSize: "15px", fontWeight: 600, margin: 0, letterSpacing: "0.02em" },
    table: { width: "100%", borderCollapse: "collapse", fontSize: "13px" },
    th: { textAlign: "left", padding: "7px 10px", opacity: 0.6, fontWeight: 600,
          fontSize: "11px", textTransform: "uppercase", letterSpacing: "0.06em" },
    td: { padding: "7px 10px", borderTop: "1px solid rgba(128,128,128,0.18)", verticalAlign: "top" },
    alert: function (level) {
      const colors = {
        critical: ["rgba(220,80,60,0.12)", "rgb(200,70,50)"],
        warning: ["rgba(200,140,40,0.12)", "rgb(180,120,30)"],
        info: ["rgba(100,140,200,0.12)", "rgb(90,130,190)"],
      };
      const [bg, fg] = colors[level] || colors.info;
      return { background: bg, borderLeft: "3px solid " + fg, color: fg,
               padding: "10px 14px", borderRadius: "0 4px 4px 0", fontSize: "13px" };
    },
    pill: function (ok) {
      return { display: "inline-block", padding: "1px 8px", borderRadius: "3px",
               fontSize: "11px", fontWeight: 600,
               background: ok ? "rgba(80,160,110,0.15)" : "rgba(200,140,40,0.15)",
               color: ok ? "rgb(60,140,90)" : "rgb(180,120,30)" };
    },
    muted: { opacity: 0.6, fontSize: "13px" },
  };

  // ---- data -----------------------------------------------------------

  function useEndpoint(path, intervalMs) {
    const [data, setData] = useState(null);
    const [error, setError] = useState(null);

    const load = useCallback(function () {
      fetch(API + path)
        .then(function (r) { return r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status)); })
        .then(function (j) { setData(j); setError(null); })
        .catch(function (e) { setError(e.message || String(e)); });
    }, [path]);

    useEffect(function () {
      load();
      if (!intervalMs) return;
      const id = setInterval(load, intervalMs);
      return function () { clearInterval(id); };
    }, [load, intervalMs]);

    return [data, error, load];
  }

  // ---- pieces ---------------------------------------------------------

  function Stat(props) {
    return h("div", { style: S.card },
      h("div", { style: Object.assign({}, S.stat, props.tone ? { color: props.tone } : {}) },
        String(props.value)),
      h("div", { style: S.label }, props.label));
  }

  function Alerts(props) {
    if (!props.alerts || !props.alerts.length) return null;
    return h("div", { style: S.section },
      props.alerts.map(function (a, i) {
        return h("div", { key: i, style: S.alert(a.level) }, a.text);
      }));
  }

  function Connection(props) {
    const s = props.status;
    return h("div", { style: S.card },
      h("div", { style: { display: "flex", alignItems: "center", gap: "10px" } },
        h("span", { style: S.pill(s.paired) }, s.paired ? "PAIRED" : "NOT PAIRED"),
        h("span", { style: { fontSize: "13px" } }, s.platform_url)),
      s.agent_id
        ? h("div", { style: Object.assign({}, S.label, { marginTop: "6px" }) }, "as " + s.agent_id)
        : h("div", { style: Object.assign({}, S.label, { marginTop: "6px" }) },
            "Run `ettok connect` to request access."));
  }

  function Accounts(props) {
    const rows = props.accounts || [];
    if (!rows.length) {
      return h("div", { style: S.muted },
        "No monitoring account has been used yet. Health appears here after the first run.");
    }
    return h("table", { style: S.table },
      h("thead", null, h("tr", null,
        ["Account", "State", "Last success", "Why"].map(function (t) {
          return h("th", { key: t, style: S.th }, t);
        }))),
      h("tbody", null, rows.map(function (a) {
        return h("tr", { key: a.account_id },
          h("td", { style: S.td }, a.account_id),
          h("td", { style: S.td },
            h("span", { style: S.pill(a.state === "healthy") }, a.state)),
          h("td", { style: S.td }, ago(a.last_success_at)),
          h("td", { style: Object.assign({}, S.td, S.muted) }, a.block_reason || "—"));
      })));
  }

  function Runs(props) {
    const rows = props.runs || [];
    if (!rows.length) {
      return h("div", { style: S.muted },
        "No run has been recorded yet. `ettok schedule --every 6h` sets up unattended runs.");
    }
    return h("table", { style: S.table },
      h("thead", null, h("tr", null,
        ["Started", "Scanned", "Flagged", "Ended because", "Errors"].map(function (t) {
          return h("th", { key: t, style: S.th }, t);
        }))),
      h("tbody", null, rows.map(function (r) {
        return h("tr", { key: r.id },
          h("td", { style: S.td }, ago(r.started_at)),
          h("td", { style: S.td }, r.scanned),
          h("td", { style: S.td }, r.flagged),
          // Every ending records why. A run that hit its budget and one whose
          // account was banned look identical without this.
          h("td", { style: S.td }, r.ended_at ? (r.stop_reason || "—") : "still running"),
          h("td", { style: Object.assign({}, S.td, S.muted) },
            (r.errors && r.errors.length) ? r.errors.length : "—"));
      })));
  }

  function Knowledge(props) {
    const k = props.knowledge;
    if (!k) return h("div", { style: S.muted }, "Loading…");
    if (!k.available) {
      return h("div", { style: S.muted }, "Unavailable — " + (k.reason || "unknown"));
    }
    return h("div", { style: S.section },
      h("div", { style: S.grid },
        h(Stat, { label: "terms", value: k.terms }),
        h(Stat, { label: "tropes", value: k.tropes }),
        h(Stat, { label: "open cases", value: k.cases })),
      (k.gaps && k.gaps.length)
        ? h("div", { style: S.section },
            h("div", { style: S.muted },
              "Detection is limited until curators fill these in. None of it is a code problem:"),
            k.gaps.map(function (g, i) {
              return h("div", { key: i, style: S.alert("warning") }, g);
            }))
        : h("div", { style: S.alert("info") }, "Knowledge looks complete."));
  }

  function Cases(props) {
    const cases = (props.knowledge && props.knowledge.cases_detail) || [];
    if (!cases.length) {
      return h("div", { style: S.muted },
        "No case is open. The agent has nothing to work until a case manager opens one "
        + "on the platform.");
    }
    return h("table", { style: S.table },
      h("thead", null, h("tr", null,
        ["Case", "State", "Communities", "Items left", "Budget left", ""].map(function (t, i) {
          return h("th", { key: i, style: S.th }, t);
        }))),
      h("tbody", null, cases.map(function (c) {
        const lim = c.limits || {};
        return h("tr", { key: c.id },
          h("td", { style: S.td }, c.title),
          h("td", { style: S.td }, h("span", { style: S.pill(c.state === "active") }, c.state)),
          h("td", { style: S.td }, (c.groups || []).join(", ") || "—"),
          // Remaining rather than total: a subtraction the reader should not do.
          h("td", { style: S.td },
            lim.items_remaining === null || lim.items_remaining === undefined
              ? "unbounded" : lim.items_remaining),
          h("td", { style: S.td },
            lim.cost_remaining_usd === null || lim.cost_remaining_usd === undefined
              ? "unbounded" : "$" + Number(lim.cost_remaining_usd).toFixed(2)),
          // The agent may raise this. It may not act on it.
          h("td", { style: Object.assign({}, S.td, S.muted) },
            c.suggests_closing ? "proposes closing" : ""));
      })));
  }

  function Reports(props) {
    const r = props.reports;
    if (!r) return h("div", { style: S.muted }, "Loading…");
    if (!r.available) {
      return h("div", { style: S.muted },
        "Unavailable — " + (r.reason || "unknown")
        + ". An older platform has no reports endpoint; that is a missing feature, not a fault here.");
    }
    const rows = r.reports || [];
    const counts = r.counts || {};
    return h("div", { style: S.section },
      h("div", { style: S.grid },
        h(Stat, { label: "awaiting review", value: counts["new"] || 0 }),
        h(Stat, { label: "reviewed", value: counts["reviewed"] || 0 }),
        h(Stat, { label: "dismissed", value: counts["false_positive"] || 0 }),
        h(Stat, {
          label: "agent disagreed", value: r.disagreements || 0,
          tone: (r.disagreements ? "rgb(180,120,30)" : null),
        })),
      (r.without_context
        ? h("div", { style: S.alert("warning") },
            r.without_context + " report(s) were judged with no parent post. "
            + "Context-dependent hate is invisible without it.")
        : null),
      rows.length
        ? h("table", { style: S.table },
            h("thead", null, h("tr", null,
              ["When", "Excerpt", "Group", "Severity", "Status", "Context"].map(function (t) {
                return h("th", { key: t, style: S.th }, t);
              }))),
            h("tbody", null, rows.map(function (row) {
              return h("tr", { key: row.id },
                h("td", { style: S.td }, ago(row.created_at)),
                h("td", { style: Object.assign({}, S.td, { maxWidth: "320px" }) }, row.excerpt),
                h("td", { style: S.td }, row.target_group || "—"),
                h("td", { style: S.td }, row.severity || "—"),
                h("td", { style: S.td },
                  h("span", { style: S.pill(row.status !== "false_positive") }, row.status)),
                h("td", { style: S.td },
                  h("span", { style: S.pill(row.had_context) },
                    row.had_context ? "yes" : "none")));
            })))
        : h("div", { style: S.muted },
            "Nothing has been submitted yet, or nothing has been confirmed."));
  }

  // ---- chat -----------------------------------------------------------
  //
  // The dashboard's built-in Chat tab is an xterm terminal streamed over a PTY.
  // That suits an operator and suits nobody else: it renders ANSI escape codes,
  // not markdown, and a research team handed a terminal will not use it.
  //
  // This talks to the same agent through the OpenAI-compatible endpoint the
  // gateway already serves, proxied same-origin by this plugin's backend. The
  // agent loop and its tools stay upstream's; only the presentation is ours.
  //
  // The markdown renderer is deliberately small and hand-written: the dashboard
  // admits no CDN, and a parser dependency would mean a build step for a plugin
  // that currently has none.

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

  const C = {
    wrap: { display: "flex", flexDirection: "column", gap: "12px", height: "calc(100vh - 210px)",
            minHeight: "340px" },
    log: { flex: 1, overflowY: "auto", display: "flex", flexDirection: "column", gap: "14px",
           paddingRight: "6px" },
    who: { fontSize: "11px", textTransform: "uppercase", letterSpacing: "0.07em",
           opacity: 0.55, fontWeight: 600, marginBottom: "3px" },
    body: { fontSize: "14px", lineHeight: 1.62 },
    userBody: { fontSize: "14px", lineHeight: 1.62, background: "rgba(128,128,128,0.10)",
                padding: "10px 14px", borderRadius: "6px" },
    row: { display: "flex", gap: "8px", alignItems: "flex-end" },
    input: { flex: 1, resize: "none", minHeight: "44px", maxHeight: "180px", padding: "11px 13px",
             borderRadius: "6px", border: "1px solid rgba(128,128,128,0.3)", background: "transparent",
             color: "inherit", font: "inherit", fontSize: "14px" },
    tools: { display: "flex", flexDirection: "column", gap: "2px", fontSize: "12px",
             fontFamily: "ui-monospace,Menlo,monospace", opacity: 0.75, margin: "0 0 8px" },
    send: { padding: "11px 20px", borderRadius: "6px", border: "none", cursor: "pointer",
            fontWeight: 600, background: "rgb(90,130,190)", color: "#fff" },
  };

  function Markdown(props) {
    return h("div", {
      style: props.role === "user" ? C.userBody : C.body,
      dangerouslySetInnerHTML: { __html: renderMarkdown(props.text) },
    });
  }

  function Chat() {
    useMarkdownStyles();
    const [messages, setMessages] = useState([]);
    const [draft, setDraft] = useState("");
    const [busy, setBusy] = useState(false);
    const [health] = useEndpoint("/chat/health", 0);
    const logRef = React.useRef(null);
    const sessionId = React.useRef("ettok-dash-" + Math.random().toString(36).slice(2, 10));

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

      fetch(API + "/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: history, session_id: sessionId.current }),
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

            for (const frame of parts) {
              const parsed = parseFrame(frame);
              if (!parsed) continue;
              const event = parsed.event;
              const obj = parsed.data;

              if (event === "hermes.tool.progress") {
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

    return h("div", { style: C.wrap },
      (health && !health.available)
        ? h("div", { style: S.alert("warning") },
            "The agent gateway is not running, so there is nothing here to talk to. "
            + "Start it with `ettok gateway run`."
            + (health.reason ? "  (" + health.reason + ")" : ""))
        : null,

      h("div", { style: C.log, ref: logRef },
        messages.length === 0
          ? h("div", { style: S.muted },
              "Ask the agent about a case, a finding, or why something was or was not "
              + "flagged. Replies render as text, not terminal output.")
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
                      : h("div", { style: S.muted }, "thinking…"));
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
          style: Object.assign({}, C.send, (busy || !draft.trim()) ? { opacity: 0.45, cursor: "default" } : {}),
          onClick: send,
          disabled: busy || !draft.trim(),
        }, busy ? "…" : "Send")));
  }

  // ---- page -----------------------------------------------------------

  function Tab(props) {
    return h("button", {
      onClick: props.onClick,
      style: {
        padding: "6px 14px", borderRadius: "5px", cursor: "pointer", fontSize: "13px",
        fontWeight: 600, border: "1px solid rgba(128,128,128,0.25)",
        background: props.active ? "rgba(90,130,190,0.15)" : "transparent",
        color: props.active ? "rgb(90,130,190)" : "inherit",
      },
    }, props.label);
  }

  function EttokPage() {
    const [view, setView] = useState("monitoring");
    const [status, statusErr] = useEndpoint("/status", view === "monitoring" ? POLL_MS : 0);
    const [knowledge] = useEndpoint("/knowledge", POLL_MS * 4);
    const [reports] = useEndpoint("/reports", POLL_MS * 2);

    if (statusErr) {
      return h("div", { style: S.page },
        h("h1", { style: S.h1 }, "Ettok AI"),
        h("div", { style: S.alert("critical") },
          "Could not read the agent's state — " + statusErr));
    }
    if (!status) return h("div", { style: S.page }, h("div", { style: S.muted }, "Loading…"));

    const q = status.queue || {};
    return h("div", { style: S.page },
      h("div", null,
        h("h1", { style: S.h1 }, "Ettok AI"),
        h("p", { style: S.sub },
          "Hate speech monitoring for minority communities in Iraq. "
          + "This agent collects and reports; the platform decides."),
        h("div", { style: { display: "flex", gap: "8px", marginTop: "12px" } },
          h(Tab, { label: "Monitoring", active: view === "monitoring",
                   onClick: function () { setView("monitoring"); } }),
          h(Tab, { label: "Chat", active: view === "chat",
                   onClick: function () { setView("chat"); } }))),

      view === "chat" ? h(Chat, null) : null,

      view === "chat" ? null : h(Alerts, { alerts: status.alerts }),
      view === "chat" ? null : h(React.Fragment, null,
        h(Connection, { status: status }),

        h("div", { style: S.section },
          h("h2", { style: S.h2 }, "Delivery"),
          h("div", { style: S.grid },
            h(Stat, { label: "waiting to send", value: q.pending || 0 }),
            h(Stat, { label: "delivered", value: q.delivered || 0 }),
            h(Stat, {
              label: "failed permanently", value: q.failed_permanent || 0,
              tone: (q.failed_permanent ? "rgb(200,70,50)" : null),
            }),
            h(Stat, { label: "evidence held locally", value: status.evidence_pending || 0 }))),

        h("div", { style: S.section },
          h("h2", { style: S.h2 }, "Open cases"),
          h(Cases, { knowledge: knowledge })),

        h("div", { style: S.section },
          h("h2", { style: S.h2 }, "Findings on the platform"),
          h(Reports, { reports: reports })),

        h("div", { style: S.section },
          h("h2", { style: S.h2 }, "What it can detect"),
          h(Knowledge, { knowledge: knowledge })),

        h("div", { style: S.section },
          h("h2", { style: S.h2 }, "Monitoring accounts"),
          h(Accounts, { accounts: status.accounts })),

        h("div", { style: S.section },
          h("h2", { style: S.h2 }, "Recent runs"),
          h(Runs, { runs: status.runs }))));
  }

  window.__HERMES_PLUGINS__.register("ettok", EttokPage);
})();
