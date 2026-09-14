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

  // Where the last-open conversation is remembered between visits.
  const LAST_SESSION_KEY = "ettok-chat.last-session";

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

  // Anything a model or a gateway calls "text", as text.
  //
  // "" + {} is "[object Object]", which is how an error message became a
  // placeholder in the one place someone needed to read it.
  function textOf(value) {
    if (value === null || value === undefined) return "";
    if (typeof value === "string") return value;
    if (typeof value === "number" || typeof value === "boolean") return String(value);
    // Content blocks: [{type: "text", text: "..."}, ...]
    if (Array.isArray(value)) return value.map(textOf).join("");
    if (typeof value === "object") {
      // The common carriers, in the order they tend to appear.
      if (typeof value.text === "string") return value.text;
      if (typeof value.content === "string") return value.content;
      if (typeof value.message === "string") return value.message;
      if (Array.isArray(value.content)) return textOf(value.content);
      // Unknown shape: show it rather than hide it. A reader can act on JSON;
      // nobody can act on "[object Object]".
      try { return JSON.stringify(value); } catch (e) { return String(value); }
    }
    return String(value);
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

  // ---- styles ---------------------------------------------------------

  const MD_CSS = '.ettok-p{margin:0 0 9px}'
    + '.ettok-mdh{margin:13px 0 6px;font-weight:600}'
    + '.ettok-ul{margin:0 0 9px;padding-left:22px}.ettok-ul li{margin:3px 0}'
    + '.ettok-code{background:rgba(128,128,128,.16);padding:1px 5px;border-radius:3px;'
    + 'font-family:var(--theme-font-mono, ui-monospace, Menlo, monospace);font-size:.9em}'
    + '.ettok-pre{background:rgba(128,128,128,.12);padding:12px 14px;border-radius:5px;'
    + 'overflow-x:auto;margin:0 0 10px}'
    + '.ettok-pre code{font-family:var(--theme-font-mono, ui-monospace, Menlo, monospace);font-size:12.5px;line-height:1.5}';

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
    shell: { display: "flex", height: "100%", minHeight: 0 },

    // Sidebar
    side: { width: "232px", flexShrink: 0, borderRight: "1px solid rgba(128,128,128,0.2)",
            display: "flex", flexDirection: "column", padding: "14px 10px", gap: "8px",
            minHeight: 0 },
    newBtn: { padding: "8px 12px", borderRadius: "6px", cursor: "pointer", textAlign: "left",
              border: "1px solid rgba(128,128,128,0.3)", background: "transparent",
              color: "inherit", font: "inherit", fontSize: "13px", fontWeight: 600 },
    sideList: { flex: 1, overflowY: "auto", display: "flex", flexDirection: "column", gap: "2px",
                minHeight: 0 },
    sideItem: { padding: "7px 9px", borderRadius: "5px", cursor: "pointer", textAlign: "left",
                border: "none", background: "transparent", color: "inherit", font: "inherit",
                display: "block", width: "100%" },
    sideItemActive: { background: "rgba(90,130,190,0.14)" },
    sideTitle: { fontSize: "13px", fontWeight: 500, whiteSpace: "nowrap", overflow: "hidden",
                 textOverflow: "ellipsis" },

    // Main column
    page: { flex: 1, display: "flex", flexDirection: "column", maxWidth: "900px",
            margin: "0 auto", padding: "16px 24px", gap: "12px", minHeight: 0,
            boxSizing: "border-box" },
    head: { display: "flex", alignItems: "center", gap: "10px", flexWrap: "wrap" },
    h1: { fontSize: "18px", fontWeight: 600, margin: 0 },
    meta: { fontSize: "11.5px", opacity: 0.55, whiteSpace: "nowrap" },
    select: { background: "transparent", color: "inherit", font: "inherit", fontSize: "11.5px",
              border: "1px solid rgba(128,128,128,0.3)", borderRadius: "5px", padding: "3px 6px" },

    // Transcript
    log: { flex: 1, overflowY: "auto", display: "flex", flexDirection: "column", gap: "14px",
           paddingRight: "6px", minHeight: 0 },
    who: { fontSize: "11px", textTransform: "uppercase", letterSpacing: "0.07em",
           opacity: 0.55, fontWeight: 600, marginBottom: "3px" },
    body: { fontSize: "14px", lineHeight: 1.62 },
    userBody: { fontSize: "14px", lineHeight: 1.62, background: "rgba(128,128,128,0.10)",
                padding: "10px 14px", borderRadius: "6px" },
    tools: { display: "flex", flexDirection: "column", gap: "2px", fontSize: "12px",
             fontFamily: "var(--theme-font-mono, ui-monospace, Menlo, monospace)",
             opacity: 0.75, margin: "0 0 8px" },

    // Composer
    chips: { display: "flex", flexWrap: "wrap", gap: "6px" },
    chip: { display: "inline-flex", alignItems: "center", gap: "4px", fontSize: "12px",
            background: "rgba(128,128,128,0.14)", borderRadius: "4px", padding: "3px 4px 3px 8px" },
    chipX: { border: "none", background: "transparent", color: "inherit", cursor: "pointer",
             fontSize: "14px", lineHeight: 1, padding: "0 4px" },
    row: { display: "flex", gap: "8px", alignItems: "flex-end" },
    attach: { padding: "10px 12px", borderRadius: "6px", cursor: "pointer", fontSize: "15px",
              border: "1px solid rgba(128,128,128,0.3)", background: "transparent", color: "inherit" },
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

  function ago(iso) {
    if (!iso) return "";
    const t = typeof iso === "number" ? iso * 1000 : new Date(iso).getTime();
    const secs = Math.max(0, (Date.now() - t) / 1000);
    if (secs < 60) return "just now";
    if (secs < 3600) return Math.round(secs / 60) + "m ago";
    if (secs < 86400) return Math.round(secs / 3600) + "h ago";
    return Math.round(secs / 86400) + "d ago";
  }

  function compact(n) {
    if (!n) return "0";
    if (n < 1000) return String(n);
    if (n < 1000000) return (n / 1000).toFixed(n < 10000 ? 1 : 0) + "k";
    return (n / 1000000).toFixed(1) + "M";
  }

  /** Tokens in this conversation against what the model can hold.
   *
   *  Worth the screen space: the agent silently compresses a session that
   *  outgrows its window, and a long case discussion losing its early context
   *  looks like the agent forgetting rather than a limit being hit. */
  function ContextMeter(props) {
    const limit = props.limit || 0;
    if (!limit) return null;
    // Occupancy cannot exceed the window: the runtime compresses a session
    // before it would. A number above the limit therefore means the meter is
    // being fed the wrong quantity -- it happened once, with cumulative spend
    // wired in here -- so refuse to render rather than print an impossibility
    // and teach the reader to distrust the panel.
    const raw = props.used || 0;
    if (raw > limit) return null;
    const used = raw;
    const pct = Math.min(100, (used / limit) * 100);
    const tone = pct > 85 ? "rgb(200,70,50)" : pct > 60 ? "rgb(180,120,30)" : "rgb(90,130,190)";
    return h("div", { style: { display: "flex", alignItems: "center", gap: "7px" },
                      title: "Conversation: " + used.toLocaleString() + " of "
                             + limit.toLocaleString() + " tokens.\n"
                             + "Counts the stored messages only. The system prompt and tool "
                             + "definitions are resident in every request on top of this "
                             + "(~14k here) and are not included, so treat it as a floor." },
      h("div", { style: { width: "54px", height: "4px", borderRadius: "2px",
                          background: "rgba(128,128,128,0.25)", overflow: "hidden" } },
        h("div", { style: { width: pct + "%", height: "100%", background: tone } })),
      h("span", { style: C.meta }, compact(used) + " / " + compact(limit)));
  }

  function Sessions(props) {
    return h("div", { style: C.side },
      h("button", {
        style: C.newBtn,
        onClick: props.onNew,
      }, "+  New chat"),
      h("div", { style: C.sideList },
        (props.sessions || []).length === 0
          ? h("div", { style: Object.assign({}, C.meta, { padding: "8px 4px" }) },
              "No conversations yet.")
          : props.sessions.map(function (s) {
              const active = s.id === props.activeId;
              return h("button", {
                key: s.id,
                onClick: function () { props.onOpen(s.id); },
                style: Object.assign({}, C.sideItem, active ? C.sideItemActive : {}),
                title: s.preview || s.title || s.id,
              },
                h("div", { style: C.sideTitle }, s.title || "Untitled"),
                h("div", { style: C.meta },
                  ago(s.last_activity_at || s.started_at)
                  + (s.message_count ? "  ·  " + s.message_count + " msgs" : "")));
            })));
  }

  function EttokChatPage() {
    useMarkdownStyles();
    const [messages, setMessages] = useState([]);
    const [draft, setDraft] = useState("");
    const [busy, setBusy] = useState(false);
    // The in-flight turn, so it can be called off. A ref rather than state:
    // aborting must not wait for a re-render, and nothing renders from it.
    const abortRef = useRef(null);
    const [health, setHealth] = useState(null);
    const [model, setModel] = useState(null);
    const [sessions, setSessions] = useState([]);
    // Which conversation this tab is in, kept across reloads. Held in component
    // state alone, it was lost the moment the page unmounted -- navigate to
    // Sessions and back, or reload, and the chat opened blank while the
    // conversation sat in the sidebar unreferenced. It reads as lost history,
    // and the messages were never gone.
    const [sessionId, setSessionId] = useState(function () {
      try { return window.localStorage.getItem(LAST_SESSION_KEY) || null; }
      catch (e) { return null; }
    });

    useEffect(function () {
      // Per-viewer convenience, so localStorage is right -- and it can throw in
      // a private window or with site data blocked, which must not take the
      // chat down with it.
      try {
        if (sessionId) window.localStorage.setItem(LAST_SESSION_KEY, sessionId);
        else window.localStorage.removeItem(LAST_SESSION_KEY);
      } catch (e) { /* not worth a broken page */ }
    }, [sessionId]);
    const [effort, setEffort] = useState("");
    const [attachments, setAttachments] = useState([]);
    const [usedTokens, setUsedTokens] = useState(0);
    const [turnSpend, setTurnSpend] = useState(0);
    const logRef = useRef(null);
    const fileRef = useRef(null);

    const loadSessions = useCallback(function () {
      // The gateway tags everything it runs as `api_server`, which is exactly
      // this chat plus anything else on the OpenAI-compatible endpoint. Cron
      // runs carry their own source and stay out of the list.
      SDK.fetchJSON("/api/sessions?limit=30&order=recent&source=api_server&min_messages=1")
        .then(function (d) { setSessions((d && d.sessions) || []); })
        .catch(function () { /* the list is a convenience; chat still works */ });
    }, []);

    useEffect(function () {
      SDK.fetchJSON(API + "/chat/health").then(setHealth)
        .catch(function (e) { setHealth({ available: false, reason: String(e) }); });
      SDK.fetchJSON("/api/model/info").then(setModel).catch(function () {});
      loadSessions();
    }, [loadSessions]);

    useEffect(function () {
      if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
    }, [messages]);

    const refreshOccupancy = useCallback(function (id) {
      // The only honest source of "how full is the window": the per-message
      // token counts the runtime stored for this session. Summed, that is what
      // the next request will carry. Cumulative usage counters cannot answer
      // this -- they count what was billed, not what is resident.
      SDK.fetchJSON("/api/sessions/" + encodeURIComponent(id) + "/messages?limit=500&order=oldest")
        .then(function (d) {
          let used = 0;
          for (const m of (d && d.messages) || []) used += m.token_count || 0;
          setUsedTokens(used);
        })
        .catch(function () { /* the meter is advisory; a failure just leaves it */ });
    }, []);

    const openSession = useCallback(function (id) {
      setSessionId(id);
      setMessages([{ role: "assistant", content: "_Loading…_" }]);
      SDK.fetchJSON("/api/sessions/" + encodeURIComponent(id) + "/messages?limit=200&order=oldest")
        .then(function (d) {
          const rows = (d && d.messages) || [];
          const out = [];
          let used = 0;
          for (const m of rows) {
            used += m.token_count || 0;
            if (m.role !== "user" && m.role !== "assistant") continue;
            let text = m.content;
            if (Array.isArray(text)) {
              // Stored multimodal turns: keep the words, name the rest.
              text = text.map(function (b) {
                return b && b.type === "text" ? b.text : "_[" + ((b && b.type) || "attachment") + "]_";
              }).join("\n");
            }
            if (!text) continue;
            out.push({ role: m.role, content: String(text) });
          }
          setMessages(out);
          setUsedTokens(used);
        })
        .catch(function (e) {
          setMessages([{ role: "assistant", content: "**Could not load that conversation.** " + e }]);
        });
    }, []);

    // Reopen whatever was last open, once, on arrival. Guarded by a ref rather
    // than an empty dependency list so a re-render cannot restart it and
    // overwrite a turn already in flight.
    const restored = useRef(false);
    useEffect(function () {
      if (restored.current) return;
      restored.current = true;
      if (sessionId) openSession(sessionId);
    }, [sessionId, openSession]);

    const newChat = useCallback(function () {
      setSessionId(null);
      setMessages([]);
      setUsedTokens(0);
      setTurnSpend(0);
      setAttachments([]);
    }, []);

    const addFiles = useCallback(function (fileList) {
      // Read locally into data URLs. The agent is a local process, but the file
      // still has to travel as part of the message -- there is no shared path
      // between a browser file picker and the gateway.
      for (const file of Array.from(fileList || [])) {
        if (file.size > 8 * 1024 * 1024) {
          setAttachments(function (a) {
            return a.concat([{ name: file.name, error: "larger than 8 MB" }]);
          });
          continue;
        }
        const reader = new FileReader();
        reader.onload = function () {
          setAttachments(function (a) {
            return a.concat([{ name: file.name, type: file.type, dataUrl: reader.result }]);
          });
        };
        reader.readAsDataURL(file);
      }
    }, []);

    const send = useCallback(function () {
      const text = draft.trim();
      const usable = attachments.filter(function (a) { return a.dataUrl; });
      if ((!text && !usable.length) || busy) return;

      // OpenAI content blocks when there is anything but text, a plain string
      // otherwise -- some providers reject the block form for text-only turns.
      let content = text;
      if (usable.length) {
        content = [{ type: "text", text: text || "(see attachment)" }].concat(
          usable.map(function (a) {
            return a.type && a.type.indexOf("image/") === 0
              ? { type: "image_url", image_url: { url: a.dataUrl } }
              : { type: "text", text: "[attached file: " + a.name + "]" };
          }));
      }

      const shown = text + (usable.length
        ? "\n\n" + usable.map(function (a) { return "`📎 " + a.name + "`"; }).join(" ")
        : "");
      const history = messages.concat([{ role: "user", content: shown }]);
      setMessages(history.concat([{ role: "assistant", content: "" }]));
      setDraft("");
      setAttachments([]);
      setBusy(true);

      const wire = messages.map(function (m) { return { role: m.role, content: m.content }; })
        .concat([{ role: "user", content: content }]);

      const controller = new AbortController();
      abortRef.current = controller;

      SDK.authedFetch(API + "/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          messages: wire,
          resume_session_id: sessionId || "",
          reasoning_effort: effort || undefined,
        }),
        signal: controller.signal,
      }).then(function (res) {
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let acc = "";
        let tools = [];
        let landed = sessionId;

        function pump() {
          return reader.read().then(function (r) {
            if (r.done) {
              abortRef.current = null;
              setBusy(false);
              // Titles, counts and per-message token counts are written as the
              // turn completes, so both refreshes wait for it.
              setTimeout(function () {
                loadSessions();
                if (landed) refreshOccupancy(landed);
              }, 800);
              return;
            }
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
              } else if (obj.session_id) {
                // Which session this turn landed in; send it back to continue.
                landed = obj.session_id;
                if (!sessionId) setSessionId(obj.session_id);
                continue;
              } else if (obj.error) {
                acc += "\n\n**" + textOf(obj.error) + "**"
                     + (obj.hint ? "\n\n" + textOf(obj.hint) : "");
              } else {
                const choice = (obj.choices || [])[0] || {};
                const delta = choice.delta || choice.message || {};
                // reasoning_content is what a thinking model streams before its
                // answer. Shown, because a reply that takes thirty seconds with
                // an empty bubble reads as broken.
                if (delta.reasoning_content) acc += textOf(delta.reasoning_content);
                if (delta.content) acc += textOf(delta.content);
                // Deliberately NOT fed to the context meter. `usage` is the
                // agent's, summed across every model call in the turn: a reply
                // that ran three shell commands reports ~28k prompt tokens
                // against a context that never exceeded ~15k, because each tool
                // round-trip resends the conversation. Summing that across a
                // long session climbs past the window and reads as impossible.
                // It is real spend, so it is shown as spend.
                if (obj.usage && obj.usage.total_tokens) setTurnSpend(obj.usage.total_tokens);
              }
              setMessages(history.concat([{ role: "assistant", content: acc, tools: tools }]));
            }
            return pump();
          });
        }
        return pump();
      }).catch(function (e) {
        abortRef.current = null;
        setBusy(false);
        // Stopping is a decision, not an error. Whatever had already streamed
        // is kept -- the half-written answer is usually why it was stopped, and
        // throwing it away to show an error message would be the second
        // annoyance in a row.
        if (e && (e.name === "AbortError" || controller.signal.aborted)) {
          setMessages(function (prev) {
            const last = prev[prev.length - 1] || {};
            const partial = (last.content || "").trim();
            return prev.slice(0, -1).concat([{
              role: "assistant",
              content: (partial ? partial + "\n\n" : "") + "_Stopped._",
              tools: last.tools,
            }]);
          });
          return;
        }
        setMessages(history.concat([{
          role: "assistant",
          content: "**Could not reach the agent.** " + (e.message || String(e)),
        }]));
      });
    }, [draft, busy, messages, sessionId, effort, attachments, loadSessions]);

    const stop = useCallback(function () {
      const controller = abortRef.current;
      if (!controller) return;
      abortRef.current = null;
      // Aborting closes the response body, which ends the proxy's relay and
      // with it the connection to the agent. The turn already in the model's
      // hands may finish server-side; what stops immediately is this turn's
      // hold on the page.
      controller.abort();
    }, []);

    const vision = model && model.capabilities && model.capabilities.supports_vision;
    const reasoning = model && model.capabilities && model.capabilities.supports_reasoning;

    return h("div", { style: C.shell },
      h(Sessions, { sessions: sessions, activeId: sessionId, onOpen: openSession, onNew: newChat }),

      h("div", { style: C.page },
        h("div", { style: C.head },
          h("h1", { style: C.h1 }, "Ettok AI"),
          model && model.model
            ? h("span", { style: C.meta }, model.provider + " · " + model.model)
            : null,
          h("div", { style: { flex: 1 } }),
          h(ContextMeter, { used: usedTokens, limit: model && model.effective_context_length }),
          turnSpend
            ? h("span", { style: C.meta, title: "Tokens billed for the last reply, summed "
                                               + "across every model call the agent made in it" },
                compact(turnSpend) + " spent")
            : null,
          reasoning
            ? h("select", {
                style: C.select, value: effort,
                title: "How long the agent thinks before answering",
                onChange: function (e) { setEffort(e.target.value); },
              },
                h("option", { value: "" }, "effort: default"),
                h("option", { value: "low" }, "effort: low"),
                h("option", { value: "medium" }, "effort: medium"),
                h("option", { value: "high" }, "effort: high"))
            : null),

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

        attachments.length
          ? h("div", { style: C.chips }, attachments.map(function (a, i) {
              return h("span", { key: i, style: C.chip },
                (a.error ? "⚠ " : "📎 ") + a.name + (a.error ? " — " + a.error : ""),
                h("button", {
                  style: C.chipX,
                  onClick: function () {
                    setAttachments(attachments.filter(function (_, j) { return j !== i; }));
                  },
                }, "×"));
            }))
          : null,

        h("div", { style: C.row },
          h("input", {
            ref: fileRef, type: "file", multiple: true, style: { display: "none" },
            onChange: function (e) { addFiles(e.target.files); e.target.value = ""; },
          }),
          h("button", {
            style: Object.assign({}, C.attach, vision ? {} : { opacity: 0.4 }),
            onClick: function () { if (fileRef.current) fileRef.current.click(); },
            title: vision
              ? "Attach an image or file"
              : "This model does not accept images; files are sent as a note naming them",
          }, "📎"),
          h("textarea", {
            style: C.input,
            value: draft,
            placeholder: "Ask the agent…  (Enter to send, Shift+Enter for a new line)",
            onChange: function (e) { setDraft(e.target.value); },
            onPaste: function (e) {
              const files = e.clipboardData && e.clipboardData.files;
              if (files && files.length) { e.preventDefault(); addFiles(files); }
            },
            onKeyDown: function (e) {
              if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
            },
          }),
          h("button", {
            style: Object.assign({}, C.send,
              (!busy && !draft.trim() && !attachments.length) ? { opacity: 0.45, cursor: "default" } : {}),
            onClick: busy ? stop : send,
            disabled: !busy && !draft.trim() && !attachments.length,
            title: busy ? "Stop the agent" : "Send",
          }, busy ? "Stop" : "Send"))));
  }

  window.__HERMES_PLUGINS__.register("ettok-chat", EttokChatPage);
})();
