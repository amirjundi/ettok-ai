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

  const LAST_SESSION_KEY = "ettok-chat.last-session";
  // Where the turn currently in flight is kept.
  //
  // A message is only written into the session when its turn COMPLETES. Leave
  // the page mid-turn and come back and the transcript is the one from before
  // you asked: your question is not in it, the answer is not in it, and the
  // agent looks like it never heard you -- while it is in fact still working.
  // Holding the question and whatever has streamed so far here means the page
  // can show the turn it is actually in, and hand over to the stored copy as
  // soon as the server has one.
  const INFLIGHT_KEY = "ettok-chat.inflight";

  function readInflight() {
    try {
      const raw = window.localStorage.getItem(INFLIGHT_KEY);
      if (!raw) return null;
      const v = JSON.parse(raw);
      // Anything older than half an hour is a turn that died with a closed tab.
      if (!v || !v.at || Date.now() - v.at > 30 * 60 * 1000) return null;
      return v;
    } catch (e) { return null; }
  }

  function writeInflight(v) {
    try {
      if (v) window.localStorage.setItem(INFLIGHT_KEY, JSON.stringify(v));
      else window.localStorage.removeItem(INFLIGHT_KEY);
    } catch (e) { /* a private window must not break the chat */ }
  }

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const React = SDK.React;
  const h = React.createElement;
  const { useState, useEffect, useCallback, useRef, useMemo } = React;

  const API = "/api/plugins/ettok";

  // A session's `source` is the platform it happened on: the gateway stores
  // `platform.value` for every messaging adapter and "api_server" for this
  // page. Ordered so the channel you are typing in sits first.
  const CHANNELS = [
    { key: "api_server", label: "Dashboard", icon: "▣", live: true },
    { key: "telegram", label: "Telegram", icon: "✈" },
    { key: "whatsapp", label: "WhatsApp", icon: "✆" },
    { key: "whatsapp_cloud", label: "WhatsApp Business", icon: "✆" },
    { key: "discord", label: "Discord", icon: "◈" },
    { key: "signal", label: "Signal", icon: "◉" },
    { key: "webhook", label: "Webhook", icon: "⇥" },
    { key: "local", label: "Terminal", icon: "▮" },
  ];
  const CHANNEL_BY_KEY = {};
  for (const c of CHANNELS) CHANNEL_BY_KEY[c.key] = c;

  // fetchJSON throws Error("<status>: <body>"), and the body is usually JSON.
  // Nobody should read a status line and a brace to find out what went wrong.
  function reason(err) {
    const raw = (err && err.message) || "";
    const body = raw.replace(/^\d{3}:\s*/, "");
    try {
      const parsed = JSON.parse(body);
      const detail = parsed.detail || (parsed.error && parsed.error.message) || parsed.message;
      if (detail) return String(detail);
    } catch (e) { /* not JSON; the text itself is the message */ }
    return body || "The question may have timed out.";
  }

  function channelOf(source) {
    const key = String(source || "").toLowerCase();
    return CHANNEL_BY_KEY[key]
      || { key: key || "other", label: key ? key.replace(/_/g, " ") : "Other",
           icon: "•" };
  }

  // ---- markdown -------------------------------------------------------
  //
  // Block-level: fenced code, headings, ordered and unordered lists (nested by
  // indent), tables, blockquotes, rules. Enough that an agent writing an
  // ordinary structured answer is rendered as one, rather than as a wall of
  // asterisks.

  function escapeHtml(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function inlineMd(s) {
    let out = escapeHtml(s);
    // Code first: whatever is inside a span of backticks is literal, so it must
    // be lifted out before emphasis and links can claim any of it.
    const spans = [];
    out = out.replace(/`([^`]+)`/g, function (_m, code) {
      spans.push(code);
      return "\u0000" + (spans.length - 1) + "\u0000";
    });
    out = out
      .replace(/\*\*\*([^*]+)\*\*\*/g, "<strong><em>$1</em></strong>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      .replace(/(^|[\s(])_([^_\n]+)_/g, "$1<em>$2</em>")
      .replace(/~~([^~]+)~~/g, "<del>$1</del>")
      // Links: only http(s) and mailto. A bare [x](javascript:...) in model
      // output must not become a live handler.
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+|mailto:[^\s)]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    return out.replace(/\u0000(\d+)\u0000/g, function (_m, i) {
      return '<code class="ettok-code">' + spans[Number(i)] + "</code>";
    });
  }

  function renderMarkdown(text) {
    const lines = String(text == null ? "" : text).split("\n");
    const out = [];
    let i = 0;

    // A list level: its kind, the indent it opened at, and whether an <li> is
    // still open on it. Closing a level has to close that <li> first, or the
    // nested list ends up outside the item it belongs to.
    const levels = [];

    function closeLevel() {
      const lv = levels.pop();
      if (lv.liOpen) out.push("</li>");
      out.push(lv.kind === "ol" ? "</ol>" : "</ul>");
    }
    function endLists() { while (levels.length) closeLevel(); }

    while (i < lines.length) {
      const line = lines[i];

      // Fenced code. An unclosed fence is the normal state mid-stream, so the
      // rest of the buffer is treated as code rather than dropped.
      const fence = line.match(/^\s*```+\s*([A-Za-z0-9_+-]*)\s*$/);
      if (fence) {
        endLists();
        const lang = fence[1];
        const body = [];
        i += 1;
        while (i < lines.length && !/^\s*```+\s*$/.test(lines[i])) { body.push(lines[i]); i += 1; }
        i += 1;
        out.push('<pre class="ettok-pre"' + (lang ? ' data-lang="' + escapeHtml(lang) + '"' : "")
                 + "><code>" + escapeHtml(body.join("\n")) + "</code></pre>");
        continue;
      }

      // Table: a header row followed by a |---|---| separator.
      if (/^\s*\|/.test(line) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
        endLists();
        const cells = function (row) {
          return row.trim().replace(/^\||\|$/g, "").split("|").map(function (c) { return c.trim(); });
        };
        const head = cells(line);
        i += 2;
        const rows = [];
        while (i < lines.length && /^\s*\|/.test(lines[i])) { rows.push(cells(lines[i])); i += 1; }
        out.push('<div class="ettok-tablewrap"><table class="ettok-table"><thead><tr>'
          + head.map(function (c) { return "<th>" + inlineMd(c) + "</th>"; }).join("")
          + "</tr></thead><tbody>"
          + rows.map(function (r) {
              return "<tr>" + r.map(function (c) { return "<td>" + inlineMd(c) + "</td>"; }).join("") + "</tr>";
            }).join("")
          + "</tbody></table></div>");
        continue;
      }

      const heading = line.match(/^(#{1,6})\s+(.*)$/);
      if (heading) {
        endLists();
        const lvl = Math.min(4, heading[1].length);
        out.push('<div class="ettok-mdh ettok-mdh' + lvl + '">' + inlineMd(heading[2]) + "</div>");
        i += 1;
        continue;
      }

      if (/^\s*(?:---+|\*\*\*+|___+)\s*$/.test(line)) {
        endLists();
        out.push('<hr class="ettok-hr">');
        i += 1;
        continue;
      }

      const quote = line.match(/^\s*>\s?(.*)$/);
      if (quote) {
        endLists();
        const body = [quote[1]];
        i += 1;
        while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
          body.push(lines[i].replace(/^\s*>\s?/, "")); i += 1;
        }
        out.push('<blockquote class="ettok-quote">'
                 + body.map(function (b) { return inlineMd(b); }).join("<br>") + "</blockquote>");
        continue;
      }

      const item = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
      if (item) {
        const indent = item[1].replace(/\t/g, "  ").length;
        const kind = /^\d/.test(item[2]) ? "ol" : "ul";

        // Shallower than the level we are on: close levels until we are back.
        while (levels.length && indent < levels[levels.length - 1].indent) closeLevel();

        const top = levels[levels.length - 1];
        if (top && indent === top.indent) {
          if (top.kind !== kind) {
            // A numbered list starting where a bulleted one was: a new list.
            closeLevel();
          } else {
            if (top.liOpen) { out.push("</li>"); top.liOpen = false; }
          }
        }

        const cur = levels[levels.length - 1];
        if (!cur || indent > cur.indent) {
          // Deeper: a child list, which belongs inside the parent's open <li>.
          levels.push({ kind: kind, indent: indent, liOpen: false });
          out.push(kind === "ol" ? '<ol class="ettok-ol">' : '<ul class="ettok-ul">');
        }

        const lv = levels[levels.length - 1];
        out.push("<li>" + inlineMd(item[3]));
        lv.liOpen = true;
        i += 1;
        continue;
      }

      if (!line.trim()) { endLists(); i += 1; continue; }

      endLists();
      out.push('<p class="ettok-p">' + inlineMd(line) + "</p>");
      i += 1;
    }

    endLists();
    return out.join("");
  }

  // ---- the wire -------------------------------------------------------

  // Anything a model or a gateway calls "text", as text.
  //
  // "" + {} is "[object Object]", which is how an error message became a
  // placeholder in the one place someone needed to read it.
  function textOf(value) {
    if (value == null) return "";
    if (typeof value === "string") return value;
    if (Array.isArray(value)) return value.map(textOf).join("");
    if (typeof value === "object") {
      if (typeof value.text === "string") return value.text;
      if (typeof value.content === "string") return value.content;
      if (typeof value.message === "string") return value.message;
      if (value.content) return textOf(value.content);
      try { return JSON.stringify(value); } catch (e) { return String(value); }
    }
    return String(value);
  }

  function parseFrame(frame) {
    // "message" is the SSE default for a frame with no event: line, and the
    // tool-progress branch distinguishes itself by name.
    let event = "message";
    const data = [];
    for (const raw of frame.split("\n")) {
      if (raw.startsWith("event:")) event = raw.slice(6).trim();
      else if (raw.startsWith("data:")) data.push(raw.slice(5).trim());
    }
    const payload = data.join("\n");
    if (!payload || payload === "[DONE]") return null;
    try { return { event: event, data: JSON.parse(payload) }; }
    catch (e) { return null; }
  }

  // ---- token estimate -------------------------------------------------

  // The runtime has a token_count column on every message and never writes to
  // it: all 258 rows on this install are NULL. Summing it produced a meter that
  // read zero for ever, which is worse than no meter -- it says "plenty of room"
  // with the same confidence whatever is true.
  //
  // So estimate from the text, and label it as an estimate. ~3.6 characters per
  // token is a reasonable middle for this content: English runs nearer 4, and
  // the Arabic these conversations are full of is denser than that.
  function estimateTokens(value) {
    const text = typeof value === "string" ? value : textOf(value);
    if (!text) return 0;
    return Math.ceil(text.length / 3.6);
  }

  const MD_CSS = '.ettok-p{margin:0 0 9px}'
    + '.ettok-p:last-child{margin-bottom:0}'
    + '.ettok-mdh{font-weight:650;margin:14px 0 7px;line-height:1.35}'
    + '.ettok-mdh1{font-size:1.28em}.ettok-mdh2{font-size:1.16em}'
    + '.ettok-mdh3{font-size:1.06em}.ettok-mdh4{font-size:1em;opacity:.85}'
    + '.ettok-ul,.ettok-ol{margin:0 0 9px;padding-left:22px}'
    + '.ettok-ul{list-style:disc}.ettok-ol{list-style:decimal}'
    + '.ettok-ul .ettok-ul{list-style:circle;margin:4px 0}'
    + '.ettok-ol .ettok-ol{list-style:lower-alpha;margin:4px 0}'
    + '.ettok-ul li,.ettok-ol li{margin:3px 0}'
    + '.ettok-code{font-family:var(--theme-font-mono, ui-monospace, Menlo, monospace);'
    + 'font-size:.9em;background:rgba(128,128,128,.16);padding:1px 5px;border-radius:4px}'
    + '.ettok-pre{margin:0 0 10px;padding:11px 13px;border-radius:7px;overflow-x:auto;'
    + 'background:rgba(128,128,128,.13);border:1px solid rgba(128,128,128,.2)}'
    + '.ettok-pre code{font-family:var(--theme-font-mono, ui-monospace, Menlo, monospace);'
    + 'font-size:12.5px;line-height:1.6;background:none;padding:0}'
    + '.ettok-pre[data-lang]::before{content:attr(data-lang);display:block;'
    + 'font-size:10px;text-transform:uppercase;letter-spacing:.08em;opacity:.5;margin-bottom:6px}'
    + '.ettok-quote{margin:0 0 10px;padding:2px 0 2px 12px;'
    + 'border-left:3px solid rgba(128,128,128,.4);opacity:.85}'
    + '.ettok-hr{border:none;border-top:1px solid rgba(128,128,128,.28);margin:14px 0}'
    + '.ettok-tablewrap{overflow-x:auto;margin:0 0 10px}'
    + '.ettok-table{border-collapse:collapse;font-size:13px;width:100%}'
    + '.ettok-table th,.ettok-table td{border:1px solid rgba(128,128,128,.25);'
    + 'padding:6px 10px;text-align:left;vertical-align:top}'
    + '.ettok-table th{background:rgba(128,128,128,.12);font-weight:600}'
    + '.ettok-body a{color:rgb(90,130,190)}'
    + '@keyframes ettok-pulse{0%,100%{opacity:.35}50%{opacity:1}}'
    + '.ettok-live{animation:ettok-pulse 1.4s ease-in-out infinite}';

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
    sideGroup: { display: "flex", justifyContent: "space-between", alignItems: "center",
                 fontSize: "10.5px", textTransform: "uppercase", letterSpacing: "0.08em",
                 fontWeight: 700, opacity: 0.5, padding: "12px 9px 4px" },
    sideTitle: { fontSize: "13px", fontWeight: 500, whiteSpace: "nowrap", overflow: "hidden",
                 textOverflow: "ellipsis" },

    page: { flex: 1, display: "flex", flexDirection: "column", maxWidth: "900px",
            margin: "0 auto", padding: "16px 24px", gap: "12px", minHeight: 0,
            boxSizing: "border-box", position: "relative" },
    head: { display: "flex", alignItems: "center", gap: "10px", flexWrap: "wrap" },
    h1: { fontSize: "18px", fontWeight: 600, margin: 0 },
    meta: { fontSize: "11.5px", opacity: 0.55, whiteSpace: "nowrap" },
    select: { background: "transparent", color: "inherit", font: "inherit", fontSize: "11.5px",
              border: "1px solid rgba(128,128,128,0.3)", borderRadius: "5px", padding: "3px 6px" },

    log: { flex: 1, overflowY: "auto", display: "flex", flexDirection: "column", gap: "14px",
           paddingRight: "6px", minHeight: 0 },
    who: { fontSize: "11px", textTransform: "uppercase", letterSpacing: "0.07em",
           opacity: 0.55, fontWeight: 600, marginBottom: "3px" },
    body: { fontSize: "14px", lineHeight: 1.62 },
    userBody: { fontSize: "14px", lineHeight: 1.62, background: "rgba(128,128,128,0.10)",
                padding: "10px 14px", borderRadius: "6px" },

    // Activity
    act: { display: "flex", flexDirection: "column", gap: "3px", margin: "0 0 9px",
           padding: "8px 11px", borderRadius: "7px", background: "rgba(90,130,190,0.08)",
           border: "1px solid rgba(90,130,190,0.22)" },
    actRow: { display: "flex", alignItems: "center", gap: "7px", fontSize: "12.5px" },
    actLabel: { fontFamily: "var(--theme-font-mono, ui-monospace, Menlo, monospace)",
                fontSize: "12px" },
    done: { display: "inline-flex", alignItems: "center", gap: "6px", fontSize: "11.5px",
            opacity: 0.6, marginTop: "6px" },

    queued: { display: "flex", alignItems: "center", gap: "8px", fontSize: "12.5px",
              padding: "7px 11px", borderRadius: "6px",
              background: "rgba(180,120,30,0.10)", border: "1px solid rgba(180,120,30,0.28)" },

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
    stopBtn: { padding: "11px 20px", borderRadius: "6px", cursor: "pointer", fontWeight: 600,
               border: "1px solid rgba(200,70,50,0.5)", background: "transparent",
               color: "rgb(200,70,50)", font: "inherit" },

    palette: { position: "absolute", left: "24px", right: "24px", bottom: "86px", zIndex: 30,
               borderRadius: "8px", overflow: "hidden", maxHeight: "244px", overflowY: "auto",
               border: "1px solid rgba(128,128,128,0.32)",
               background: "var(--theme-bg-elevated, var(--theme-bg, #1b1b1b))",
               boxShadow: "0 10px 30px rgba(0,0,0,0.28)" },
    palItem: { display: "flex", gap: "10px", alignItems: "baseline", width: "100%",
               padding: "9px 13px", border: "none", background: "transparent", color: "inherit",
               font: "inherit", textAlign: "left", cursor: "pointer" },
    palItemOn: { background: "rgba(90,130,190,0.18)" },
    palCmd: { fontFamily: "var(--theme-font-mono, ui-monospace, Menlo, monospace)",
              fontSize: "12.5px", fontWeight: 600, minWidth: "92px" },
    palWhat: { fontSize: "12.5px", opacity: 0.72 },

    // A pending question: the only thing on the page the agent is waiting on, so
    // it carries the accent rather than sitting in the same grey as everything else.
    ask: { display: "flex", flexDirection: "column", gap: "9px",
           padding: "14px 16px", borderRadius: "8px",
           background: "rgba(90,130,190,0.08)",
           border: "1px solid rgba(90,130,190,0.35)" },
    askDanger: { display: "flex", flexDirection: "column", gap: "9px",
                 padding: "14px 16px", borderRadius: "8px",
                 background: "rgba(200,60,60,0.10)",
                 border: "1px solid rgba(200,60,60,0.45)" },
    askHead: { fontSize: "10.5px", textTransform: "uppercase", letterSpacing: "0.08em",
               fontWeight: 700, opacity: 0.6 },
    askQ: { fontSize: "14.5px", lineHeight: 1.45, fontWeight: 600 },
    askRows: { display: "flex", flexDirection: "column", gap: "6px" },
    askBtn: { display: "flex", alignItems: "center", gap: "9px", width: "100%",
              textAlign: "left", font: "inherit", fontSize: "13.5px", cursor: "pointer",
              padding: "9px 12px", borderRadius: "6px",
              background: "var(--card, rgba(128,128,128,0.08))",
              border: "1px solid rgba(128,128,128,0.3)" },
    askBtnOn: { borderColor: "rgb(90,130,190)", background: "rgba(90,130,190,0.16)" },
    askBox: { display: "inline-flex", alignItems: "center", justifyContent: "center",
              width: "15px", height: "15px", flex: "0 0 auto", fontSize: "11px",
              borderRadius: "3px", border: "1px solid rgba(128,128,128,0.55)" },
    askSend: { alignSelf: "flex-start", font: "inherit", fontSize: "13px", cursor: "pointer",
               padding: "7px 14px", borderRadius: "6px", border: "none", color: "#fff",
               background: "rgb(90,130,190)" },

    // Shell, as shell: monospace, its own ground, and scrollable rather than
    // pushing the conversation sideways.
    actShell: { fontSize: "12px", whiteSpace: "pre-wrap", wordBreak: "break-all",
                background: "transparent", padding: 0 },
    shellWrap: { display: "flex", flexDirection: "column", gap: "4px",
                 margin: "2px 0 6px 21px" },
    shellToggle: { alignSelf: "flex-start", border: "none", background: "transparent",
                   font: "inherit", fontSize: "11.5px", opacity: 0.6, cursor: "pointer",
                   padding: 0 },
    shellOut: { margin: 0, maxHeight: "240px", overflow: "auto", fontSize: "11.5px",
                lineHeight: 1.5, padding: "8px 10px", borderRadius: "5px",
                whiteSpace: "pre-wrap", wordBreak: "break-word",
                fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
                background: "rgba(128,128,128,0.10)",
                border: "1px solid rgba(128,128,128,0.2)" },

    readonly: { display: "flex", flexWrap: "wrap", alignItems: "baseline", gap: "5px",
                fontSize: "13px", padding: "11px 14px", borderRadius: "6px",
                background: "rgba(128,128,128,0.10)",
                border: "1px solid rgba(128,128,128,0.25)" },
    linkBtn: { border: "none", background: "transparent", color: "rgb(90,130,190)",
               font: "inherit", fontSize: "13px", cursor: "pointer", padding: 0,
               textDecoration: "underline" },
    warn: { background: "rgba(200,140,40,0.12)", borderLeft: "3px solid rgb(180,120,30)",
            color: "rgb(180,120,30)", padding: "10px 14px", borderRadius: "0 4px 4px 0",
            fontSize: "13px" },
    muted: { opacity: 0.6, fontSize: "13px" },
  };

  function Markdown(props) {
    return h("div", {
      className: "ettok-body",
      style: props.role === "user" ? C.userBody : C.body,
      dangerouslySetInnerHTML: { __html: renderMarkdown(props.text) },
    });
  }

  function ago(iso) {
    if (!iso) return "";
    const then = new Date(iso).getTime();
    if (!then) return "";
    const mins = Math.max(0, Math.round((Date.now() - then) / 60000));
    if (mins < 1) return "just now";
    if (mins < 60) return mins + "m ago";
    const hrs = Math.round(mins / 60);
    if (hrs < 24) return hrs + "h ago";
    return Math.round(hrs / 24) + "d ago";
  }

  function compact(n) {
    if (!n) return "0";
    if (n < 1000) return String(n);
    if (n < 1000000) return (n / 1000).toFixed(n < 10000 ? 1 : 0) + "k";
    return (n / 1000000).toFixed(1) + "M";
  }

  function secs(ms) {
    const s = Math.round(ms / 100) / 10;
    return s < 60 ? s + "s" : Math.floor(s / 60) + "m " + Math.round(s % 60) + "s";
  }

  function ContextMeter(props) {
    const limit = props.limit || 0;
    if (!limit) return null;
    const used = Math.max(0, props.used || 0);
    const pct = Math.min(100, (used / limit) * 100);
    const tone = pct > 85 ? "rgb(200,70,50)" : pct > 60 ? "rgb(180,120,30)" : "rgb(90,130,190)";
    return h("div", { style: { display: "flex", alignItems: "center", gap: "7px" },
                      title: "Roughly " + used.toLocaleString() + " of "
                             + limit.toLocaleString() + " tokens of conversation.\n\n"
                             + "An estimate from the text itself: the runtime does not record "
                             + "a token count per message, so this is measured rather than "
                             + "reported. The system prompt and tool definitions ride on top "
                             + "of every request and are not counted here, so treat it as a "
                             + "floor." },
      h("div", { style: { width: "54px", height: "4px", borderRadius: "2px",
                          background: "rgba(128,128,128,0.25)", overflow: "hidden" } },
        h("div", { style: { width: pct + "%", height: "100%", background: tone } })),
      h("span", { style: C.meta }, "≈" + compact(used) + " / " + compact(limit)));
  }

  function Sessions(props) {
    const groups = [];
    const seen = {};
    for (const session of props.sessions || []) {
      const channel = channelOf(session.source);
      if (!seen[channel.key]) {
        seen[channel.key] = { channel: channel, rows: [] };
        groups.push(seen[channel.key]);
      }
      seen[channel.key].rows.push(session);
    }
    // Known channels in declared order, anything unrecognised after them.
    groups.sort(function (a, b) {
      const ai = CHANNELS.findIndex(function (c) { return c.key === a.channel.key; });
      const bi = CHANNELS.findIndex(function (c) { return c.key === b.channel.key; });
      return (ai < 0 ? 99 : ai) - (bi < 0 ? 99 : bi);
    });

    return h("div", { style: C.side },
      h("button", { style: C.newBtn, onClick: props.onNew }, "+  New chat"),
      h("div", { style: C.sideList },
        groups.length === 0
          ? h("div", { style: Object.assign({}, C.meta, { padding: "8px 4px" }) },
              "No conversations yet.")
          : groups.map(function (group) {
              return h("div", { key: group.channel.key },
                h("div", { style: C.sideGroup },
                  h("span", null, group.channel.icon + "  " + group.channel.label),
                  h("span", { style: { opacity: 0.6 } }, String(group.rows.length))),
                group.rows.map(function (session) {
                  const active = session.id === props.activeId;
                  // Who it was with, where the channel records it.
                  const who = session.display_name
                            || (session.chat_type === "group" ? "group chat" : "")
                            || session.user_id || "";
                  return h("button", {
                    key: session.id,
                    onClick: function () { props.onOpen(session.id, session.source); },
                    style: Object.assign({}, C.sideItem, active ? C.sideItemActive : {}),
                    title: session.preview || session.title || session.id,
                  },
                    h("div", { style: C.sideTitle }, session.title || "Untitled"),
                    h("div", { style: C.meta },
                      (who ? who + "  ·  " : "")
                      + ago(session.last_activity_at || session.started_at)
                      + (session.message_count ? "  ·  " + session.message_count + " msgs" : "")));
                }));
            })));
  }

  // Tools whose argument is a command, so it reads as one.
  const SHELL_TOOLS = { terminal: 1, execute_code: 1, read_terminal: 1 };

  // "Running npm test" -> "npm test". The verb is already carried by the row.
  function stripVerb(label) {
    return String(label || "").replace(/^(Running code|Running)\s+/, "");
  }

  // Shell output, folded. Open while short: the common case is three lines you
  // want to see without asking, not a build log.
  function ShellOutput(props) {
    const text = String(props.text || "").replace(/\s+$/, "");
    const lines = text ? text.split("\n") : [];
    const [open, setOpen] = useState(lines.length <= 12);
    if (!text) return null;
    return h("div", { style: C.shellWrap },
      h("button", {
        style: C.shellToggle, onClick: function () { setOpen(!open); },
        title: open ? "Hide the output" : "Show the output",
      }, (open ? "▾ " : "▸ ") + lines.length + " line"
         + (lines.length === 1 ? "" : "s") + " of output"),
      open ? h("pre", { style: C.shellOut }, text) : null);
  }

  // Words that make a question a credential request. The agent is forbidden to
  // ask for these, so one arriving means something upstream talked it into it.
  const SECRET_RE = new RegExp(
    [
      /\b(pass[\s_-]?(word|wd|phrase)|api[\s_-]?key|secret[\s_-]?key|private[\s_-]?key)\b/.source,
      /\b((access|refresh|bearer)[\s_-]?token|credit[\s_-]?card|card[\s_-]?number)\b/.source,
      /\b(cvv|cvc|pin[\s_-]?code|otp|one[\s_-]?time[\s_-]?(code|password)|2fa|mfa[\s_-]?code)\b/.source,
      /\b(seed[\s_-]?phrase|recovery[\s_-]?(code|phrase)|credential(s)?|login[\s_-]?details)\b/.source,
    ].join("|"), "i");

  // A question the agent is blocked on. Choices are buttons because options
  // written into the question text are prose nobody can click.
  function Ask(props) {
    const ask = props.ask;
    if (SECRET_RE.test(ask.question || "")) {
      // Deliberately no input of any kind: the safe answer is not a careful one.
      return h("div", { style: C.askDanger },
        h("div", { style: C.askHead }, "Blocked — this question asks for a secret"),
        h("div", { style: C.askQ }, ask.question),
        h("div", { style: { fontSize: "13px", lineHeight: 1.5 } },
          "Ettok is not allowed to take passwords, keys or card numbers in a "
          + "conversation, so it should never have asked. Do not type it here or "
          + "anywhere else in this chat. Treat it as a sign the agent read "
          + "something that tried to steer it, and tell whoever runs the agent. "
          + "Credentials go in the dashboard's own settings, never to the agent."),
        h("button", { style: C.linkBtn, onClick: function () { props.onDismiss(); } },
          "Dismiss this question"));
    }
    const picked = props.picked || [];
    const choices = ask.choices || [];
    function toggle(choice) {
      props.onPick(picked.indexOf(choice) < 0
        ? picked.concat([choice])
        : picked.filter(function (c) { return c !== choice; }));
    }
    return h("div", { style: C.ask },
      h("div", { style: C.askHead }, "The agent is asking"),
      h("div", { style: C.askQ }, ask.question),
      choices.length
        ? h("div", { style: C.askRows },
            choices.map(function (choice, i) {
              const on = picked.indexOf(choice) >= 0;
              return h("button", {
                key: i,
                style: Object.assign({}, C.askBtn, on ? C.askBtnOn : {}),
                onClick: function () { ask.multi ? toggle(choice) : props.onAnswer(choice); },
              },
                ask.multi ? h("span", { style: C.askBox }, on ? "✓" : "") : null,
                h("span", null, choice));
            }))
        : null,
      ask.multi && choices.length
        ? h("button", {
            style: Object.assign({}, C.askSend, picked.length ? {} : { opacity: 0.45 }),
            disabled: !picked.length,
            onClick: function () { props.onAnswer(picked); },
          }, "Send " + (picked.length || "no") + " answer" + (picked.length === 1 ? "" : "s"))
        : null,
      h("div", { style: C.meta },
        choices.length
          ? "Or type your own answer below."
          : "Type your answer below."));
  }

  // What the agent is doing, while it does it. Dropping these is what makes a
  // working agent look hung.
  function Activity(props) {
    const tools = props.tools || [];
    if (!tools.length) return null;
    const running = tools.filter(function (t) { return !t.done; });
    return h("div", { style: C.act },
      tools.slice(-6).map(function (t, i) {
        return h("div", { key: t.id || i },
          h("div", { style: C.actRow },
            h("span", { className: t.done ? "" : "ettok-live",
                        style: { fontSize: "13px" } }, t.done ? "✓" : (t.emoji || "•")),
            SHELL_TOOLS[t.tool]
              ? h("code", { className: "ettok-code",
                            style: Object.assign({}, C.actShell,
                                                 t.done ? { opacity: 0.6 } : {}) },
                  "$ " + stripVerb(t.label || t.tool))
              : h("span", { style: Object.assign({}, C.actLabel, t.done ? { opacity: 0.55 } : {}) },
                  t.label || t.tool || "working")),
          t.output ? h(ShellOutput, { text: t.output }) : null);
      }),
      running.length > 1
        ? h("div", { style: C.meta }, running.length + " running")
        : null);
  }

  const COMMANDS = [
    { cmd: "/new", what: "Start a new conversation" },
    { cmd: "/stop", what: "Stop the agent mid-reply" },
    { cmd: "/retry", what: "Send the last message again" },
    { cmd: "/scan", what: "Ask the agent to work the open cases" },
    { cmd: "/doctor", what: "Ask whether everything it needs is working" },
    { cmd: "/status", what: "Pairing, open cases and the delivery queue" },
    { cmd: "/clear", what: "Clear this view (the conversation is kept)" },
    { cmd: "/help", what: "List these commands" },
  ];

  function Palette(props) {
    const items = props.items || [];
    if (!items.length) return null;
    return h("div", { style: C.palette, role: "listbox" },
      items.map(function (c, i) {
        return h("button", {
          key: c.cmd,
          style: Object.assign({}, C.palItem, i === props.index ? C.palItemOn : {}),
          onMouseDown: function (e) { e.preventDefault(); props.onPick(c); },
          role: "option",
          "aria-selected": i === props.index,
        },
          h("span", { style: C.palCmd }, c.cmd),
          h("span", { style: C.palWhat }, c.what));
      }));
  }

  function EttokChatPage() {
    useMarkdownStyles();
    const [messages, setMessages] = useState([]);
    const [draft, setDraft] = useState("");
    const [busy, setBusy] = useState(false);
    const abortRef = useRef(null);
    const [health, setHealth] = useState(null);
    const [model, setModel] = useState(null);
    const [sessions, setSessions] = useState([]);
    // The channel the open conversation belongs to. A Telegram thread can be
    // read here but not answered: a reply goes back over this page's stream and
    // never reaches Telegram, so the composer is closed rather than pretending.
    const [sessionChannel, setSessionChannel] = useState("api_server");
    const [sessionId, setSessionId] = useState(function () {
      try { return window.localStorage.getItem(LAST_SESSION_KEY) || null; }
      catch (e) { return null; }
    });

    // Anything a message arrives for -- a send, opening a session, a poll --
    // stamps this. An async callback that finishes after the epoch moved on is
    // writing into a conversation the reader has left, so it drops its result
    // instead. Without it a slow load could land on top of a live turn and take
    // the message just typed with it, which reads as the message vanishing.
    const epoch = useRef(0);
    const bump = useCallback(function () { epoch.current += 1; return epoch.current; }, []);

    useEffect(function () {
      try {
        if (sessionId) window.localStorage.setItem(LAST_SESSION_KEY, sessionId);
        else window.localStorage.removeItem(LAST_SESSION_KEY);
      } catch (e) { /* not worth a broken page */ }
    }, [sessionId]);

    const [effort, setEffort] = useState("");
    const [attachments, setAttachments] = useState([]);
    const [usedTokens, setUsedTokens] = useState(0);
    const [turnSpend, setTurnSpend] = useState(0);
    const [lastTurn, setLastTurn] = useState(null);   // {ms, tools}
    const [queued, setQueued] = useState([]);         // "by the way" messages
    const [ask, setAsk] = useState(null);             // a question the agent is blocked on
    const [picked, setPicked] = useState([]);         // multi-select, before it is sent
    const [palIndex, setPalIndex] = useState(0);
    const logRef = useRef(null);
    const fileRef = useRef(null);
    const sendRef = useRef(null);

    // The slash palette, open whenever the draft is a bare command being typed.
    const palette = useMemo(function () {
      const m = draft.match(/^\/(\S*)$/);
      if (!m) return [];
      const q = m[1].toLowerCase();
      return COMMANDS.filter(function (c) { return c.cmd.slice(1).indexOf(q) === 0; });
    }, [draft]);

    useEffect(function () { setPalIndex(0); }, [draft]);

    const loadSessions = useCallback(function () {
      // Every channel the agent talks on, newest first. Cron runs are excluded
      // rather than listed: they are scheduled work with no human on the other
      // end, and they have their own page.
      SDK.fetchJSON("/api/sessions?limit=80&order=recent&min_messages=1"
                    + "&exclude_sources=cron")
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
    }, [messages, queued]);

    // Occupancy, measured from the text, because the runtime stores no count.
    const recount = useCallback(function (rows) {
      let used = 0;
      for (const m of rows || []) used += estimateTokens(m.content);
      setUsedTokens(used);
    }, []);

    const waitFor = useRef(null);
    const stopPolling = useCallback(function () {
      if (waitFor.current) { clearInterval(waitFor.current); waitFor.current = null; }
    }, []);

    const waitForReply = useCallback(function (id, base, mine) {
      stopPolling();
      let tries = 0;
      waitFor.current = setInterval(function () {
        tries += 1;
        if (tries > 80) { stopPolling(); return; }
        SDK.fetchJSON("/api/sessions/" + encodeURIComponent(id) + "/messages?limit=200&order=oldest")
          .then(function (d) {
            if (epoch.current !== mine) { stopPolling(); return; }
            const rows = (d && d.messages) || [];
            const last = rows[rows.length - 1] || {};
            const text = typeof last.content === "string" ? last.content : "";
            if (last.role !== "assistant" || !text.trim()) return;
            stopPolling();
            writeInflight(null);
            setMessages(base.concat([{ role: "assistant", content: text }]));
          })
          .catch(function () { /* the next tick tries again */ });
      }, 3000);
    }, [stopPolling]);

    useEffect(function () { return stopPolling; }, [stopPolling]);

    const openSession = useCallback(function (id, source) {
      const mine = bump();
      stopPolling();
      setSessionId(id);
      if (source !== undefined) setSessionChannel(String(source || "api_server"));
      setAsk(null);
      setPicked([]);
      setMessages([{ role: "assistant", content: "_Loading…_" }]);
      SDK.fetchJSON("/api/sessions/" + encodeURIComponent(id) + "/messages?limit=200&order=oldest")
        .then(function (d) {
          if (epoch.current !== mine) return;
          const rows = (d && d.messages) || [];
          const out = [];
          for (const m of rows) {
            if (m.role !== "user" && m.role !== "assistant") continue;
            let text = m.content;
            if (Array.isArray(text)) {
              text = text.map(function (b) {
                if (typeof b === "string") return b;
                if (b && b.type === "text") return b.text || "";
                if (b && b.type === "image_url") return "`📎 image`";
                return "";
              }).join("\n").trim();
            }
            if (typeof text !== "string" || !text.trim()) continue;
            out.push({ role: m.role, content: text });
          }
          recount(out);
          SDK.fetchJSON(API + "/chat/active")
            .then(function (a) {
              if (epoch.current !== mine) return;
              const running = ((a && a.sessions) || []).some(function (row) {
                return row.session_id === id;
              });
              // The stored transcript is what COMPLETED. A turn in flight is
              // not in it yet, so fall back to the local record: it holds the
              // question that was asked and whatever had streamed back before
              // the page was left.
              const flight = readInflight();
              const usable = flight && (!flight.sessionId || flight.sessionId === id)
                             && flight.history && flight.history.length > out.length;
              const base = usable ? flight.history : out;

              if (!running) {
                // Not running and nothing stored for it: the turn ended while
                // away and its result is already in `out`.
                setMessages(out);
                if (usable) writeInflight(null);
                return;
              }

              setMessages(base.concat([{
                role: "assistant",
                content: (usable && flight.partial ? flight.partial + "\n\n" : "")
                       + "_Still working on this. It keeps going whether or not "
                       + "this page is open; the reply appears here when it lands._",
                pending: true,
              }]));
              waitForReply(id, base, mine);
            })
            .catch(function () { if (epoch.current === mine) setMessages(out); });
        })
        .catch(function (e) {
          if (epoch.current !== mine) return;
          setMessages([{ role: "assistant", content: "**Could not load that conversation.** " + e }]);
        });
    }, [waitForReply, stopPolling, bump, recount]);

    const restored = useRef(false);
    useEffect(function () {
      if (restored.current) return;
      restored.current = true;
      if (sessionId) {
        // The remembered id carries no channel, so look it up before deciding
        // whether the composer should be open.
        SDK.fetchJSON("/api/sessions/" + encodeURIComponent(sessionId))
          .then(function (d) { openSession(sessionId, d && (d.source || (d.session || {}).source)); })
          .catch(function () { openSession(sessionId, "api_server"); });
      }
    }, [sessionId, openSession]);

    const newChat = useCallback(function () {
      bump();
      stopPolling();
      setSessionId(null);
      setSessionChannel("api_server");
      // A question belongs to the turn that asked it. Carrying it into another
      // conversation would answer something nobody is looking at.
      setAsk(null);
      setPicked([]);
      setMessages([]);
      setUsedTokens(0);
      setTurnSpend(0);
      setLastTurn(null);
      setQueued([]);
      setAttachments([]);
    }, [bump, stopPolling]);

    const addFiles = useCallback(function (fileList) {
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

    // One turn, from a given history. Split out from the composer so a queued
    // "by the way" can be sent by the turn that finishes before it.
    const runTurn = useCallback(function (history, content, shown) {
      const mine = bump();
      stopPolling();
      const startedAt = Date.now();

      setMessages(history.concat([{ role: "assistant", content: "" }]));
      setBusy(true);
      setLastTurn(null);

      const wire = history.map(function (m) {
        return { role: m.role, content: m.role === "user" && m.wire ? m.wire : m.content };
      });
      if (content != null) wire[wire.length - 1] = { role: "user", content: content };

      const controller = new AbortController();
      abortRef.current = controller;

      const asked = history[history.length - 1];
      writeInflight({
        at: Date.now(), sessionId: sessionId || null,
        history: history.map(function (m) { return { role: m.role, content: m.content }; }),
        partial: "",
      });

      return SDK.authedFetch(API + "/chat", {
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
              writeInflight(null);
              if (epoch.current === mine) {
                setBusy(false);
                setLastTurn({ ms: Date.now() - startedAt, tools: tools.length });
                setMessages(function (prev) {
                  const next = prev.slice();
                  const last = next[next.length - 1];
                  if (last && last.role === "assistant") {
                    next[next.length - 1] = Object.assign({}, last, { streaming: false });
                  }
                  recount(next);
                  return next;
                });
              }
              setTimeout(function () { loadSessions(); }, 800);
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
                if (obj.status === "running") {
                  tools = tools.concat([{ id: obj.toolCallId, emoji: obj.emoji,
                                          tool: obj.tool,
                                          label: obj.label || obj.tool, done: false }]);
                } else {
                  tools = tools.map(function (t) {
                    return t.id === obj.toolCallId
                      ? Object.assign({}, t, { done: true, output: obj.output || t.output })
                      : t;
                  });
                }
              } else if (parsed.event === "hermes.clarify") {
                setAsk({ id: obj.clarifyId, question: obj.question,
                         choices: obj.choices || null, multi: !!obj.multiSelect });
              } else if (parsed.event === "hermes.clarify.done") {
                setAsk(function (a) { return a && a.id === obj.clarifyId ? null : a; });
              } else if (obj.session_id) {
                landed = obj.session_id;
                if (!sessionId) setSessionId(obj.session_id);
                continue;
              } else if (obj.error) {
                acc += "\n\n**" + textOf(obj.error) + "**"
                     + (obj.hint ? "\n\n" + textOf(obj.hint) : "");
              } else {
                const choice = (obj.choices || [])[0] || {};
                const delta = choice.delta || choice.message || {};
                if (delta.reasoning_content) acc += textOf(delta.reasoning_content);
                if (delta.content) acc += textOf(delta.content);
                // Spend, not occupancy: `usage` is summed over every model call
                // in the turn, so a reply that ran three tools reports far more
                // than the window ever held.
                if (obj.usage && obj.usage.total_tokens) setTurnSpend(obj.usage.total_tokens);
              }
              if (epoch.current === mine) {
                setMessages(history.concat([
                  { role: "assistant", content: acc, tools: tools, streaming: true }]));
                writeInflight({
                  at: Date.now(), sessionId: landed || sessionId || null,
                  history: history.map(function (m) {
                    return { role: m.role, content: m.content };
                  }),
                  partial: acc,
                });
              }
            }
            return pump();
          });
        }
        return pump();
      }).catch(function (e) {
        abortRef.current = null;
        writeInflight(null);
        if (epoch.current !== mine) return;
        setBusy(false);
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
    }, [sessionId, effort, loadSessions, bump, stopPolling, recount]);

    const runSlash = useCallback(function (cmd) {
      if (cmd === "/new") { newChat(); return true; }
      if (cmd === "/clear") { setMessages([]); return true; }
      if (cmd === "/stop") {
        const c = abortRef.current;
        if (c) { abortRef.current = null; c.abort(); }
        return true;
      }
      if (cmd === "/help") {
        setMessages(function (prev) {
          return prev.concat([{ role: "assistant", content:
            "**Commands**\n\n"
            + COMMANDS.map(function (c) { return "- `" + c.cmd + "` — " + c.what; }).join("\n")
            + "\n\nAnything else you type goes to the agent."
          }]);
        });
        return true;
      }
      return false;   // /scan, /doctor, /status and friends go to the agent
    }, [newChat]);

    // Send an answer to the pending question. The turn is still open; this goes
    // on a request of its own because the thread that asked is parked on it.
    const answer = useCallback(function (value) {
      const current = ask;
      if (!current) return;
      const text = Array.isArray(value) ? value.join(", ") : String(value || "").trim();
      if (!text) return;
      setAsk(null);
      setPicked([]);
      setMessages(function (list) {
        return list.concat([{ role: "user", content: text }]);
      });
      SDK.fetchJSON(API + "/chat/clarify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ clarify_id: current.id, response: text }),
      }).catch(function (err) {
        setMessages(function (list) {
          return list.concat([{
            role: "assistant",
            content: "**That answer did not reach the agent.** " + reason(err),
          }]);
        });
      });
    }, [ask]);

    const send = useCallback(function (override) {
      const text = (override != null ? override : draft).trim();
      const usable = attachments.filter(function (a) { return a.dataUrl; });
      if (!text && !usable.length) return;

      // A command that the page can answer never reaches the agent.
      if (!usable.length && text.charAt(0) === "/" && runSlash(text.split(/\s+/)[0])) {
        setDraft("");
        return;
      }

      // A pending question outranks the queue: the agent is blocked on it, so a
      // queued answer would wait for the turn that is waiting for the answer.
      if (ask) {
        // A blocked question is answered by nobody, including the composer --
        // the banner is the whole response.
        if (!SECRET_RE.test(ask.question || "")) answer(text);
        setDraft("");
        return;
      }

      // "By the way" -- typed while the agent is working. Rather than being
      // refused (which is what "my message disappeared" was: the composer
      // silently dropped it), it waits and goes as soon as the turn ends.
      if (busy) {
        setQueued(function (q) { return q.concat([text]); });
        setDraft("");
        return;
      }

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
      const history = messages.concat([{ role: "user", content: shown, wire: content }]);

      setDraft("");
      setAttachments([]);
      runTurn(history, content, shown);
    }, [draft, busy, messages, attachments, runTurn, runSlash, ask, answer]);

    sendRef.current = send;

    // Drain the queue the moment the agent is free.
    useEffect(function () {
      if (busy || ask || !queued.length) return;
      const next = queued[0];
      setQueued(function (q) { return q.slice(1); });
      const t = setTimeout(function () { sendRef.current(next); }, 120);
      return function () { clearTimeout(t); };
    }, [busy, queued, ask]);

    const stop = useCallback(function () {
      const controller = abortRef.current;
      if (!controller) return;
      abortRef.current = null;
      controller.abort();
    }, []);

    const onKeyDown = useCallback(function (e) {
      if (palette.length) {
        if (e.key === "ArrowDown") {
          e.preventDefault(); setPalIndex(function (i) { return (i + 1) % palette.length; }); return;
        }
        if (e.key === "ArrowUp") {
          e.preventDefault();
          setPalIndex(function (i) { return (i - 1 + palette.length) % palette.length; }); return;
        }
        if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
          e.preventDefault();
          const pick = palette[palIndex];
          if (pick) {
            if (runSlash(pick.cmd)) { setDraft(""); return; }
            setDraft(pick.cmd + " ");
          }
          return;
        }
        if (e.key === "Escape") { e.preventDefault(); setDraft(""); return; }
      }
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
    }, [palette, palIndex, send, runSlash]);

    const unavailable = health && health.available === false;
    const chan = channelOf(sessionChannel);
    const readOnly = Boolean(sessionId) && sessionChannel !== "api_server";
    const caps = (model && model.capabilities) || {};
    const vision = Boolean(caps.supports_vision);
    const reasoning = Boolean(caps.supports_reasoning);
    const limit = (model && (model.effective_context_length
                             || (model.capabilities || {}).context_window)) || 0;
    const canSend = Boolean(draft.trim() || attachments.length);

    return h("div", { style: C.shell },
      h(Sessions, { sessions: sessions, activeId: sessionId, onOpen: openSession, onNew: newChat }),
      h("div", { style: C.page },

        h("div", { style: C.head },
          h("h1", { style: C.h1 }, "Ettok AI"),
          model && model.model
            ? h("span", { style: C.meta }, (model.provider ? model.provider + " · " : "") + model.model)
            : null,
          h("div", { style: { flex: 1 } }),
          busy
            ? h("span", { style: Object.assign({}, C.meta, { opacity: 0.9 }), className: "ettok-live" },
                "● working")
            : lastTurn
              ? h("span", { style: C.meta },
                  "done in " + secs(lastTurn.ms)
                  + (lastTurn.tools ? " · " + lastTurn.tools + " step"
                                      + (lastTurn.tools === 1 ? "" : "s") : ""))
              : null,
          turnSpend
            ? h("span", { style: C.meta, title: "Tokens billed for the last turn, summed across "
                                               + "every model call it made." },
                compact(turnSpend) + " spent")
            : null,
          h(ContextMeter, { used: usedTokens, limit: limit }),
          reasoning ? h("select", {
            style: C.select, value: effort,
            title: "How hard the model should think before answering.",
            onChange: function (e) { setEffort(e.target.value); },
          },
            h("option", { value: "" }, "effort: default"),
            h("option", { value: "low" }, "effort: low"),
            h("option", { value: "medium" }, "effort: medium"),
            h("option", { value: "high" }, "effort: high")) : null),

        unavailable
          ? h("div", { style: C.warn },
              h("strong", null, "The agent is not reachable. "),
              textOf(health.reason || "The gateway is not running."),
              health.hint ? h("div", { style: { marginTop: "5px", opacity: 0.85 } },
                              textOf(health.hint)) : null)
          : null,

        h("div", { style: C.log, ref: logRef },
          messages.length === 0
            ? h("div", { style: C.muted },
                "Ask the agent something. Type ",
                h("code", { className: "ettok-code" }, "/"),
                " for commands.")
            : messages.map(function (m, i) {
                return h("div", { key: i },
                  h("div", { style: C.who }, m.role === "user" ? "You" : "Ettok"),
                  m.tools && m.tools.length ? h(Activity, { tools: m.tools }) : null,
                  h(Markdown, { role: m.role, text: m.content }),
                  m.streaming && !m.content
                    ? h("div", { style: C.meta, className: "ettok-live" }, "thinking…")
                    : null);
              })),

        ask
          ? h(Ask, { ask: ask, picked: picked, onPick: setPicked, onAnswer: answer,
                     onDismiss: function () { setAsk(null); setPicked([]); } })
          : null,

        queued.length
          ? h("div", { style: C.queued },
              h("span", null, "↑"),
              h("span", null,
                queued.length === 1
                  ? "1 message waiting — it goes as soon as the agent finishes."
                  : queued.length + " messages waiting — they go as soon as the agent finishes."),
              h("div", { style: { flex: 1 } }),
              h("button", {
                style: C.chipX, title: "Discard the waiting messages",
                onClick: function () { setQueued([]); },
              }, "×"))
          : null,

        attachments.length
          ? h("div", { style: C.chips },
              attachments.map(function (a, i) {
                return h("span", { key: i, style: C.chip },
                  a.error ? a.name + " — " + a.error : a.name,
                  h("button", {
                    style: C.chipX,
                    onClick: function () {
                      setAttachments(function (list) {
                        return list.filter(function (_x, j) { return j !== i; });
                      });
                    },
                  }, "×"));
              }))
          : null,

        palette.length
          ? h(Palette, {
              items: palette, index: palIndex,
              onPick: function (c) {
                if (runSlash(c.cmd)) { setDraft(""); return; }
                setDraft(c.cmd + " ");
              },
            })
          : null,

        readOnly
          ? h("div", { style: C.readonly },
              h("strong", null, chan.icon + "  " + chan.label + " conversation — read only."),
              " Replying here would answer on this page, not on "
              + chan.label + ", so the composer is closed. ",
              h("button", {
                style: C.linkBtn, onClick: newChat,
              }, "Start a dashboard chat instead"))
          : h("div", { style: C.row },
          h("input", {
            type: "file", multiple: true, ref: fileRef, style: { display: "none" },
            onChange: function (e) { addFiles(e.target.files); e.target.value = ""; },
          }),
          vision ? h("button", {
            style: C.attach, title: "Attach a file or image",
            onClick: function () { fileRef.current && fileRef.current.click(); },
          }, "📎") : null,
          h("textarea", {
            style: C.input,
            value: draft,
            placeholder: busy
              ? "The agent is working — type anyway, it goes next…"
              : "Ask the agent…   /  for commands",
            onChange: function (e) { setDraft(e.target.value); },
            onPaste: function (e) {
              const files = e.clipboardData && e.clipboardData.files;
              if (files && files.length) { e.preventDefault(); addFiles(files); }
            },
            onKeyDown: onKeyDown,
          }),
          busy
            ? h("button", { style: C.stopBtn, onClick: stop, title: "Stop the agent" }, "Stop")
            : h("button", {
                style: Object.assign({}, C.send, canSend ? {} : { opacity: 0.45, cursor: "default" }),
                onClick: function () { send(); },
                disabled: !canSend,
                title: "Send",
              }, "Send"))));
  }

  window.__HERMES_PLUGINS__.register("ettok-chat", EttokChatPage);
})();
