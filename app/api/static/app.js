"use strict";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const state = { mode: "demo", pinSet: false, payees: [], csrf: null, info: null, view: "overview", pools: [], quote: null, skew: 0, before: null, timer: null };
const pending = {};   // idempotency keys kept until the server gives a definitive answer, so retries are safe

// ---- DOM helpers (textContent only: server data is never parsed as HTML)
function el(tag, props = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") e.className = v; else if (k.startsWith("on")) e.addEventListener(k.slice(2), v); else e.setAttribute(k, v);
  }
  e.append(...kids.flat().filter((x) => x != null).map((x) => (x.nodeType ? x : document.createTextNode(String(x)))));
  return e;
}
const fill = (node, ...kids) => node.replaceChildren(...kids.flat().filter((x) => x != null).map((x) => (x.nodeType ? x : document.createTextNode(String(x)))));
const say = (text, kind = "ok") => { const m = $("#msg"); m.textContent = text; m.className = kind; };
const num = (n, min, max) => Number(n).toLocaleString(undefined, { minimumFractionDigits: min, maximumFractionDigits: max });
const ngn = (k) => "\u20A6" + num(k / 100, 2, 2);
const usdt = (m) => num(m / 1e6, 2, 4) + " USDT";
const money = (c, v) => (c === "NGN" ? ngn(v) : usdt(v));
const pct = (x) => (x * 100).toFixed(1) + "%";
const when = (t) => new Date(t * 1000).toLocaleString();
const sign = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");

function parseAmount(text, dec) {
  const m = new RegExp("^(\\d{1," + (14 - dec) + "})(?:\\.(\\d{1," + dec + "}))?$").exec(String(text).trim());
  if (!m) throw new Error("Enter a valid amount with at most " + dec + " decimals.");
  const minor = Number(m[1]) * 10 ** dec + (m[2] ? Number(m[2].padEnd(dec, "0")) : 0);
  if (minor < 1) throw new Error("Amount must be greater than zero.");
  return minor;
}
const decimals = (cur) => (cur === "NGN" ? 2 : 6);

async function api(method, path, body, keyName) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["X-CSRF-Token"] = state.csrf || "";
  headers["X-Funds-Mode"] = state.mode;
  let id = null;
  if (keyName) { id = keyName + JSON.stringify(body || {}); pending[id] ||= crypto.randomUUID(); headers["Idempotency-Key"] = pending[id]; }
  let res;
  try { res = await fetch(path, { method, headers, credentials: "same-origin", body: body === undefined ? undefined : JSON.stringify(body) }); }
  catch (e) { throw new Error("Network error. Repeating the same action is safe."); }
  let data = {};
  try { data = await res.json(); } catch (e) { /* non-JSON error */ }
  if (id && res.status < 500 && res.status !== 429) delete pending[id];
  if (!res.ok) {
    const err = new Error((data.error && data.error.message) || "Request failed (" + res.status + ")");
    err.code = data.error && data.error.code; err.details = data.error || {};
    if (err.code === "live_unavailable" && state.mode === "live") { say(err.message, "err"); setMode("demo"); }
    if (err.code === "session_required" && state.csrf) { showAuth(); say("Your session ended. Please log in again.", "err"); }
    throw err;
  }
  return data;
}

