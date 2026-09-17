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
    return span(secs) + " ago";
  }

  // A scheduled time is in the future, and `ago` clamps at zero -- so the next
  // run of a job six hours away read "0s ago", which says the opposite of what
  // is true. Future, overdue and unscheduled are three different states and
  // each says which it is.
  function when(iso) {
    if (!iso) return "not scheduled";
    const secs = (new Date(iso).getTime() - Date.now()) / 1000;
    if (secs > 0) return "in " + span(secs);
    return "overdue by " + span(-secs);
  }

  function span(secs) {
    if (secs < 60) return Math.round(secs) + "s";
    if (secs < 3600) return Math.round(secs / 60) + "m";
    if (secs < 86400) return Math.round(secs / 3600) + "h";
    return Math.round(secs / 86400) + "d";
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
    formRow: { display: "flex", alignItems: "center", gap: "10px",
               margin: "8px 0" },
    label: { width: "120px", fontSize: "13px", opacity: 0.75, flexShrink: 0 },
    input: { flex: 1, padding: "6px 9px", fontSize: "13px",
             border: "1px solid rgba(128,128,128,0.35)", borderRadius: "4px",
             background: "transparent", color: "inherit" },
    btn: { padding: "6px 14px", fontSize: "13px", borderRadius: "4px",
           border: "1px solid rgba(128,128,128,0.35)", background: "transparent",
           color: "inherit", cursor: "pointer" },
    problem: { fontSize: "13px", color: "rgb(200,70,50)", margin: "8px 0" },
    linkBtn: { border: "none", background: "transparent", color: "rgb(90,130,190)",
               cursor: "pointer", font: "inherit", fontSize: "12px", padding: "0 8px 0 0" },
  };

  // ---- data -----------------------------------------------------------

  function useEndpoint(path, intervalMs) {
    const [data, setData] = useState(null);
    const [error, setError] = useState(null);

    const load = useCallback(function () {
      // SDK.fetchJSON, not fetch: the dashboard's API is session-authenticated,
      // and the SDK is what knows whether this install uses a loopback token or
      // a gated cookie. A bare fetch here just gets 401.
      SDK.fetchJSON(API + path)
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
    // Paired and connected are different claims and this panel used to make
    // only the first while looking like the second. Pairing is a key on this
    // disk; reaching the platform is a thing that either happened recently or
    // did not. An operator reading a green pill through an outage is being told
    // the opposite of what is true.
    const reached = props.reachedAt || null;
    const failure = props.reachError || null;
    return h("div", { style: S.card },
      h("div", { style: { display: "flex", alignItems: "center", gap: "10px", flexWrap: "wrap" } },
        h("span", { style: S.pill(s.paired) }, s.paired ? "PAIRED" : "NOT PAIRED"),
        (s.paired
          ? h("span", { style: S.pill(!failure && !!reached) },
              failure ? "CANNOT REACH THE PLATFORM"
                      : (reached ? "REACHED" : "NOT REACHED YET"))
          : null),
        h("span", { style: { fontSize: "13px" } }, s.platform_url)),
      s.agent_id
        ? h("div", { style: Object.assign({}, S.label, { marginTop: "6px" }) }, "as " + s.agent_id)
        : h("div", { style: Object.assign({}, S.label, { marginTop: "6px" }) },
            "Run `ettok connect` to request access."),
      failure
        ? h("div", { style: Object.assign({}, S.alert("warning"), { marginTop: "8px" }) },
            "The key on this machine is fine; the platform did not answer — "
            + failure + ". Anything shown below was read locally and may be out of date.")
        : (reached
            ? h("div", { style: Object.assign({}, S.label, { marginTop: "6px" }) },
                "last reached " + ago(reached))
            : null));
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


  // The platform's review vocabulary, in its words rather than ours. Every
  // status except false_positive used to render in the positive colour, so
  // "new" -- which means nobody has looked at it -- read exactly like a
  // confirmation. A status this agent does not recognise gets a neutral pill
  // and its raw name: inventing a colour for it would be guessing about
  // somebody's finding.
  const STATUS = {
    "new": ["awaiting review", "neutral"],
    "reviewed": ["confirmed", "positive"],
    "escalated": ["escalated", "positive"],
    "false_positive": ["false positive", "negative"],
    "dismissed": ["dismissed", "negative"],
  };

  function statusLabel(status) {
    const known = STATUS[status];
    return known ? known[0] : (status || "unknown");
  }

  function statusStyle(status) {
    const known = STATUS[status];
    const tone = known ? known[1] : "neutral";
    if (tone === "neutral") {
      return Object.assign({}, S.pill(false), {
        background: "rgba(128,128,128,0.15)", color: "inherit", opacity: 0.8,
      });
    }
    return S.pill(tone === "positive");
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
        h(Stat, { label: "nobody has looked", value: counts["new"] || 0 }),
        h(Stat, { label: "confirmed", value: (counts["reviewed"] || 0) + (counts["escalated"] || 0) }),
        h(Stat, { label: "dismissed", value: (counts["false_positive"] || 0) + (counts["dismissed"] || 0) }),
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
                h("td", { style: S.td }, h("span", { style: statusStyle(row.status) },
                  statusLabel(row.status))),
                h("td", { style: S.td },
                  h("span", { style: S.pill(row.had_context) },
                    row.had_context ? "yes" : "none")));
            })))
        : h("div", { style: S.muted },
            "Nothing has been submitted yet, or nothing has been confirmed."));
  }

  // ---- goals ----------------------------------------------------------
  //
  // A goal is a standing instruction the agent wakes up and works on: "watch
  // Sinjar-related pages every six hours", "check the outbox each morning".
  //
  // It is a cron job, not a timer of our own. cron survives restarts, reboots
  // and a closed laptop; an in-process loop does not, and for an agent whose
  // whole point is working unattended on someone else's machine that difference
  // is the feature. The dashboard already exposes the cron API, so this panel
  // speaks to it directly rather than adding a second way to schedule things.

  const GOAL_PREFIX = "ettok-goal";
  const CADENCES = [
    ["30m", "every 30 minutes"],
    ["1h", "hourly"],
    ["6h", "every 6 hours"],
    ["12h", "twice a day"],
    ["1d", "daily"],
    ["0 9 * * *", "every day at 09:00"],
    ["0 9 * * 1", "Mondays at 09:00"],
  ];

  function scheduleLabel(job) {
    // The stored shape is normalised, not the string that was submitted: "6h"
    // comes back as {kind:"interval", minutes:360, display:"every 360m"}. Read
    // `display` first and only fall back to guessing.
    const s = job.schedule || {};
    if (s.display) return String(s.display);
    if (s.expr) {
      const known = CADENCES.find(function (c) { return c[0] === s.expr; });
      return known ? known[1] : String(s.expr);
    }
    if (s.minutes) {
      return s.minutes % 60 === 0
        ? "every " + (s.minutes / 60) + "h"
        : "every " + s.minutes + "m";
    }
    return String(s.kind || "once");
  }

  function Goals(props) {
    const [jobs, setJobs] = useState(null);
    const [text, setText] = useState("");
    const [every, setEvery] = useState("6h");
    const [error, setError] = useState(null);
    const [saving, setSaving] = useState(false);

    const load = useCallback(function () {
      SDK.fetchJSON("/api/cron/jobs")
        .then(function (d) {
          const all = (d && (d.jobs || d)) || [];
          setJobs(all.filter(function (j) {
            const n = j.name || "";
            return n.indexOf(GOAL_PREFIX) === 0 || n === "ettok-scan";
          }));
          setError(null);
        })
        .catch(function (e) { setError(String(e.message || e)); });
    }, []);

    useEffect(load, [load]);

    function add() {
      const goal = text.trim();
      if (!goal || saving) return;
      setSaving(true);
      SDK.fetchJSON("/api/cron/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          // The goal IS the prompt. The skill is loaded alongside so the agent
          // works a case the way it is supposed to rather than improvising.
          prompt: goal + "\n\nLoad the ettok:working-a-case skill and follow it. "
                + "Report what you found, what stopped you, and anything an operator "
                + "should act on.",
          schedule: every,
          name: GOAL_PREFIX + ": " + goal.slice(0, 60),
          skills: ["ettok:working-a-case"],
          enabled_toolsets: ["ettok", "browser"],
          deliver: "local",
        }),
      }).then(function () {
        setText("");
        setSaving(false);
        load();
      }).catch(function (e) {
        setError(String(e.message || e));
        setSaving(false);
      });
    }

    function act(id, path, method) {
      SDK.fetchJSON("/api/cron/jobs/" + encodeURIComponent(id) + (path || ""),
                    { method: method || "POST" })
        .then(load)
        .catch(function (e) { setError(String(e.message || e)); });
    }

    return h("div", { style: S.section },
      h("h2", { style: S.h2 }, "Goals"),
      h("p", { style: S.sub },
        "A standing instruction the agent wakes up and works on. It runs through "
        + "cron, so it survives a restart or a reboot."),

      error ? h("div", { style: S.alert("warning") }, error) : null,

      h("div", { style: { display: "flex", gap: "8px", flexWrap: "wrap" } },
        h("input", {
          style: {
            flex: "1 1 340px", minWidth: "220px", padding: "9px 12px", borderRadius: "6px",
            border: "1px solid rgba(128,128,128,0.3)", background: "transparent",
            color: "inherit", font: "inherit", fontSize: "13px",
          },
          placeholder: "e.g. Watch Sinjar-related pages and report anything targeting Yazidis",
          value: text,
          onChange: function (e) { setText(e.target.value); },
          onKeyDown: function (e) { if (e.key === "Enter") add(); },
        }),
        h("select", {
          style: {
            padding: "9px 10px", borderRadius: "6px", border: "1px solid rgba(128,128,128,0.3)",
            background: "transparent", color: "inherit", font: "inherit", fontSize: "13px",
          },
          value: every,
          onChange: function (e) { setEvery(e.target.value); },
        }, CADENCES.map(function (c) {
          return h("option", { key: c[0], value: c[0] }, c[1]);
        })),
        h("button", {
          style: {
            padding: "9px 18px", borderRadius: "6px", border: "none", cursor: "pointer",
            fontWeight: 600, background: "rgb(90,130,190)", color: "#fff",
            opacity: (text.trim() && !saving) ? 1 : 0.45,
          },
          onClick: add,
          disabled: !text.trim() || saving,
        }, saving ? "…" : "Give goal")),

      jobs === null
        ? h("div", { style: S.muted }, "Loading…")
        : jobs.length === 0
          ? h("div", { style: S.muted },
              "No standing goals. Without one the agent only works when you ask it to "
              + "in chat, or when a case is scheduled with `ettok schedule`.")
          : h("table", { style: S.table },
              h("thead", null, h("tr", null,
                h("th", { style: S.th }, "goal"),
                h("th", { style: S.th }, "runs"),
                h("th", { style: S.th }, "last"),
                h("th", { style: S.th }, "next"),
                h("th", { style: S.th }, ""))),
              h("tbody", null, jobs.map(function (j) {
                const paused = j.paused || j.enabled === false;
                const name = String(j.name || "");
                const label = name.indexOf(GOAL_PREFIX + ": ") === 0
                  ? name.slice(GOAL_PREFIX.length + 2)
                  : name;
                return h("tr", { key: j.id },
                  h("td", { style: S.td },
                    label,
                    paused ? h("span", { style: Object.assign({}, S.pill(false),
                                                              { marginLeft: "6px" }) }, "paused") : null),
                  h("td", { style: S.td }, scheduleLabel(j)),
                  h("td", { style: S.td }, ago(j.last_run_at)),
                  h("td", { style: S.td }, paused ? "—" : when(j.next_run_at)),
                  h("td", { style: Object.assign({}, S.td, { whiteSpace: "nowrap" }) },
                    h("button", { style: S.linkBtn, onClick: function () { act(j.id, "/trigger"); } },
                      "run now"),
                    h("button", {
                      style: S.linkBtn,
                      onClick: function () { act(j.id, paused ? "/resume" : "/pause"); },
                    }, paused ? "resume" : "pause"),
                    h("button", {
                      style: Object.assign({}, S.linkBtn, { color: "rgb(200,70,50)" }),
                      onClick: function () { act(j.id, "", "DELETE"); },
                    }, "remove")));
              }))));
  }

  // What this agent decided, held on this machine.
  //
  // "Findings on the platform" answers what the platform made of a submission.
  // This is the other half and the half nobody could see: the agent's own
  // verdict and its reasoning. Without it, "the agent is judging badly" could
  // only be checked by logging into somebody else's database, and a run made
  // while unpaired left no visible trace of its reasoning at all.
  //
  // Read, matched and judged are three different numbers and the difference
  // between them is the whole point: a rule firing is why a comment was read,
  // not a verdict on it.
  function Judgements() {
    const [only, setOnly] = useState("matched");
    const [caseId, setCaseId] = useState("");
    const query = "/judgements?limit=100&only=" + only
      + (caseId ? "&case_id=" + encodeURIComponent(caseId) : "");
    const [data, error] = useEndpoint(query, 0);
    const [openId, setOpenId] = useState(null);

    if (error) return h("div", { style: S.muted }, "Could not read them — " + error);
    if (!data) return h("div", { style: S.muted }, "Loading…");

    const rows = data.judgements || [];
    const totals = data.totals || {};

    function filterBtn(value, label) {
      return h("button", {
        key: value,
        style: Object.assign({}, S.linkBtn,
          only === value ? { fontWeight: 700, textDecoration: "underline" } : {}),
        onClick: function () { setOnly(value); },
      }, label);
    }

    return h("div", { style: S.section },
      h("div", { style: S.grid },
        h(Stat, { label: "comments read", value: totals.read || 0 }),
        h(Stat, { label: "a rule fired", value: totals.matched || 0 }),
        h(Stat, { label: "judged hate speech", value: totals.judged_hate || 0 })),

      h("div", { style: { margin: "8px 0" } },
        filterBtn("matched", "a rule fired"),
        filterBtn("hate", "judged hate speech"),
        filterBtn("clear", "judged not hate speech"),
        filterBtn("", "everything read"),
        (data.cases || []).length
          ? h("select", {
              style: { marginLeft: "12px" },
              value: caseId,
              onChange: function (e) { setCaseId(e.target.value); },
            },
            [h("option", { key: "", value: "" }, "every case")].concat(
              (data.cases || []).map(function (c) {
                return h("option", { key: c.id, value: c.id },
                  (c.title || "case " + c.id) + " (" + c.count + ")");
              })))
          : null),

      rows.length
        ? h("div", null, rows.map(function (row) {
            const open = openId === row.id;
            return h("div", {
              key: row.id,
              style: {
                borderTop: "1px solid rgba(0,0,0,.08)", padding: "10px 0",
              },
            },
              h("div", {
                style: { display: "flex", gap: "10px", alignItems: "baseline", cursor: "pointer" },
                onClick: function () { setOpenId(open ? null : row.id); },
              },
                h("span", { style: S.pill(row.is_hate_speech) },
                  row.is_hate_speech ? "hate speech" : "not hate speech"),
                h("span", { style: { flex: 1, minWidth: 0 }, dir: "auto" },
                  (row.excerpt || "").slice(0, 160)),
                h("span", { style: S.muted }, ago(row.at))),

              open
                ? h("div", { style: { padding: "8px 0 4px", fontSize: "13px" } },
                    row.parent_excerpt
                      ? h("div", { style: S.muted, dir: "auto" },
                          "under a post saying: " + row.parent_excerpt)
                      : h("div", { style: S.alert("warning") },
                          "No parent post was captured. Context-dependent hate cannot be "
                          + "judged without it, and this verdict is weaker for it."),
                    row.why_flagged
                      ? h("div", null, h("b", null, "what fired: "), row.why_flagged)
                      : h("div", { style: S.muted },
                          "Nothing fired. Kept as the denominator: findings without the "
                          + "number of comments they came out of cannot be turned into a rate."),
                    row.reason
                      ? h("div", null, h("b", null, "why: "), row.reason)
                      : null,
                    row.category || row.severity
                      ? h("div", { style: S.muted },
                          (row.category || "uncategorised")
                          + (row.severity ? " · severity " + row.severity : ""))
                      : null,
                    row.exemption_applied
                      ? h("div", { style: S.muted },
                          "exemption applied: " + row.exemption_applied)
                      : null,
                    row.tier === "matched_only"
                      ? h("div", { style: S.muted },
                          "Decided by the rules alone — there was no budget for a model "
                          + "call on that run.")
                      : null,
                    h("div", { style: S.muted },
                      "knowledge: "
                      + Object.keys(row.versions || {}).map(function (k) {
                          return k + " " + row.versions[k];
                        }).join(", ")),
                    row.url
                      ? h("a", { href: row.url, target: "_blank", rel: "noopener noreferrer" },
                          "open the comment")
                      : null)
                : null);
          }))
        : h("div", { style: S.muted },
            "Nothing recorded yet. Every comment this agent reads is kept here, with "
            + "what fired and why it decided as it did."));
  }

  // The corpus as a thread: case, then post, then the comments under it.
  //
  // The same shape the platform shows, built from this machine's own records,
  // so it works with no network and answers what THIS agent saw -- which is the
  // question somebody brings to the agent's dashboard rather than to the
  // platform's.
  //
  // Three numbers per post, kept apart: collected is the denominator, matched
  // is why a comment was read, and judged is what the agent concluded. Only a
  // person on the platform can turn the third into a finding.
  function Threads() {
    const [data, error] = useEndpoint("/threads?limit=40", 0);
    const [openPost, setOpenPost] = useState(null);

    if (error) return h("div", { style: S.muted }, "Could not read them — " + error);
    if (!data) return h("div", { style: S.muted }, "Loading…");

    const cases = data.cases || [];
    if (!cases.length) {
      return h("div", { style: S.muted },
        "Nothing collected yet. Once a run reads a page, its comments appear here "
        + "grouped under the post they replied to.");
    }

    return h("div", null, cases.map(function (c) {
      return h("div", { key: c.id || "none", style: S.section },
        h("h3", { style: S.h2 }, c.title),
        h("div", { style: S.muted },
          c.collected + " collected · " + c.matched + " matched a rule · "
          + c.judged_hate + " judged hate speech"),

        (c.posts || []).map(function (post) {
          const key = (c.id || "none") + "|" + (post.url || "");
          const open = openPost === key;
          const tone = post.share >= 50 ? "rgb(159,18,57)"
            : post.share >= 20 ? "rgb(180,83,9)" : "rgb(51,65,85)";
          return h("div", {
            key: key,
            style: { borderTop: "1px solid rgba(0,0,0,.08)", padding: "10px 0" },
          },
            h("div", {
              style: { display: "flex", gap: "10px", alignItems: "baseline", cursor: "pointer" },
              onClick: function () { setOpenPost(open ? null : key); },
            },
              h("span", { style: { fontSize: "18px", fontWeight: 700, color: tone } },
                post.share + "%"),
              h("span", { style: S.muted },
                post.matched + " of " + post.collected + " comments matched a rule"),
              post.judged_hate
                ? h("span", { style: S.pill(false) }, post.judged_hate + " judged hate speech")
                : null,
              h("span", { style: Object.assign({}, S.muted, { marginLeft: "auto" }) },
                ago(post.last_seen))),

            h("div", { style: Object.assign({}, S.muted, { marginTop: "4px" }), dir: "auto" },
              post.post
                ? "under a post saying: " + post.post
                : "No parent post was captured — context-dependent hate cannot be judged without it."),

            open
              ? h("div", { style: { marginTop: "8px" } },
                  (post.comments || []).map(function (comment, i) {
                    return h("div", {
                      key: i,
                      style: {
                        padding: "8px 0 8px 12px",
                        borderLeft: "2px solid rgba(0,0,0,.08)",
                        opacity: comment.why_flagged ? 1 : 0.6,
                      },
                    },
                      h("div", { dir: "auto", style: { fontSize: "14px" } }, comment.text),
                      h("div", { style: Object.assign({}, S.muted, { marginTop: "4px" }) },
                        h("span", { style: S.pill(comment.is_hate_speech) },
                          comment.is_hate_speech ? "hate speech" : "not hate speech"),
                        " ",
                        comment.why_flagged || "nothing fired — kept as the denominator"),
                      comment.reason
                        ? h("div", { style: S.muted }, comment.reason)
                        : null);
                  }),
                  post.url
                    ? h("a", { href: post.url, target: "_blank", rel: "noopener noreferrer" },
                        "open the page")
                    : null)
              : null);
        }));
    }));
  }

  // ---- page -----------------------------------------------------------

  function EttokPage() {
    const [status, statusErr] = useEndpoint("/status", POLL_MS);
    const [knowledge] = useEndpoint("/knowledge", POLL_MS * 4);
    const [reports] = useEndpoint("/reports", POLL_MS * 2);
    const [view, setView] = useState("work");

    if (statusErr) {
      return h("div", { style: S.page },
        h("h1", { style: S.h1 }, "Ettok AI"),
        h("div", { style: S.alert("critical") },
          "Could not read the agent's state — " + statusErr));
    }
    if (!status) return h("div", { style: S.page }, h("div", { style: S.muted }, "Loading…"));

    const q = status.queue || {};

    // Sub-navigation inside this one tab, rather than four entries in the host
    // agent's sidebar. That sidebar belongs to the agent and is shared with its
    // own pages and every other plugin: filling it with Ettok would make a
    // general-purpose agent look single-purpose, and it would mean editing core
    // navigation to add a product screen.
    //
    // Alerts and the connection state stay above the navigation on every view.
    // They are the two things that mean "nothing below this is running", and a
    // reader who has navigated away from an overview must not lose them.
    const VIEWS = [
      ["work", "What it is doing"],
      ["cases", "Cases"],
      ["collected", "Collected"],
      ["decided", "What it decided"],
      ["platform", "On the platform"],
      ["setup", "Detection and accounts"],
    ];

    return h("div", { style: S.page },
      h("div", null,
        h("h1", { style: S.h1 }, "Ettok AI"),
        h("p", { style: S.sub },
          "Hate speech monitoring for minority communities in Iraq. "
          + "This agent collects and reports; the platform decides.")),

      h(Alerts, { alerts: status.alerts }),
      h(Connection, {
        status: status,
        // Whether the platform actually answered, taken from the call that
        // crosses the network rather than from what is stored on disk.
        reachedAt: (knowledge && knowledge.available) ? knowledge.fetched_at : null,
        reachError: (knowledge && knowledge.available === false) ? knowledge.reason : null,
      }),

      h("div", {
        style: {
          display: "flex", flexWrap: "wrap", gap: "4px",
          margin: "16px 0 4px", borderBottom: "1px solid rgba(0,0,0,.1)",
        },
      }, VIEWS.map(function (entry) {
        const active = view === entry[0];
        return h("button", {
          key: entry[0],
          onClick: function () { setView(entry[0]); },
          style: {
            background: "none", border: "none", cursor: "pointer",
            padding: "8px 12px", fontSize: "14px",
            fontWeight: active ? 700 : 400,
            borderBottom: active ? "2px solid currentColor" : "2px solid transparent",
            opacity: active ? 1 : 0.7,
          },
        }, entry[1]);
      })),

      view === "work"
        ? h("div", null,
            h(Goals, null),
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
              h("h2", { style: S.h2 }, "Recent runs"),
              h(Runs, { runs: status.runs })))
        : null,

      view === "cases"
        ? h("div", { style: S.section },
            h("h2", { style: S.h2 }, "Open cases"),
            h("p", { style: S.sub },
              "Opened and closed on the platform, never here. An agent that could "
              + "close its own case could also decide it had looked long enough."),
            h(Cases, { knowledge: knowledge }))
        : null,

      view === "collected"
        ? h("div", { style: S.section },
            h("h2", { style: S.h2 }, "Everything collected"),
            h("p", { style: S.sub },
              "Case, then post, then the comments under it — the same shape the "
              + "platform shows, built from this machine's own records, so it reads "
              + "with no network at all."),
            h(Threads, null))
        : null,

      view === "decided"
        ? h("div", { style: S.section },
            h("h2", { style: S.h2 }, "What this agent decided"),
            h("p", { style: S.sub },
              "Its own judgements, kept on this machine. The platform re-judges "
              + "everything and its verdict is the one that stands — these are here "
              + "so a disagreement between the two is visible rather than silent."),
            h(Judgements, null))
        : null,

      view === "platform"
        ? h("div", { style: S.section },
            h("h2", { style: S.h2 }, "Findings on the platform"),
            h(Reports, { reports: reports }))
        : null,

      view === "setup"
        ? h("div", null,
            h("div", { style: S.section },
              h("h2", { style: S.h2 }, "What it can detect"),
              h(Knowledge, { knowledge: knowledge })),
            h("div", { style: S.section },
              h("h2", { style: S.h2 }, "Monitoring accounts"),
              h(Accounts, { accounts: status.accounts })),
            h("div", { style: S.section },
              h("h2", { style: S.h2 }, "Account vault"),
              h(Vault, null)))
        : null);
  }

  // Accounts the agent can sign in with.
  //
  // The vault has always existed as a CLI command and nothing in the dashboard
  // said so, so the only way to discover it was to read `ettok vault --help`.
  // An operator who does not find it types the password into the chat instead,
  // which is the one place it must never go: a transcript keeps it for good, and
  // a credential that has been in one has to be changed rather than used.
  //
  // The password leaves this form and is never returned. The list shows the
  // site, the name and the login identifier -- the identifier is deliberately
  // not a secret, because the agent types it itself; only the password is
  // encrypted and filled server-side, on the exact origin it was saved for.
  function Vault() {
    const [data, error, reload] = useEndpoint("/vault", 0);
    const [open, setOpen] = useState(false);
    const [form, setForm] = useState({
      label: "", origin: "", identifier: "", identifier_type: "email",
      password: "", otp_secret: "",
    });
    const [saving, setSaving] = useState(false);
    const [problem, setProblem] = useState("");

    const set = function (key) {
      return function (e) {
        const value = e.target.value;
        setForm(function (prev) {
          const next = Object.assign({}, prev); next[key] = value; return next;
        });
      };
    };

    const save = useCallback(function () {
      setSaving(true); setProblem("");
      SDK.fetchJSON(API + "/vault", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(form),
      }).then(function (r) {
        setSaving(false);
        if (!r || !r.ok) { setProblem((r && r.error) || "Could not save it."); return; }
        // Cleared immediately: no reason for the password to sit in the page
        // after it has been stored.
        setForm({ label: "", origin: "", identifier: "", identifier_type: "email",
                  password: "", otp_secret: "" });
        setOpen(false);
        reload();
      }).catch(function (e) {
        setSaving(false); setProblem(e.message || String(e));
      });
    }, [form, reload]);

    const remove = useCallback(function (id, label) {
      if (!window.confirm("Remove \"" + label + "\" from the vault?")) return;
      SDK.fetchJSON(API + "/vault/" + encodeURIComponent(id), { method: "DELETE" })
        .then(reload).catch(function () { reload(); });
    }, [reload]);

    if (error) return h("div", { style: S.muted }, "Could not read the vault: " + error);
    const items = (data && data.items) || [];

    return h("div", null,
      h("div", { style: S.muted },
        "Accounts the agent signs in with. The password is stored encrypted on "
        + "this machine and filled into the page server-side, on the site it was "
        + "saved for \u2014 it is never shown again, never sent to the model, and "
        + "never written to a log. Add accounts here rather than typing them into "
        + "the chat, where a transcript would keep them."),

      items.length
        ? h("table", { style: S.table },
            h("thead", null, h("tr", null,
              h("th", { style: S.th }, "Name"),
              h("th", { style: S.th }, "Site"),
              h("th", { style: S.th }, "Signs in as"),
              h("th", { style: S.th }, ""))),
            h("tbody", null, items.map(function (it) {
              return h("tr", { key: it.id },
                h("td", { style: S.td }, it.label),
                h("td", { style: S.td }, it.origin || "\u2014"),
                h("td", { style: S.td },
                  (it.identifier || "\u2014")
                  + (it.has_otp ? "  \u00b7 2FA stored" : "")),
                h("td", { style: S.td },
                  h("button", {
                    style: S.linkBtn,
                    onClick: function () { remove(it.id, it.label); },
                  }, "Remove")));
            })))
        : h("div", { style: S.muted }, "No accounts stored yet."),

      open
        ? h("div", { style: S.card },
            h("div", { style: S.formRow },
              h("label", { style: S.label }, "Name"),
              h("input", { style: S.input, value: form.label, onChange: set("label"),
                           placeholder: "Facebook monitoring account" })),
            h("div", { style: S.formRow },
              h("label", { style: S.label }, "Site"),
              h("input", { style: S.input, value: form.origin, onChange: set("origin"),
                           placeholder: "https://www.facebook.com" })),
            h("div", { style: S.formRow },
              h("label", { style: S.label }, "Signs in as"),
              h("input", { style: S.input, value: form.identifier,
                           onChange: set("identifier"), placeholder: "name@example.org" })),
            h("div", { style: S.formRow },
              h("label", { style: S.label }, "Password"),
              h("input", { style: S.input, type: "password", value: form.password,
                           onChange: set("password"), autoComplete: "new-password" })),
            h("div", { style: S.formRow },
              h("label", { style: S.label }, "2FA secret"),
              h("input", { style: S.input, value: form.otp_secret,
                           onChange: set("otp_secret"),
                           placeholder: "optional \u2014 the setup key, not a 6-digit code" })),
            problem ? h("div", { style: S.problem }, problem) : null,
            h("div", { style: S.formRow },
              h("button", { style: S.btn, onClick: save, disabled: saving },
                saving ? "Saving\u2026" : "Save"),
              h("button", { style: S.linkBtn, onClick: function () { setOpen(false); setProblem(""); } },
                "Cancel")))
        : h("button", { style: S.btn, onClick: function () { setOpen(true); } },
            "Add an account"));
  }

  window.__HERMES_PLUGINS__.register("ettok", EttokPage);
})();
