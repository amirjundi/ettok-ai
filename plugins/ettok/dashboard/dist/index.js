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

  // ---- page -----------------------------------------------------------

  function EttokPage() {
    const [status, statusErr] = useEndpoint("/status", POLL_MS);
    const [knowledge] = useEndpoint("/knowledge", POLL_MS * 4);

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
          + "This agent collects and reports; the platform decides.")),

      h(Alerts, { alerts: status.alerts }),
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
        h("h2", { style: S.h2 }, "What it can detect"),
        h(Knowledge, { knowledge: knowledge })),

      h("div", { style: S.section },
        h("h2", { style: S.h2 }, "Monitoring accounts"),
        h(Accounts, { accounts: status.accounts })),

      h("div", { style: S.section },
        h("h2", { style: S.h2 }, "Recent runs"),
        h(Runs, { runs: status.runs })));
  }

  window.__HERMES_PLUGINS__.register("ettok", EttokPage);
})();