const tag = () => (state.mode === "demo" ? " (demo)" : "");
const simKyc = () => (state.info && state.info.simulation ? " (simulated approval)" : "");
function askPin(what) {
  return new Promise((resolve) => {
    const box = $("#pin-overlay"), input = $("#pin-input");
    $("#pin-what").textContent = what || ""; input.value = ""; box.hidden = false; input.focus();
    const done = (value) => { box.hidden = true; input.value = ""; $("#pin-ok").onclick = $("#pin-cancel").onclick = input.onkeydown = null; resolve(value); };
    $("#pin-ok").onclick = () => done(input.value.trim() || null);
    $("#pin-cancel").onclick = () => done(null);
    input.onkeydown = (e) => { if (e.key === "Enter") done(input.value.trim() || null); if (e.key === "Escape") done(null); };
  });
}
async function withPin(body, what) {
  if (!state.pinSet) { go("security"); throw new Error("Set your transaction PIN first. You need one before any transaction."); }
  const pin = await askPin(what);
  if (!pin) throw new Error("Cancelled.");
  return { ...body, pin };
}
function renderBanner() {
  const b = $("#banner"), i = state.info || {}, onLive = i.data_source === "live";
  b.className = "banner";
  if (state.mode === "live") {
    b.classList.add("live");
    b.textContent = i.live && i.live.sandbox ? "LIVE BOOKS, SANDBOX RAILS. These are the live ledgers, but the payment and chain rails are a sandbox: no real money moves."
      : "LIVE FUNDS. Real money. Check every amount and destination before you confirm.";
  } else {
    if (onLive) b.classList.add("demo-live");
    b.textContent = onLive ? "DEMO FUNDS ON LIVE POOLS. Positions are paper-traded against live pool data. No real money moves and nothing is executed on-chain."
      : "SIMULATION ONLY. Demo funds and synthetic example pool data: no real money, bank, exchange or blockchain is involved.";
  }
  $("#mode-demo").classList.toggle("on", state.mode === "demo"); $("#mode-live").classList.toggle("on", state.mode === "live");
}
function applyMode() { const live = state.mode === "live"; $("#demo-deposit").hidden = live; $("#live-deposit").hidden = !live; }
function setMode(mode) { state.mode = mode; state.quote = null; fill($("#quote")); fill($("#live-instructions")); renderBanner(); applyMode(); go(state.view); }
async function openLive() {
  const s = await api("GET", "/api/live/status"), unmet = s.requirements.filter((r) => !r.ok), onlyTerms = unmet.length === 1 && unmet[0].key === "terms";
  fill($("#live-body"), s.requirements.map((r) => el("div", { class: "req" }, el("b", { class: r.ok ? "ok" : "no" }, r.ok ? "\u2713" : "\u2717"), el("span", {}, r.message))),
    s.rails.sandbox ? el("p", { class: "hint" }, "The rails behind live mode are a sandbox: no real money moves.") : null,
    !s.available && !onlyTerms ? el("p", { class: "hint" }, "Finish the unmet items (most are on the Security tab), then try again.") : null,
    onlyTerms ? [el("p", { class: "hint" }, "Please read and accept:"), ...s.terms.map((t) => el("p", { class: "hint" }, "\u2022 " + t))] : null);
  $("#live-accept").hidden = !onlyTerms; $("#live-go").disabled = !s.available; $("#live-overlay").hidden = false;
}

// ---- screens
function showAuth() {
  state.csrf = null; clearInterval(state.timer);
  $("#auth").hidden = false; $("#app").hidden = true; $("#who").hidden = true;
}
function showApp(info) {
  state.csrf = info.csrf_token; state.info = info;
  $("#username").textContent = info.user.username;
  $("#auth").hidden = true; $("#app").hidden = false; $("#who").hidden = false;
  $("#deposit-limits").textContent = "Per request up to " + ngn(info.deposit_limits.per_request.NGN) + " or " + usdt(info.deposit_limits.per_request.USDT) + "; per day " + ngn(info.deposit_limits.per_day.NGN) + " or " + usdt(info.deposit_limits.per_day.USDT) + ".";
  $("#simnote").textContent = (info.sim_time_scale === 1 ? "Fees accrue in real time. " : "Fee accrual runs " + info.sim_time_scale + "x faster than real time (about " + (info.sim_time_scale / 1440).toFixed(1) + " simulated day per real minute). ") + "Conversion fee " + (info.fx_fee_bps / 100) + "%, quotes valid " + info.quote_ttl_seconds + "s. Pool data: " + info.pool_data + ".";
  state.mode = "demo"; renderBanner(); applyMode(); guard(loadSecurity);
  go(state.view);
  clearInterval(state.timer); state.timer = setInterval(() => { if (!document.hidden) refresh(); }, 10000);
}
function go(view) {
  state.view = view;
  $$("#nav button").forEach((b) => b.classList.toggle("on", b.dataset.view === view));
  $$("[data-panel]").forEach((p) => { p.hidden = p.dataset.panel !== view; });
  refresh(true);
}
async function guard(fn) { try { await fn(); } catch (e) { say(e.message, "err"); } }
const refresh = (full) => guard(async () => {
  const v = state.view;
  if (v === "overview") await loadOverview();
  if (v === "wallet") { if (full) await loadPayees(); await loadWithdrawals(); }   // the timed refresh must not reset the form
  if (v === "pools" && full) await loadPools();
  if (v === "positions") { if (full || !state.pools.length) await loadPools(); await loadPositions(); }
  if (v === "history" && full) { state.before = null; fill($("#history")); await loadHistory(); }
  if (v === "security" && full) { await loadSecurity(); await loadActivity(); }
});

