// Shared UI for onboarding (onboarding.html) and the page's Settings (index.html).
// Everything talks to this person's own profile through /api; nothing here is shared.
var TL = (function () {
  var TL = {};
  TL.$ = function (id) { return document.getElementById(id); };
  TL.h = function (tag, attrs, kids) {
    var e = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      var v = attrs[k]; if (v == null || v === false) return;
      if (k === "class") e.className = v; else if (k === "text") e.textContent = v;
      else if (k.slice(0, 2) === "on") e.addEventListener(k.slice(2), v);
      else if (k === "value" || k === "checked" || k === "disabled") e[k] = v; else e.setAttribute(k, v === true ? "" : v);
    });
    (kids || []).forEach(function (c) { if (c != null && c !== false) e.append(c); });
    return e;
  };
  var h = TL.h;
  TL.clone = function (o) { return JSON.parse(JSON.stringify(o)); };
  TL.local = function (k, v) { try { if (v === undefined) return JSON.parse(localStorage.getItem(k) || "null"); if (v === null) localStorage.removeItem(k); else localStorage.setItem(k, JSON.stringify(v)); } catch (e) { return null; } };
  TL.copy = function (text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    var t = h("textarea", { value: text, style: "position:fixed;opacity:0" }); document.body.appendChild(t); t.select();
    try { document.execCommand("copy"); } finally { t.remove(); } return Promise.resolve();
  };
  TL.hourLabel = function (hr) { return (hr % 12 || 12) + (hr < 12 ? " am" : " pm"); };
  TL.handles = function (v) { return v.split(/[\s,]+/).map(function (x) { return x.replace(/^@/, "").replace(/^https?:\/\/(x|twitter)\.com\//, "").replace(/\/.*$/, "").trim(); }).filter(Boolean); };
  TL.localTz = function () { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "America/Los_Angeles"; } catch (e) { return "America/Los_Angeles"; } };
  TL.tzShort = function (tz) { try { return new Intl.DateTimeFormat("en-US", { timeZone: tz, timeZoneName: "short" }).formatToParts(new Date()).filter(function (p) { return p.type === "timeZoneName"; })[0].value; } catch (e) { return tz; } };

  // JSON API. Throws an Error with the server's human message.
  TL.api = function (method, path, body) {
    return fetch(path, { method: method, credentials: "same-origin", headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined })
      .then(function (r) {
        return r.json().catch(function () { return null; }).then(function (j) {
          if (r.status === 401 && path !== "/api/login") { location.href = "/login"; throw new Error("Signed out."); }
          if (!r.ok) {
            var d = j && (j.error || j.detail);
            if (Array.isArray(d)) d = d.map(function (x) { return x.msg || String(x); }).join("; ");
            throw new Error(d || ("Something went wrong (" + r.status + ")."));
          }
          return j;
        });
      });
  };

  TL.slackDefaults = function (sl) {
    sl = Object.assign({ enabled: false, frequency: "daily", hour: 8, weekdays_only: true, top_tweets: 5 }, sl || {});
    if (sl.hour == null && sl.hour_pt != null) sl.hour = sl.hour_pt;
    delete sl.hour_pt;
    sl.hour = parseInt(sl.hour, 10) || 0; sl.top_tweets = Math.min(10, Math.max(0, parseInt(sl.top_tweets, 10) || 0));
    return sl;
  };

  // ---- Editors ----
  TL.listEditor = function (box, arr, placeholder) {
    box.innerHTML = "";
    function read() { arr.length = 0; box.querySelectorAll("input").forEach(function (i) { if (i.value.trim()) arr.push(i.value.trim()); }); }
    function row(v) {
      var inp = h("input", { class: "inp", value: v, placeholder: placeholder });
      var r = h("div", { class: "li" }, [inp, h("button", { class: "rm-x", type: "button", "aria-label": "Remove", text: "×", onclick: function () { r.remove(); read(); } })]);
      inp.addEventListener("input", read);
      inp.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); var n = row(""); r.after(n); n.firstChild.focus(); } });
      return r;
    }
    arr.forEach(function (v) { box.append(row(v)); });
    return function () { var n = row(""); box.append(n); n.firstChild.focus(); };
  };
  TL.tagEditor = function (box, arr) {
    box.innerHTML = "";
    var wrap = h("div", { class: "tagbox" }), inp = h("input", { placeholder: "@handle", spellcheck: "false", autocomplete: "off", "aria-label": "Add a handle" });
    function draw() {
      wrap.querySelectorAll(".tag").forEach(function (t) { t.remove(); });
      arr.forEach(function (hd, i) {
        wrap.insertBefore(h("span", { class: "tag" }, ["@" + hd, h("button", { type: "button", "aria-label": "Remove @" + hd, text: "×",
          onclick: function () { arr.splice(i, 1); draw(); } })]), inp);
      });
    }
    function add() {
      TL.handles(inp.value).forEach(function (x) { if (!arr.some(function (a) { return a.toLowerCase() === x.toLowerCase(); })) arr.push(x); });
      inp.value = ""; draw();
    }
    inp.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === "," || e.key === " ") { e.preventDefault(); add(); }
      else if (e.key === "Backspace" && !inp.value && arr.length) { arr.pop(); draw(); }
    });
    inp.addEventListener("blur", add);
    inp.addEventListener("paste", function () { setTimeout(add); });
    wrap.addEventListener("click", function (e) { if (e.target === wrap) inp.focus(); });
    wrap.append(inp); box.append(wrap); draw();
  };

  function mixColor(i) { return "color-mix(in srgb, var(--accent) " + Math.max(28, 100 - i * 10) + "%, var(--surface))"; }
  // Topic on/off plus a share slider each, with a live bar of the resulting mix.
  TL.mixEditor = function (box, topics) {
    box.innerHTML = "";
    var bar = h("div", { class: "mixbar", "aria-hidden": "true" }), legend = h("p", { class: "mix-legend" }), rows = h("div");
    var ts = topics.filter(function (t) { return t.key !== "other"; });
    function draw() {
      var on = ts.filter(function (t) { return t.include && t.share > 0; });
      var total = on.reduce(function (a, t) { return a + t.share; }, 0) || 1;
      bar.innerHTML = "";
      on.forEach(function (t, i) { bar.append(h("i", { style: "flex-grow:" + t.share + ";background:" + mixColor(i), title: t.label })); });
      legend.textContent = on.length ? on.map(function (t) { return t.label + " " + Math.round(t.share / total * 100) + "%"; }).join(" · ") : "Turn on at least one topic.";
      rows.querySelectorAll(".mix-row").forEach(function (r) {
        var t = ts[+r.dataset.i];
        r.classList.toggle("off", !t.include);
        r.querySelector(".pct").textContent = t.include ? Math.round(t.share / total * 100) + "%" : "Off";
        r.querySelector("input[type=range]").disabled = !t.include;
      });
    }
    ts.forEach(function (t, i) {
      var cb = h("input", { type: "checkbox", checked: t.include !== false, "aria-label": "Include " + t.label });
      var rg = h("input", { type: "range", min: "0", max: "40", step: "1", value: String(Math.round((t.share || 0) * 100)), "aria-label": t.label + " share" });
      cb.addEventListener("change", function () { t.include = cb.checked; if (t.include && !t.share) { t.share = 0.1; rg.value = "10"; } draw(); });
      rg.addEventListener("input", function () { t.share = +rg.value / 100; draw(); });
      rows.append(h("div", { class: "mix-row", "data-i": String(i) }, [cb, h("div", { style: "min-width:0" }, [h("b", { text: t.label }), h("small", { text: t.description, title: t.description })]), rg, h("span", { class: "pct" })]));
    });
    box.append(bar, legend, rows); draw();
  };
  TL.normalizeTopics = function (topics) {
    var t = TL.clone(topics), total = t.reduce(function (a, x) { return a + (x.include && x.key !== "other" ? x.share : 0); }, 0);
    t.forEach(function (x) { x.share = x.include && x.key !== "other" && total > 0 ? Math.round(x.share / total * 1000) / 1000 : 0; });
    if (!t.some(function (x) { return x.key === "other"; })) t.push({ key: "other", label: "Other", description: "Anything else", include: false, share: 0 });
    return t;
  };

  TL.FILTERS = { sharper: { min_signal: 2.2, min_relevance: 0.7, max_bait: 0.4 },
                 balanced: { min_signal: 1.8, min_relevance: 0.6, max_bait: 0.5 },
                 wider: { min_signal: 1.4, min_relevance: 0.5, max_bait: 0.6 } };
  TL.filterName = function (th) {
    for (var k in TL.FILTERS) { var p = TL.FILTERS[k]; if (p.min_signal === +th.min_signal && p.min_relevance === +th.min_relevance && p.max_bait === +th.max_bait) return k; }
    return null;
  };
  // Three presets plus the exact numbers. Mutates s.thresholds.
  TL.filterEditor = function (box, s) {
    box.innerHTML = "";
    var cards = [["sharper", "Sharper", "Fewer tweets, each clearly notable. Best when the page feels noisy."],
                 ["balanced", "Balanced", "The default. Keeps what's notable and skips most filler."],
                 ["wider", "Wider net", "More tweets, some smaller stories too. Best when things feel missing."]];
    var nums = {};
    var grid = h("div", { class: "presets" }, cards.map(function (c) {
      return h("button", { type: "button", class: "preset", "data-p": c[0], onclick: function () { s.thresholds = TL.clone(TL.FILTERS[c[0]]); draw(); } },
        [h("b", { text: c[1] }), h("span", { text: c[2] })]);
    }));
    function num(key, min, max, step, hint) {
      nums[key] = h("input", { class: "inp", type: "number", min: String(min), max: String(max), step: String(step), "aria-label": hint });
      nums[key].addEventListener("input", function () { var v = parseFloat(this.value); if (!isNaN(v)) { s.thresholds[key] = v; mark(); } });
      return h("div", null, [nums[key], h("p", { class: "hint", text: hint })]);
    }
    function mark() { var cur = TL.filterName(s.thresholds); grid.querySelectorAll(".preset").forEach(function (b) { b.setAttribute("aria-pressed", String(b.dataset.p === cur)); }); }
    function draw() { Object.keys(nums).forEach(function (k) { nums[k].value = s.thresholds[k]; }); mark(); }
    box.append(grid, h("details", { class: "more" }, [h("summary", { text: "Exact numbers" }), h("div", { class: "row3" }, [
      num("min_signal", 0, 4, 0.1, "Min signal, 0–4. About 2 means notable."),
      num("min_relevance", 0, 1, 0.05, "Min relevance, 0–1: how on-topic it must be"),
      num("max_bait", 0, 1, 0.05, "Max bait, 0–1: how much engagement bait is tolerated")])]));
    draw();
  };
  TL.checkFilter = function (th) {
    [["min_signal", 0, 4, "Min signal"], ["min_relevance", 0, 1, "Min relevance"], ["max_bait", 0, 1, "Max bait"]].forEach(function (c) {
      var v = +th[c[0]]; if (isNaN(v) || v < c[1] || v > c[2]) throw new Error(c[3] + " must be between " + c[1] + " and " + c[2] + ".");
    });
  };

  // Preset cards: pick one; onPick(preset) gets the full preset.
  TL.presetPicker = function (box, presets, currentId, onPick) {
    box.innerHTML = "";
    var grid = h("div", { class: "preset-grid" });
    presets.forEach(function (p) {
      var topics = (p.settings.topics || []).filter(function (t) { return t.include && t.key !== "other"; }).map(function (t) { return t.label; });
      var b = h("button", { type: "button", class: "preset big", "aria-pressed": String(p.id === currentId), onclick: function () {
        grid.querySelectorAll(".preset").forEach(function (x) { x.setAttribute("aria-pressed", String(x === b)); }); onPick(p);
      } }, [h("b", { text: p.name }), h("span", { class: "tagline", text: p.tagline }), h("span", { text: p.description }),
        h("span", { class: "topics-line", text: topics.slice(0, 5).join(" · ") + (topics.length > 5 ? " · …" : "") })]);
      grid.append(b);
    });
    box.append(grid);
  };
  // Apply a preset's feed on top of a profile, keeping personal delivery settings.
  TL.applyPreset = function (s, p) {
    var n = TL.clone(p.settings); delete n.preset;
    n.slack = s.slack || n.slack; n.timezone = s.timezone || n.timezone;
    n.preset_id = p.id;
    return n;
  };

  // ---- API keys ----
  TL.KEYS = [
    { id: "twitterapi_io", label: "twitterapi.io", required: true, placeholder: "Your twitterapi.io API key",
      get: "https://twitterapi.io/dashboard", getLabel: "twitterapi.io/dashboard",
      why: "Fetches tweets. Usually about $10–15 a month at this volume.", check: true },
    { id: "ai_gateway", label: "Vercel AI Gateway", required: true, placeholder: "vck_…",
      get: "https://vercel.com/dashboard/ai-gateway", getLabel: "vercel.com → AI Gateway → API Keys",
      why: "Runs Jev (scores tweets) and Claude (writes the thesis). Usually about $10–20 a month.", check: true },
    { id: "typesafe", label: "TypeSafe", required: false, placeholder: "Optional",
      get: "https://console.typesafe.ai", getLabel: "console.typesafe.ai",
      why: "Optional. Sends Jev calls to TypeSafe directly instead of through the gateway." }
  ];
  // Key cards. me.keys says which are saved (never the values). opts.onChange(ready) fires when readiness changes.
  TL.keyCards = function (box, me, opts) {
    opts = opts || {};
    box.innerHTML = "";
    var ok = {}; TL.KEYS.forEach(function (k) { ok[k.id] = !!me.keys[k.id]; });
    var redraw = [];
    function ready() { return TL.KEYS.every(function (k) { return !k.required || ok[k.id]; }); }
    TL.KEYS.forEach(function (k) {
      var pill = h("span", { class: "pill" });
      var inp = h("input", { class: "inp", type: "password", autocomplete: "off", spellcheck: "false", placeholder: me.keys[k.id] ? "Saved. Paste a new key to replace it" : k.placeholder, "aria-label": k.label + " key" });
      var note = h("p", { class: "note", hidden: true });
      function tell(m, kind) { note.hidden = !m; note.textContent = m || ""; note.className = "note" + (kind ? " " + kind : ""); }
      function status() {
        if (me.keys[k.id]) setPill(pill, ok[k.id] ? "Connected" : "Saved · check failed", ok[k.id] ? "ok" : "err");
        else setPill(pill, k.required ? "Required" : "Optional", k.required ? "err" : "");
      }
      var btn = h("button", { class: "btn primary", type: "button", text: k.check ? "Check and save" : "Save", onclick: function () {
        var v = inp.value.trim(); if (!v) { inp.focus(); tell("Paste the key first.", "err"); return; }
        btn.disabled = true; tell(k.check ? "Checking with " + k.label + "…" : "Saving…");
        var body = {}; body[k.id] = v;
        (k.check ? TL.api("POST", "/api/keys/check", body) : Promise.resolve(null)).then(function (r) {
          var res = r && r[k.id];
          if (res && !res.ok) { tell(res.message, "err"); return; }
          return TL.api("PUT", "/api/keys", body).then(function (j) {
            me.keys = j.keys; ok[k.id] = true; inp.value = ""; inp.placeholder = "Saved. Paste a new key to replace it";
            tell(res ? res.message || "Works. Saved." : "Saved.", "ok"); status(); if (opts.onChange) opts.onChange(ready());
          });
        }).catch(function (e) { tell(e.message, "err"); }).finally(function () { btn.disabled = false; });
      } });
      inp.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); btn.click(); } });
      var rmBtn = me.keys[k.id] && !k.required ? h("button", { class: "btn", type: "button", text: "Remove", onclick: function () {
        var body = {}; body[k.id] = "";
        TL.api("PUT", "/api/keys", body).then(function (j) { me.keys = j.keys; ok[k.id] = false; rmBtn.remove(); tell("Removed.", "ok"); status(); })
          .catch(function (e) { tell(e.message, "err"); });
      } }) : null;
      var c = h("div", { class: "key" }, [
        h("div", { class: "key-top" }, [h("b", { text: k.label }), pill]),
        h("p", { class: "hint", style: "margin:0 0 10px" }, [k.why + " Get it at ", h("a", { href: k.get, target: "_blank", rel: "noopener", text: k.getLabel }), "."]),
        h("div", { class: "key-row" }, [inp, btn, rmBtn]), note]);
      status(); redraw.push(status);
      if (k.required) box.append(c);
      else { var d = h("details", { class: "more", open: !!me.keys[k.id] }, [h("summary", { text: "Optional: " + k.label }), c]); box.append(d); }
    });
    // Saved keys: re-check once so a revoked key shows up now, not at the next run.
    if (opts.recheck && TL.KEYS.some(function (k) { return k.check && me.keys[k.id]; })) TL.api("POST", "/api/keys/check", {}).then(function (r) {
      TL.KEYS.forEach(function (k) { if (k.check && me.keys[k.id] && r[k.id]) ok[k.id] = !!r[k.id].ok; });
      redraw.forEach(function (f) { f(); });
      if (opts.onChange) opts.onChange(ready());
    }).catch(function () {});
    return { ready: ready };
  };

  function setPill(p, text, kind) { p.textContent = text; p.className = "pill" + (kind ? " " + kind : ""); }
  TL.setPill = setPill;
  function card(logo, name, sub, open, body) {
    var pill = h("span", { class: "pill" });
    var wrap = h("div", { class: "conn-body", hidden: !open }, body);
    var head = h("div", { class: "conn-head", role: "button", tabindex: "0", "aria-expanded": String(!!open),
      onclick: function () { wrap.hidden = !wrap.hidden; head.setAttribute("aria-expanded", String(!wrap.hidden)); },
      onkeydown: function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); head.click(); } } },
      [h("span", { class: "conn-logo", text: logo }), h("span", null, [h("b", { text: name }), h("span", { class: "sub", text: sub })]), pill]);
    return { el: h("div", { class: "conn" }, [head, wrap]), pill: pill, body: wrap };
  }

  // ---- Daily recap: timezone, Slack (their own webhook), Instinct, Muse ----
  var SLACK_MANIFEST = "https://api.slack.com/apps?new_app=1&manifest_json=" + encodeURIComponent(JSON.stringify({
    display_information: { name: "Timeline recap", description: "Your daily read of what tech Twitter is talking about" },
    oauth_config: { scopes: { bot: ["incoming-webhook"] } },
    settings: { org_deploy_enabled: false, socket_mode_enabled: false, token_rotation_enabled: false }
  }));
  function isWebhook(u) { return /^https:\/\/hooks\.slack\.com\/services\/\S+$/.test(u); }
  TL.timezonePicker = function (s, onChange) {
    var zones = []; try { zones = Intl.supportedValuesOf("timeZone"); } catch (e) {}
    if (zones.indexOf(s.timezone) < 0) zones.unshift(s.timezone);
    var sel = h("select", { class: "inp", "aria-label": "Your time zone" }, zones.map(function (z) { return h("option", { value: z, text: z.replace(/_/g, " ") }); }));
    sel.value = s.timezone;
    sel.addEventListener("change", function () { s.timezone = sel.value; if (onChange) onChange(); });
    return h("div", { class: "field" }, [h("label", { text: "Your time zone" }), sel,
      h("p", { class: "hint", text: "Your week starts Monday at midnight here, and daily times below use it." })]);
  };
  function slackCard(s, me, o) {
    var d = s.slack = TL.slackDefaults(s.slack), lastError = (me.slack || {}).last_error;
    var note = h("p", { class: "note", hidden: true });
    function tell(msg, kind) { note.hidden = !msg; note.textContent = msg || ""; note.className = "note" + (kind ? " " + kind : ""); }
    var url = h("input", { class: "inp", type: "url", placeholder: "https://hooks.slack.com/services/…", autocomplete: "off", spellcheck: "false" });
    var enabled = h("input", { type: "checkbox", checked: d.enabled });
    var hour = h("select", { class: "inp", "aria-label": "Post after" });
    for (var i = 0; i < 24; i++) hour.append(h("option", { value: String(i), text: TL.hourLabel(i) }));
    hour.value = String(d.hour);
    var freq = h("select", { class: "inp", "aria-label": "How often" }, [
      h("option", { value: "daily", text: "Once a day" }), h("option", { value: "on_change", text: "When the thesis changes" }),
      h("option", { value: "every_update", text: "Every update (3 hours)" })]);
    freq.value = d.frequency;
    var weekdays = h("input", { type: "checkbox", checked: d.weekdays_only });
    var top = h("input", { class: "inp", type: "number", min: "0", max: "10", step: "1", value: String(d.top_tweets), "aria-label": "Tweets to include" });
    var hourWrap = h("div", { class: "field" }, [h("span", { class: "lbl", text: "After" }), hour]);
    var wdWrap = h("label", { class: "check" }, [weekdays, " Weekdays only"]);
    var c = card("#", "Slack", "Posts to a channel or DM you choose", o.open, []);
    function sync() {
      d.enabled = enabled.checked; d.frequency = freq.value; d.hour = parseInt(hour.value, 10);
      d.weekdays_only = weekdays.checked; d.top_tweets = Math.min(10, Math.max(0, parseInt(top.value, 10) || 0));
      var daily = d.frequency === "daily"; hourWrap.style.visibility = daily ? "" : "hidden"; wdWrap.hidden = !daily;
      if (!d.enabled) setPill(c.pill, me.keys.slack_webhook ? "Connected · off" : "Off");
      else if (!me.keys.slack_webhook) setPill(c.pill, "Needs a webhook", "err");
      else if (lastError) setPill(c.pill, "Last post failed", "err");
      else setPill(c.pill, d.frequency === "daily" ? (d.weekdays_only ? "Weekdays" : "Daily") + " around " + TL.hourLabel(d.hour) + " " + TL.tzShort(s.timezone)
        : d.frequency === "on_change" ? "When the thesis changes" : "Every update", "ok");
      if (o.onChange) o.onChange();
    }
    [enabled, freq, hour, weekdays, top].forEach(function (x) { x.addEventListener("change", sync); x.addEventListener("input", sync); });
    function test() {
      var v = url.value.trim();
      if (v && !isWebhook(v)) { tell("That doesn't look like a Slack webhook. It starts with https://hooks.slack.com/services/.", "err"); url.focus(); return; }
      if (!v && !me.keys.slack_webhook) { tell("Paste the webhook URL first.", "err"); url.focus(); return; }
      tell("Sending a test…");
      TL.api("POST", "/api/slack/test", v ? { webhook: v } : {}).then(function (r) { tell(r.message || (r.ok ? "Sent. Check Slack." : "Slack said no."), r.ok ? "ok" : "err"); })
        .catch(function (e) { tell(e.message, "err"); });
    }
    function save() {
      var v = url.value.trim();
      if (!isWebhook(v)) { tell("Paste the webhook URL first. It starts with https://hooks.slack.com/services/.", "err"); url.focus(); return; }
      tell("Saving…");
      TL.api("PUT", "/api/keys", { slack_webhook: v }).then(function (j) {
        me.keys = j.keys; lastError = null; url.value = ""; enabled.checked = true; sync();
        tell(o.saveHint || "Connected. Posting is on.", "ok");
      }).catch(function (e) { tell(e.message, "err"); });
    }
    var steps = h("details", { class: "more", open: !me.keys.slack_webhook }, [
      h("summary", { text: me.keys.slack_webhook ? "Connected. Replace the webhook or change where it posts" : "Connect Slack (about a minute)" }),
      h("ol", null, [
        h("li", null, [h("a", { class: "btn", href: SLACK_MANIFEST, target: "_blank", rel: "noopener", text: "Create the Slack app ↗" }),
          h("span", { class: "hint", style: "display:block", text: "Slack opens with everything filled in. Pick your workspace, then click Create." })]),
        h("li", { text: "Click Install to Workspace and pick where it should post: a channel, or your own DM." }),
        h("li", { text: "Go to Incoming Webhooks, copy the webhook URL, and paste it here:" })]),
      h("div", { class: "key-row" }, [url, h("button", { class: "btn", type: "button", text: "Send test", onclick: test }),
        h("button", { class: "btn primary", type: "button", text: "Save", onclick: save })])]);
    if (lastError) c.body.append(h("p", { class: "note err", text: "The last post failed: " + lastError }));
    c.body.append(steps, h("div", { style: "height:16px" }),
      h("label", { class: "check", style: "margin-bottom:12px" }, [enabled, " Post my daily recap to Slack"]),
      h("div", { class: "row3" }, [h("div", { class: "field" }, [h("span", { class: "lbl", text: "How often" }), freq]), hourWrap,
        h("div", { class: "field" }, [h("span", { class: "lbl", text: "Tweets to include" }), top])]),
      wdWrap, h("p", { class: "hint", text: "Delivery is checked every hour, so the daily post goes out shortly after the hour you pick, in your time zone." }));
    if (o.sendNow) c.body.append(h("div", { class: "btn-row", style: "margin-top:12px" }, [
      h("button", { class: "btn", type: "button", text: "Send today's recap now", onclick: function () {
        if (!me.keys.slack_webhook) { tell("Connect a webhook first.", "err"); return; }
        tell("Starting…"); TL.api("POST", "/api/run", { slack: "now" }).then(function () { tell("It posts after this update finishes, in a few minutes.", "ok"); })
          .catch(function (e) { tell(e.message, "err"); });
      } }), test && h("button", { class: "btn", type: "button", text: "Send test", onclick: test })]));
    c.body.append(note);
    sync();
    c.resync = sync;
    return c;
  }

  // Instinct and Muse can't be pushed to, so each gets a recurring task that reads this
  // person's private recap URL. Set up per device; remembered locally.
  TL.agentMessage = function (name, recapUrl, pageUrl, time, weekdays, tz) {
    var parts = time.split(":"), hr = +parts[0], min = parts[1];
    var when = (hr % 12 || 12) + ":" + min + (hr < 12 ? " AM" : " PM");
    return "Hi " + name + ". Please set up a recurring task: every " + (weekdays ? "weekday" : "day") + " at " + when + " (" + tz + ")" +
      ", read " + recapUrl + " and send me a short recap with the week's X thesis in a sentence or two, the 2–3 most interesting items under “New today” with their links, " +
      "and this link to my full page: " + pageUrl + "\nKeep it under 100 words. If nothing is marked new, just send the thesis and the link.";
  };
  function agentCard(kind, s, me, open) {
    var A = kind === "instinct"
      ? { name: "Instinct", logo: "In", sub: "Texts it to you from your Instinct thread" }
      : { name: "Muse", logo: "Mu", sub: "Sends it through Meta's Muse, in WhatsApp or at muse.ai" };
    var all = TL.local("tl_deliver") || {}, saved = all[kind] || {};
    var c = card(A.logo, A.name, A.sub, open, []);
    var time = h("input", { class: "inp", type: "time", value: saved.time || "08:00", "aria-label": "Time" });
    var weekdays = h("input", { type: "checkbox", checked: saved.weekdays !== false });
    var num = kind === "instinct" ? h("input", { class: "inp", type: "tel", placeholder: "Instinct's number (optional)", value: saved.number || "", autocomplete: "off" }) : null;
    var preview = h("div", { class: "msg-preview" });
    var note = h("p", { class: "note", hidden: true });
    var primary = h("a", { class: "btn primary", target: kind === "muse" ? "_blank" : null, rel: "noopener" });
    var alt = kind === "muse" ? h("a", { class: "btn", target: "_blank", rel: "noopener", text: "Send in WhatsApp" }) : null;
    function msg() { return TL.agentMessage(A.name, me.recap_url, me.page_url, time.value || "08:00", weekdays.checked, s.timezone); }
    function render() {
      var m = msg(); preview.textContent = m;
      if (kind === "instinct") { primary.textContent = "Text it to Instinct"; primary.href = "sms:" + num.value.replace(/[^\d+]/g, "") + "?&body=" + encodeURIComponent(m); }
      else { primary.textContent = "Open Muse ↗"; primary.href = "https://muse.ai/"; alt.href = "https://wa.me/?text=" + encodeURIComponent(m); }
      var x = (TL.local("tl_deliver") || {})[kind];
      setPill(c.pill, x && x.at ? "Set up · " + new Date(x.at).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "Not set up", x && x.at ? "ok" : "");
    }
    function remember(done) {
      var a = TL.local("tl_deliver") || {};
      a[kind] = Object.assign({}, a[kind], { time: time.value, weekdays: weekdays.checked, number: num ? num.value : undefined }, done ? { at: new Date().toISOString() } : {});
      TL.local("tl_deliver", a); render();
    }
    function done(m) { remember(true); note.hidden = false; note.className = "note ok"; note.textContent = m; }
    [time, weekdays, num].forEach(function (x) { if (x) { x.addEventListener("input", function () { remember(false); }); x.addEventListener("change", render); } });
    primary.addEventListener("click", function () {
      TL.copy(msg()).catch(function () {});
      done(kind === "instinct" ? "Opening your messages app with the text filled in. On a computer? It's copied, so paste it into your Instinct thread." : "Message copied. Paste it into Muse and send it.");
    });
    if (alt) alt.addEventListener("click", function () { done("Pick your Muse chat in WhatsApp and send."); });
    var copyBtn = h("button", { class: "btn", type: "button", text: "Copy message", onclick: function () { TL.copy(msg()).then(function () { done("Copied. Paste it into " + A.name + " and send it."); }); } });
    c.body.append(
      h("ol", null, [h("li", { text: "Pick when you want it." }),
        h("li", { text: "Send this message to " + A.name + ". It sets up a daily task that reads your private recap and sends it to you." })]),
      h("div", { class: "row3", style: "margin-bottom:12px" }, [time, h("label", { class: "check" }, [weekdays, " Weekdays only"]), num]),
      preview, h("div", { class: "btn-row" }, [primary, alt, copyBtn]), note,
      h("p", { class: "hint", style: "margin-top:12px" }, ["Your recap link is private to you and updates every three hours (",
        h("a", { href: me.recap_url, target: "_blank", rel: "noopener", text: "see what " + A.name + " reads" }),
        "). To stop, tell " + A.name + " to cancel the timeline recap."]));
    render();
    c.render = render;
    return c;
  }
  // Mounts timezone + the three delivery cards. s is the profile being edited; me from /api/me.
  TL.deliveries = function (box, s, me, o) {
    o = o || {};
    box.innerHTML = "";
    s.timezone = s.timezone || TL.localTz();
    var sc = slackCard(s, me, Object.assign({ open: true }, o));
    var ic = agentCard("instinct", s, me, !!o.openAgents), mc = agentCard("muse", s, me, false);
    box.append(TL.timezonePicker(s, function () { sc.resync(); ic.render(); mc.render(); if (o.onChange) o.onChange(); }), sc.el, ic.el, mc.el);
  };

  TL.fmtWhen = function (iso) {
    if (!iso) return "";
    var d = new Date(iso), m = Math.round((Date.now() - d) / 60000);
    if (Math.abs(m) < 1) return "just now";
    if (m < 0) { m = -m; return m < 60 ? "in " + m + " min" : "in " + Math.round(m / 60) + " h"; }
    return m < 60 ? m + " min ago" : m < 1440 ? Math.round(m / 60) + " h ago" : d.toLocaleDateString();
  };
  return TL;
})();