// ---- overview
async function loadOverview() {
  const w = await api("GET", "/api/wallet");
  fill($("#balances"), ["NGN", "USDT"].map((c) => el("div", { class: "card" }, el("div", { class: "hint" }, c + " wallet"), el("div", { class: "big" }, money(c, w.balances[c])))));
  fill($("#valuation"), el("div", { class: "kv" },
    el("span", { class: "hint" }, "Total value"), el("b", {}, usdt(w.total_value_usdt_micro) + (w.total_value_ngn_kobo === null ? "  (NGN balance not included: no rate source)" : "  (about " + ngn(w.total_value_ngn_kobo) + ")")),
    el("span", { class: "hint" }, "In open positions"), el("span", {}, usdt(w.position_value_usdt_micro) + " across " + w.open_positions + " position(s)"),
    el("span", { class: "hint" }, "Unrealized P&L"), el("span", { class: sign(w.unrealized_pnl_usdt_micro) }, usdt(w.unrealized_pnl_usdt_micro)),
    el("span", { class: "hint" }, "Realized P&L"), el("span", { class: sign(w.realized_pnl_usdt_micro) }, usdt(w.realized_pnl_usdt_micro)),
    el("span", { class: "hint" }, "Pending withdrawals"), el("span", {}, ngn(w.pending_withdrawals.NGN) + " and " + usdt(w.pending_withdrawals.USDT)),
    el("span", { class: "hint" }, "Rate used"), el("span", {}, w.rate ? "bid " + Number(w.rate.bid).toFixed(2) + " / ask " + Number(w.rate.ask).toFixed(2) + " NGN per USDT (" + w.rate.source + ")" : "none configured")));
}

// ---- wallet
function describePayee(a) {
  const d = a.destination;
  return a.currency === "NGN" ? (a.bank_name || "bank " + d.bank_code) + " ..." + d.account_number.slice(-4) + (a.account_name ? " (" + a.account_name + ")" : "") : d.network + " ..." + d.address.slice(-6);
}
async function loadPayees() {
  state.payees = (await api("GET", "/api/payouts/accounts")).accounts;
  const rows = state.payees.map((a) => el("tr", {}, el("td", {}, a.label), el("td", {}, a.currency), el("td", {}, describePayee(a)),
    el("td", {}, el("button", { class: "ghost", onclick: () => act("/api/payouts/accounts/" + a.beneficiary_id + "/delete", {}, "Payout account removed.", "Remove this payout account") }, "Remove"))));
  fill($("#payouts"), el("tr", {}, ["Label", "Currency", "Destination", ""].map((h) => el("th", {}, h))), rows.length ? rows : el("tr", {}, el("td", { colspan: 4, class: "hint" }, "No saved accounts yet.")));
  fillPayeeSelect();
}
function fillPayeeSelect() {
  const cur = $("#wd-currency").value;
  fill($("#wd-payee"), state.payees.filter((a) => a.currency === cur).map((a) => el("option", { value: a.beneficiary_id }, a.label + " - " + describePayee(a))), el("option", { value: "" }, "New destination..."));
  toggleNew();
}
function toggleNew() {
  const cur = $("#wd-currency").value;
  $("#wd-new").hidden = $("#wd-payee").value !== ""; $("#wd-ngn").hidden = cur !== "NGN"; $("#wd-usdt").hidden = cur !== "USDT";
}
function offerBanks(selectSel, rowSel, candidates) {
  fill($(selectSel), candidates.map((c) => el("option", { value: c.bank_code }, c.bank_name + (c.account_name ? " - " + c.account_name : ""))));
  $(rowSel).hidden = false;
}
async function findBank() {
  const number = $("#pa-ngn input[name=account_number]").value.trim();
  const d = await api("POST", "/api/payouts/resolve", { account_number: number });
  if (!d.candidates.length) throw new Error("No known bank matches this number. Check it.");
  offerBanks("#pa-bank", "#pa-bank-row", d.candidates);
  say(d.candidates.length + " possible bank(s). Pick the one whose account name is yours (names are simulated here).");
}
async function loadSecurity() {
  const s = await api("GET", "/api/security"), k = s.kyc;
  fill($("#kyc"), el("div", { class: "kv" }, el("span", { class: "hint" }, "Level"), el("b", {}, "Tier " + k.tier + " (" + k.status + ")"),
    el("span", { class: "hint" }, "NGN withdrawals"), el("span", {}, "up to " + ngn(k.limits.NGN.max_amount_minor) + " each, " + ngn(k.limits.NGN.daily_cap_minor) + " per day"),
    el("span", { class: "hint" }, "USDT withdrawals"), el("span", {}, "up to " + usdt(k.limits.USDT.max_amount_minor) + " each, " + usdt(k.limits.USDT.daily_cap_minor) + " per day")));
  $("#kyc-basic").hidden = !(k.tier === 0 && k.status !== "pending"); $("#kyc-enhanced").hidden = !(k.tier === 1 && k.status !== "pending");
  fill($("#email-status"), s.email ? s.email + (s.email_verified ? " (verified)" : " (not verified yet)") : "No email on file. Add one to enable password reset.");
  fill($("#totp-status"), s.totp.enabled ? "On. A code from your authenticator app is required at login." : "Off.");
  $("#totp-begin").hidden = s.totp.enabled; $("#totp-disable").hidden = !s.totp.enabled;
  state.pinSet = s.pin.set; fill($("#pin-status"), s.pin.set ? "PIN is set." : "No PIN yet. Set one before your first transaction.");
  $("#pin-set").hidden = s.pin.set; $("#pin-change").hidden = !s.pin.set; $("#pin-reset-box").hidden = !s.pin.set;
}
async function loadWithdrawals() {
  const d = await api("GET", "/api/withdrawals");
  const head = el("tr", {}, ["Requested", "Currency", "Amount", "Fee", "Status", "To", ""].map((h) => el("th", {}, h)));
  const rows = d.withdrawals.map((w) => {
    const dest = w.destination.account_number ? (w.bank_name || "bank " + w.destination.bank_code) + " ..." + w.destination.account_number.slice(-4) : w.destination.network + " ..." + w.destination.address.slice(-6);
    const acts = el("td", {});
    if (w.status === "pending") acts.append(el("button", { class: "ghost", onclick: () => act("/api/withdrawals/" + w.withdrawal_id + "/cancel", {}, "Withdrawal cancelled and funds returned.") }, "Cancel"));
    if (state.mode === "demo" && (w.status === "pending" || w.status === "processing")) {
      acts.append(" ", el("button", { onclick: () => act("/api/withdrawals/" + w.withdrawal_id + "/simulate", { outcome: "complete" }, "Simulated payout completed.") }, "Simulate payout"));
      acts.append(" ", el("button", { class: "warn", onclick: () => act("/api/withdrawals/" + w.withdrawal_id + "/simulate", { outcome: "fail" }, "Simulated payout failed; funds returned.") }, "Simulate failure"));
    }
    return el("tr", {}, el("td", {}, w.created_at), el("td", {}, w.currency), el("td", {}, money(w.currency, w.amount_minor)), el("td", {}, money(w.currency, w.fee_minor)), el("td", {}, el("span", { class: "pill" }, w.status)), el("td", {}, dest), acts);
  });
  fill($("#withdrawals"), head, rows.length ? rows : el("tr", {}, el("td", { colspan: 7, class: "hint" }, "No withdrawals yet.")));
}
async function act(path, body, okText, pinFor) { await guard(async () => { await api("POST", path, pinFor ? await withPin(body, pinFor) : body); say(okText); await refresh(true); }); }

// ---- conversion
function renderQuote() {
  const q = state.quote, box = $("#quote");
  if (!q) return fill(box);
  const left = Math.max(0, Math.round(q.expires_at - (Date.now() / 1000 + state.skew)));
  const done = q.done, expired = left <= 0;
  const buy = el("button", { onclick: executeQuote }, "Convert now");
  buy.disabled = expired || done;
  fill(box, el("div", { class: "kv mt" },
    el("span", { class: "hint" }, "You pay"), el("b", {}, money(q.source_currency, q.source_amount_minor)),
    el("span", { class: "hint" }, "You receive"), el("b", {}, money(q.target_currency, q.target_amount_minor)),
    el("span", { class: "hint" }, "Fee included"), el("span", {}, money(q.target_currency, q.fee_minor) + " (" + q.fee_bps / 100 + "%)"),
    el("span", { class: "hint" }, "Rate"), el("span", {}, Number(q.rate).toFixed(2) + " NGN per USDT (" + q.rate_source + ")"),
    el("span", { class: "hint" }, "Status"), el("span", { class: done ? "pos" : expired ? "neg" : "" }, done ? "Executed" : expired ? "Expired. Get a new quote." : "Valid for " + left + "s")), buy);
}
async function executeQuote() {
  await guard(async () => {
    const q = state.quote; await api("POST", "/api/quotes/" + q.quote_id + "/execute", await withPin({}, "Confirm this conversion"), "exec:" + q.quote_id);
    q.done = true; say("Conversion completed" + tag() + "."); renderQuote();
  });
}

// ---- pools
async function loadPools() {
  state.pools = (await api("GET", "/api/pools")).pools;
  const head = el("tr", {}, ["Pool", "Type", "Eligible", "Fee APR", "IL cost", "Net APR", "Risk", "TVL", "Notes"].map((h) => el("th", {}, h)));
  const rows = state.pools.map((p) => el("tr", {}, el("td", {}, p.tokens.join("/") + " " + p.chain), el("td", {}, p.category), el("td", { class: p.eligible ? "pos" : "neg" }, p.eligible ? "yes" : "no"),
    el("td", {}, pct(p.fee_apr)), el("td", {}, pct(p.il_apr)), el("td", { class: sign(p.net_apr) }, pct(p.net_apr)), el("td", {}, p.risk_score.toFixed(2)), el("td", {}, "$" + num(p.tvl_usd / 1e6, 1, 1) + "M"),
    el("td", { class: "hint" }, (p.eligible ? p.warnings : p.reasons).join("; "))));
  fill($("#pools"), head, rows);
  const opt = (p) => el("option", { value: p.pool_id }, p.tokens.join("/") + " " + p.chain);
  fill($("#scenario-pool"), state.pools.map(opt));
  fill($("#open-pool"), state.pools.filter((p) => p.eligible).map(opt));
}

// ---- positions
async function loadPositions() {
  const d = await api("GET", "/api/positions");
  const head = el("tr", {}, ["Pair", "Pool", "Principal", "Value + fees", "Fees", "P&L", "Sim. days", "Status", ""].map((h) => el("th", {}, h)));
  const rows = d.positions.map((p) => el("tr", {}, el("td", {}, p.pair), el("td", { class: "hint" }, p.pool_id), el("td", {}, usdt(p.principal_minor)), el("td", {}, usdt(p.value_minor)), el("td", {}, usdt(p.fees_minor)),
    el("td", { class: sign(p.pnl_minor) }, usdt(p.pnl_minor)), el("td", {}, p.simulated_days), el("td", {}, el("span", { class: "pill" }, p.status)),
    el("td", {}, p.status === "closed" ? "" : el("button", { class: "ghost", onclick: () => act("/api/positions/" + p.position_id + "/close", {}, "Position closed" + tag() + ".", "Close this position") }, "Close"))));
  fill($("#positions"), head, rows.length ? rows : el("tr", {}, el("td", { colspan: 9, class: "hint" }, "No positions yet.")));
  $("#position-note").textContent = "Minimum " + usdt(state.info.min_position_minor) + ". Position values come from a simulation model (fees at the pool's modelled APR, impermanent loss from price change), not from a market.";
}

// ---- history and activity
async function loadHistory() {
  const d = await api("GET", "/api/wallet/history?limit=50" + (state.before ? "&before=" + state.before : ""));
  const t = $("#history");
  if (!t.children.length) t.append(el("tr", {}, ["When (UTC)", "Type", "Currency", "Amount", "Note"].map((h) => el("th", {}, h))));
  d.entries.forEach((e) => t.append(el("tr", {}, el("td", {}, e.created_at), el("td", {}, e.operation_type), el("td", {}, e.currency), el("td", { class: sign(e.amount_minor) }, (e.amount_minor > 0 ? "+" : "-") + money(e.currency, Math.abs(e.amount_minor))), el("td", { class: "hint" }, e.memo))));
  state.before = d.next_before; $("#more").hidden = !d.next_before;
}
async function loadActivity() {
  const d = await api("GET", "/api/activity?limit=30");
  fill($("#activity"), el("tr", {}, ["When", "Event", "Detail"].map((h) => el("th", {}, h))), d.events.map((e) => el("tr", {}, el("td", {}, when(e.created_at)), el("td", {}, e.event.replace(/_/g, " ")), el("td", { class: "hint" }, Object.entries(e.detail).map(([k, v]) => k + ": " + v).join(", ")))));
}

// ---- forms
const forms = {
  async login(f) {
    const body = { username: f.username.value, password: f.password.value };
    if (f.otp.value.trim()) body.otp = f.otp.value.trim();
    try { await api("POST", "/api/auth/login", body); }
    catch (e) { if (e.code === "otp_required") { $("#otp-row").hidden = false; throw new Error("Enter the code from your authenticator app, then log in again. If you lost your phone, use Forgot password: resetting by email turns the authenticator off."); } throw e; }
    f.password.value = ""; f.otp.value = ""; $("#otp-row").hidden = true; showApp(await api("GET", "/api/session"));
  },
  async register(f) {
    const body = { username: f.username.value, password: f.password.value };
    if (f.email.value.trim()) body.email = f.email.value.trim();
    await api("POST", "/api/auth/register", body); say(body.email ? "Account created. A verification code was sent (see the server console). You can log in now." : "Account created. You can log in now.");
    $("#login-form").username.value = f.username.value; f.password.value = ""; authTab("login");
  },
  async resetRequest(f) { await api("POST", "/api/auth/password-reset/request", { email: f.email.value.trim() }); say("If that email is verified on an account, a code was sent (in this simulation: server console and data/outbox.log)."); },
  async resetConfirm(f) { await api("POST", "/api/auth/password-reset/confirm", { email: f.email.value.trim(), code: f.code.value.trim(), new_password: f.password.value }); f.reset(); $("#reset").hidden = true; say("Password reset. Log in with your new password."); },
  async deposit(f) { const c = f.currency.value; await api("POST", "/api/wallet/deposits", await withPin({ currency: c, amount_minor: parseAmount(f.amount.value, decimals(c)) }, "Add demo funds"), "dep"); say("Simulated deposit added."); f.amount.value = ""; },
  async withdraw(f) {
    const c = f.currency.value, body = { currency: c, amount_minor: parseAmount(f.amount.value, decimals(c)) };
    if (f.payee.value) body.beneficiary_id = f.payee.value;
    else if (c === "NGN") { body.destination = { account_number: f.account_number.value.trim() }; if (!$("#wd-bank-row").hidden && f.bank.value) body.destination.bank_code = f.bank.value; }
    else body.destination = { network: f.network.value, address: f.address.value.trim() };
    try { await api("POST", "/api/withdrawals", await withPin(body, "Confirm this withdrawal"), "wd"); }
    catch (e) {
      if (e.code === "ambiguous_bank") { offerBanks("#wd-bank", "#wd-bank-row", e.details.candidates); throw new Error("Several banks match this account number. Choose the bank and submit again."); }
      throw e;
    }
    $("#wd-bank-row").hidden = true; f.amount.value = ""; say(state.mode === "demo" ? "Withdrawal requested. Use the simulate buttons to play the payout provider." : "Withdrawal sent to the payout rail. It completes when the rail confirms."); await loadWithdrawals();
  },
  async payoutAdd(f) {
    const c = f.currency.value, destination = c === "NGN" ? { account_number: f.account_number.value.trim() } : { network: f.network.value, address: f.address.value.trim() };
    if (c === "NGN" && !$("#pa-bank-row").hidden && f.bank.value) destination.bank_code = f.bank.value;
    try { await api("POST", "/api/payouts/accounts", await withPin({ currency: c, label: f.label.value.trim(), destination }, "Save this payout account")); }
    catch (e) { if (e.code === "ambiguous_bank") { offerBanks("#pa-bank", "#pa-bank-row", e.details.candidates); throw new Error("Several banks match. Choose the bank and save again."); } throw e; }
    f.reset(); $("#pa-bank-row").hidden = true; say("Payout account saved."); await loadPayees();
  },
  async liveDeposit(f) {
    const d = await api("POST", "/api/live/deposits", await withPin({ amount_minor: parseAmount(f.amount.value, 2) }, "Request deposit instructions"), "ldep"), i = d.instructions;
    fill($("#live-instructions"), el("div", { class: "kv mt" }, el("span", { class: "hint" }, "Reference"), el("b", {}, d.reference), el("span", { class: "hint" }, "Bank"), el("span", {}, i.bank_name),
      el("span", { class: "hint" }, "Account number"), el("span", {}, i.account_number), el("span", { class: "hint" }, "Account name"), el("span", {}, i.account_name),
      el("span", { class: "hint" }, "Amount"), el("span", {}, ngn(i.amount_minor))), el("p", { class: "hint" }, i.note));
    f.reset(); say("Deposit instructions ready. Your balance updates when the rail confirms the transfer.");
  },
  async pinSet(f) {
    if (f.pin.value !== f.again.value) throw new Error("The two PINs do not match.");
    await api("POST", "/api/security/pin/set", { password: f.password.value, pin: f.pin.value }); f.reset(); say("PIN set. You will be asked for it to confirm each transaction."); await loadSecurity();
  },
  async pinChange(f) {
    if (f.pin.value !== f.again.value) throw new Error("The two new PINs do not match.");
    await api("POST", "/api/security/pin/change", { current_pin: f.current.value, new_pin: f.pin.value }); f.reset(); say("PIN changed.");
  },
  async pinResetConfirm(f) { await api("POST", "/api/security/pin/reset/confirm", { code: f.code.value.trim(), password: f.password.value, new_pin: f.pin.value }); f.reset(); $("#pin-reset").hidden = true; say("PIN reset."); await loadSecurity(); },
  async kycBasic(f) { await api("POST", "/api/kyc/basic", { full_name: f.fullname.value.trim(), bvn: f.bvn.value.trim() }); f.reset(); say("Identity verified" + simKyc() + ". Limits raised."); await loadSecurity(); },
  async kycEnhanced(f) { await api("POST", "/api/kyc/enhanced", { address: f.address.value.trim() }); f.reset(); say("Address verified" + simKyc() + "."); await loadSecurity(); },
  async setEmail(f) { await api("POST", "/api/security/email", { email: f.email.value.trim() }); f.reset(); say("Verification code sent (in this simulation: server console and data/outbox.log)."); await loadSecurity(); },
  async verifyEmail(f) { await api("POST", "/api/security/email/verify", { code: f.code.value.trim() }); f.reset(); say("Email verified."); await loadSecurity(); },
  async totpConfirm(f) { await api("POST", "/api/security/totp/confirm", { code: f.code.value.trim() }); f.reset(); $("#totp-setup").hidden = true; say("Authenticator app turned on."); await loadSecurity(); },
  async totpDisable(f) { await api("POST", "/api/security/totp/disable", { password: f.password.value, code: f.code.value.trim() }); f.reset(); say("Authenticator app turned off."); await loadSecurity(); },
  async quote(f) {
    const c = f.source.value, q = await api("POST", "/api/quotes", { source_currency: c, amount_minor: parseAmount(f.amount.value, decimals(c)) });
    state.quote = q; state.skew = q.server_time - Date.now() / 1000; renderQuote(); say("Quote ready. It expires in " + q.ttl_seconds + " seconds.");
  },
  async recommend(f) {
    const minor = parseAmount(f.amount.value, 6), r = await api("GET", "/api/recommendation?amount_minor=" + minor), box = $("#recommendation");
    const head = el("tr", {}, ["Pool", "Type", "Amount", "Net APR after gas", "Risk"].map((h) => el("th", {}, h)));
    const rows = r.allocations.map((a) => el("tr", {}, el("td", {}, a.tokens.join("/") + " " + a.chain), el("td", {}, a.category), el("td", {}, usdt(a.amount_minor)), el("td", {}, pct(a.net_apr_after_gas)), el("td", {}, a.risk_score.toFixed(2))));
    const deploy = el("button", { onclick: () => act("/api/positions/deploy", { amount_minor: minor }, "Allocation deployed" + tag() + ".", "Open these positions") }, "Open these positions");
    deploy.disabled = !r.allocations.length || state.mode === "live";
    fill(box, el("div", { class: "scroll" }, el("table", {}, head, rows)), el("p", { class: "hint" }, "Reserve kept: " + usdt(r.reserve_minor) + ". Unallocated: " + usdt(r.unallocated_minor) + ". " + r.disclaimer), deploy, el("pre", { class: "hint pre" }, r.rationale));
  },
  async scenario(f) {
    const pctv = Number(f.pct.value); if (!Number.isFinite(pctv)) throw new Error("Enter a number for the price change.");
    const s = await api("GET", "/api/pools/" + encodeURIComponent(f.pool.value) + "/scenario?price_change_pct=" + encodeURIComponent(pctv) + "&days=30");
    fill($("#scenario"), el("div", { class: "kv mt" },
      el("span", { class: "hint" }, "Impermanent loss"), el("b", { class: sign(s.impermanent_loss_pct) }, s.impermanent_loss_pct.toFixed(2) + "%"),
      el("span", { class: "hint" }, "Modelled fees (" + s.days + " days)"), el("span", {}, "$" + s.modelled_fees.toFixed(2) + " on $" + s.assumed_deposit_usdt),
      el("span", { class: "hint" }, "Net vs holding"), el("b", { class: sign(s.net_vs_hold) }, "$" + s.net_vs_hold.toFixed(2))), el("p", { class: "hint" }, s.warnings.join(" ")));
  },
  async open(f) { if (!f.pool.value) throw new Error("No eligible pool is available."); await api("POST", "/api/positions", await withPin({ pool_id: f.pool.value, amount_minor: parseAmount(f.amount.value, 6) }, "Open this position"), "open"); say("Position opened" + tag() + "."); f.amount.value = ""; await loadPositions(); },
  async password(f) { await api("POST", "/api/auth/change-password", { current_password: f.current.value, new_password: f.next.value }); f.reset(); say("Password changed. Your other sessions were signed out."); },
};
function authTab(name) { $$("[data-auth]").forEach((b) => b.classList.toggle("on", b.dataset.auth === name)); $("#login-form").hidden = name !== "login"; $("#register-form").hidden = name !== "register"; }

document.addEventListener("submit", (ev) => {
  const f = ev.target, name = f.dataset.form; if (!name) return;
  ev.preventDefault(); const btn = $("button[type=submit]", f); btn.disabled = true;
  guard(() => forms[name](f)).finally(() => { btn.disabled = false; });
});
$$("[data-auth]").forEach((b) => b.addEventListener("click", () => authTab(b.dataset.auth)));
$$("#nav button").forEach((b) => b.addEventListener("click", () => go(b.dataset.view)));
$("#wd-currency").addEventListener("change", fillPayeeSelect);
$("#wd-payee").addEventListener("change", toggleNew);
$("#pa-currency").addEventListener("change", (e) => { $("#pa-ngn").hidden = e.target.value !== "NGN"; $("#pa-usdt").hidden = e.target.value !== "USDT"; $("#pa-bank-row").hidden = true; });
$("#pa-find").addEventListener("click", () => guard(findBank));
$("#mode-demo").addEventListener("click", () => { if (state.mode !== "demo") setMode("demo"); });
$("#mode-live").addEventListener("click", () => { if (state.mode !== "live") guard(openLive); });
$("#live-cancel").addEventListener("click", () => { $("#live-overlay").hidden = true; });
$("#live-go").addEventListener("click", () => { $("#live-overlay").hidden = true; setMode("live"); });
$("#live-accept").addEventListener("click", () => guard(async () => {
  const pin = await askPin("Accept the live-mode terms"); if (!pin) return;
  await api("POST", "/api/live/terms", { pin }); await openLive();
}));
$("#pin-forgot").addEventListener("click", () => guard(async () => {
  await api("POST", "/api/security/pin/reset/request", {}); $("#pin-reset").hidden = false; say("A code was sent to your verified email (in this simulation: the server console and data/outbox.log).");
}));
$("#show-reset").addEventListener("click", () => { $("#reset").hidden = !$("#reset").hidden; });
$("#totp-begin").addEventListener("click", () => guard(async () => {
  const d = await api("POST", "/api/security/totp/begin", {}); fill($("#totp-secret"), "Secret: " + d.secret + "\n\n" + d.otpauth_uri); $("#totp-setup").hidden = false;
}));
$("#more").addEventListener("click", () => guard(loadHistory));
$("#logout").addEventListener("click", () => guard(async () => { await api("POST", "/api/auth/logout", {}); showAuth(); say("Logged out."); }));
$("#logout-all").addEventListener("click", () => guard(async () => { await api("POST", "/api/auth/logout-all", {}); showAuth(); say("Signed out everywhere."); }));
setInterval(() => { if (state.quote && !$("#app").hidden && state.view === "convert") renderQuote(); }, 1000);

(async () => { try { showApp(await api("GET", "/api/session")); } catch (e) { showAuth(); } })();
