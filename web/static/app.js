// clanker-learn's page: the login, the projects, a Shell, an Editor and the web apps of a
// project's sandbox. Written new after the accepted mock; two parts are RS's (webui/static/
// app.js at RS commit 62788a8): the login proof (deriveLoginProof) and the terminal
// (openTerminal, handleControl). Plain script, no framework, no build step.
//
// Every string that comes from the server or a sandbox goes into the page as text: the
// page is built with h() below, which makes text nodes, and never with innerHTML.
// The password and the proof live in local variables of the call that uses them, nowhere else.
"use strict";

// ---- the login proof (RS's) -------------------------------------------------------
// MIRROR-PAIR LOCKSTEP with outer/broker_auth.py's LOGIN_PROOF_SALT / LOGIN_PROOF_ITERATIONS:
// both sides MUST stay equal or every login fails.
const LOGIN_PROOF_SALT_STR = "rs-broker-login-v1";
const LOGIN_PROOF_ITERATIONS = 600000;
// broker_auth.MIN_PASSWORD_LENGTH: the broker sees only the proof, so the page holds the line.
const MIN_PASSWORD_LENGTH = 8;

const PROJECTS_EVERY_MS = 4000;
const SERVICES_EVERY_MS = 5000;
const OP_EVERY_MS = 1000;
const LAYOUT_KEY = "cl-layout";   // browser storage: which project and tab were shown, nothing else

const b64 = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf)));

// The login proof: base64 of 256 PBKDF2 bits over the fixed public salt. Canonical wire
// form: must match outer/broker_auth.py::derive_login_proof byte for byte.
async function deriveLoginProof(password) {
    const enc = new TextEncoder();
    const baseKey = await crypto.subtle.importKey(
        "raw", enc.encode(password), "PBKDF2", false, ["deriveBits"],
    );
    const bits = await crypto.subtle.deriveBits(
        { name: "PBKDF2", salt: enc.encode(LOGIN_PROOF_SALT_STR),
          iterations: LOGIN_PROOF_ITERATIONS, hash: "SHA-256" },
        baseKey, 256,
    );
    return b64(bits);
}

// ---- small helpers -------------------------------------------------------------------

// An element. Attributes "class", "text" and on<event> are handled; strings among the
// children become text nodes.
function h(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
        if (v === null || v === undefined || v === false) continue;
        if (k === "class") e.className = v;
        else if (k === "text") e.textContent = String(v);
        else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
        else e.setAttribute(k, v === true ? "" : String(v));
    }
    for (const kid of kids.flat()) {
        if (kid === null || kid === undefined || kid === false) continue;
        e.append(kid instanceof Node ? kid : String(kid));
    }
    return e;
}

const enc = encodeURIComponent;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const projectPath = (name, tail) => `/broker/project/${enc(name)}${tail || ""}`;

function toast(text, bad) {
    const t = document.getElementById("toast");
    t.textContent = text;
    t.className = bad ? "bad" : "";
    t.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { t.hidden = true; }, 4000);
}

function loadLayout() {
    try {
        const v = JSON.parse(localStorage.getItem(LAYOUT_KEY) || "{}");
        return { project: typeof v.project === "string" ? v.project : null,
                 tab: typeof v.tab === "string" ? v.tab : null };
    } catch (_) {
        return { project: null, tab: null };
    }
}

function saveLayout() {
    try {
        localStorage.setItem(LAYOUT_KEY, JSON.stringify({ project: S.current, tab: S.current ? tabOf(S.current) : null }));
    } catch (_) { /* no storage: the page works without it */ }
}

// One request to the web server: {status, body}. status 0 is "no answer". A 401 on a route
// that needs the login ends the page's login.
async function api(method, path, body) {
    const opts = { method, credentials: "same-origin", cache: "no-store", headers: {} };
    if (body !== undefined) {
        opts.headers["Content-Type"] = "application/json";
        opts.body = JSON.stringify(body);
    }
    let res;
    try {
        res = await fetch(path, opts);
    } catch (_) {
        return { status: 0, body: { ok: false, error: { kind: "network" } } };
    }
    let data;
    try { data = await res.json(); } catch (_) { data = null; }
    if (!data || typeof data !== "object") {
        data = { ok: false, error: { kind: res.status === 403 ? "origin" : "http", message: `answer ${res.status}` } };
    }
    if (res.status === 401 && S.loggedIn && !path.startsWith("/broker/login") && !path.startsWith("/broker/setup")) {
        loggedOut("Your login has ended. Log in again.");
    }
    return { status: res.status, body: data };
}

// A refusal or failure in words a person reads.
function errText(body) {
    const e = (body && body.error) || {};
    const msg = typeof e.message === "string" ? e.message : "";
    switch (e.kind) {
        case "network": return "The web server does not answer.";
        case "broker_unavailable": return "The broker does not answer. clanker-learn may be starting or stopping; try again in a moment.";
        case "forbidden": return "The broker refused the web server.";
        case "origin": return "The web server refused a request from this page's address.";
        case "unauthorized": return "Your login has ended.";
        case "step_up_required": return "That password is not right.";
        case "busy": return msg || "Something else is being done with this project; try again when it is over.";
        case "bad_request": return msg || "The web server did not understand the request.";
        default: return msg || (e.kind ? `Refused (${e.kind}).` : "Something went wrong.");
    }
}

const NAME_RULE = "A project name is 1 to 31 lower-case letters, digits or dashes, and starts and ends with a letter or a digit.";

// ---- state ------------------------------------------------------------------------------

const S = {
    loggedIn: false,
    projects: [],          // the broker's list rows
    ops: [],               // operations in flight: {op_id, project, verb}
    current: null,         // the project shown
    creating: null,        // a project being created: shown before the broker lists it
    tabs: {},              // project -> tab id shown
    services: {},          // project -> {id: row}, as the service list last said
    panes: {},             // "project|tab" -> pane record
    timers: [],
    shell: null,           // the app's fixed elements once logged in
};

const tabOf = (project) => S.tabs[project] || "shell";
const paneKey = (project, tab) => `${project}|${tab}`;
const projectRow = (name) => S.projects.find((p) => p.name === name) || null;
const opOf = (name) => S.ops.find((o) => o.project === name) || null;

// ---- start: set-up, login or the projects -----------------------------------------------

async function start() {
    if (!window.crypto || !crypto.subtle) {
        return screen("This page must be opened over HTTPS.", [
            h("p", { class: "muted" }, "The browser derives the login from your password with its own cryptography, which it offers to secure pages only."),
        ]);
    }
    screen("clanker-learn", [h("p", { class: "muted" }, "Asking the server…")]);
    const setup = await api("GET", "/broker/setup");
    if (!setup.body.ok) return unavailable(setup.body);
    if (setup.body.needed) return firstPasswordScreen();
    const list = await api("GET", "/broker/projects");
    if (list.status === 401) return loginScreen();
    if (!list.body.ok) return unavailable(list.body);
    enterApp(list.body.result);
}

// A centred card in place of the whole page.
function screen(title, body) {
    teardownApp();
    const app = document.getElementById("app");
    app.replaceChildren(h("main", { class: "center" }, h("div", { class: "card entry" }, h("h1", {}, title), body)));
}

function unavailable(body) {
    screen("clanker-learn", [
        h("p", { class: "bad" }, errText(body)),
        h("div", { class: "btns" }, h("button", { class: "primary", onclick: start }, "Try again")),
    ]);
}

function formRow(label, input, note) {
    return h("label", { class: "field" }, h("span", {}, label), input, note ? h("span", { class: "note" }, note) : null);
}

function loginScreen(message, plain) {
    const pw = h("input", { type: "password", autocomplete: "current-password", required: true });
    const said = h("p", { class: message && !plain ? "bad" : "muted" }, message || "");
    const go = h("button", { class: "primary", type: "submit" }, "Log in");
    const form = h("form", { method: "post", action: "#", onsubmit: async (ev) => {
        ev.preventDefault();
        if (!pw.value) return;
        go.disabled = true;
        said.className = "muted";
        said.textContent = "Checking…";
        const proof = await deriveLoginProof(pw.value);
        pw.value = "";
        const r = await api("POST", "/broker/login", { proof });
        go.disabled = false;
        if (r.body.ok) return start();
        said.className = "bad";
        said.textContent = loginRefusal(r);
        pw.focus();
    } }, formRow("Password", pw), said, h("div", { class: "btns" }, go));
    screen("clanker-learn", [form]);
    pw.focus();
}

function loginRefusal(r) {
    const e = r.body.error || {};
    if (r.status === 401 || e.kind === "auth") return "That password is not right.";
    if (r.status === 429 || e.kind === "rate_limited") {
        return e.retry_after ? `Too many wrong passwords. Wait ${e.retry_after} seconds and try again.`
                             : (e.message || "Too many wrong passwords. Wait a minute and try again.");
    }
    return errText(r.body);
}

function firstPasswordScreen() {
    const code = h("input", { type: "text", autocomplete: "off", spellcheck: "false", required: true });
    const pw1 = h("input", { type: "password", autocomplete: "new-password", required: true });
    const pw2 = h("input", { type: "password", autocomplete: "new-password", required: true });
    const said = h("p", { class: "muted" });
    const go = h("button", { class: "primary", type: "submit" }, "Set the password");
    const form = h("form", { method: "post", action: "#", onsubmit: async (ev) => {
        ev.preventDefault();
        const bad = (text) => { said.className = "bad"; said.textContent = text; };
        if (!code.value.trim()) return bad("Give the set-up code from the container's log.");
        if (pw1.value.length < MIN_PASSWORD_LENGTH) return bad(`The password needs at least ${MIN_PASSWORD_LENGTH} characters.`);
        if (pw1.value !== pw2.value) return bad("The two passwords are not the same.");
        go.disabled = true;
        said.className = "muted";
        said.textContent = "Setting the password…";
        const proof = await deriveLoginProof(pw1.value);
        pw1.value = "";
        pw2.value = "";
        const r = await api("POST", "/broker/setup", { code: code.value.trim(), proof });
        if (r.body.ok) {
            said.textContent = "The password is set. Logging in…";
            const login = await api("POST", "/broker/login", { proof });
            if (login.body.ok) return start();
            return loginScreen("The password is set, but the login failed: " + loginRefusal(login));
        }
        go.disabled = false;
        const e = r.body.error || {};
        if (r.status === 401 || e.kind === "auth") {
            bad("That is not the set-up code in the container's log, or a password is set already (then reload this page to log in).");
        } else if (r.status === 429 || e.kind === "rate_limited") {
            bad(e.retry_after ? `Too many wrong set-up codes. Wait ${e.retry_after} seconds and try again.`
                              : (e.message || "Too many wrong set-up codes. Wait a minute and try again."));
        } else if (e.kind === "validation") {
            bad("The password was not accepted: " + (e.message || "the broker gave no reason."));
        } else if (e.kind === "failed") {
            bad("The password could not be stored. The container's log says why.");
        } else {
            bad(errText(r.body));
        }
    } },
    h("p", { class: "muted" }, "No password is set yet. The set-up code was printed in clanker-learn's container log when it first started."),
    formRow("Set-up code", code, "from the container's log"),
    formRow("New password", pw1, `at least ${MIN_PASSWORD_LENGTH} characters`),
    formRow("The password again", pw2),
    said, h("div", { class: "btns" }, go));
    screen("Set the first password", [form]);
    code.focus();
}

async function logout() {
    await api("POST", "/broker/logout");
    loggedOut("You are logged out.", true);
}

// The login ended (a logout, or a 401 on the way): every terminal and frame goes.
function loggedOut(message, plain) {
    if (!S.loggedIn) return;
    loginScreen(message, plain);
}

// ---- the app: rail, tab strip, panes ------------------------------------------------------

function enterApp(projects) {
    teardownApp();
    S.loggedIn = true;
    S.projects = Array.isArray(projects) ? projects : [];
    const rail = h("div", { class: "rail-list" });
    const tabbar = h("div", { class: "tabbar" });
    const panes = h("div", { class: "panes" });
    const empty = h("div", { class: "pane page" });
    panes.append(empty);
    document.getElementById("app").replaceChildren(h("div", { class: "shell" },
        h("nav", { class: "rail" },
            h("div", { class: "rail-head" }, h("span", {}, "PROJECTS")),
            rail,
            h("div", { class: "rail-foot" },
                h("button", { class: "foot", onclick: newProjectDialog }, "+ New project"),
                h("button", { class: "foot", onclick: logout }, "Log out"))),
        h("div", { class: "main" }, tabbar, panes)));
    S.shell = { rail, tabbar, panes, empty };
    const layout = loadLayout();
    if (layout.project && projectRow(layout.project)) {
        S.current = layout.project;
        if (layout.tab) S.tabs[layout.project] = layout.tab;
    } else if (S.projects.length) {
        S.current = S.projects[0].name;
    }
    S.timers.push(setInterval(refreshProjects, PROJECTS_EVERY_MS));
    S.timers.push(setInterval(() => { if (S.current) refreshServices(S.current); }, SERVICES_EVERY_MS));
    window.addEventListener("resize", refitVisible);
    refreshProjects();
    if (S.current) refreshServices(S.current);
    render();
}

// Everything of a login goes: timers, terminals (their sockets closed), frames, dialogs.
function teardownApp() {
    S.timers.forEach(clearInterval);
    S.timers = [];
    for (const key of Object.keys(S.panes)) dropPane(key);
    closeModal();
    window.removeEventListener("resize", refitVisible);
    Object.assign(S, { loggedIn: false, projects: [], ops: [], current: null, creating: null, tabs: {}, services: {}, panes: {}, shell: null, listError: null });
}

async function refreshProjects() {
    if (!S.loggedIn || refreshProjects.busy) return;
    refreshProjects.busy = true;
    try {
        const [list, ops] = await Promise.all([api("GET", "/broker/projects"), api("GET", "/broker/lifecycle")]);
        if (!S.loggedIn) return;
        if (list.body.ok && Array.isArray(list.body.result)) {
            S.projects = list.body.result;
            S.listError = null;
        } else {
            S.listError = errText(list.body);
        }
        if (ops.body.ok) S.ops = (ops.body.result && Array.isArray(ops.body.result.ops)) ? ops.body.result.ops : [];
        // A project the broker no longer lists loses its panes.
        for (const key of Object.keys(S.panes)) {
            if (!projectRow(key.split("|")[0])) dropPane(key);
        }
        if (S.current && S.current !== S.creating && !projectRow(S.current) && list.body.ok) {
            S.current = S.projects.length ? S.projects[0].name : null;
            if (S.current) refreshServices(S.current);
        }
        render();
    } finally {
        refreshProjects.busy = false;
    }
}

async function refreshServices(name) {
    if (!S.loggedIn) return;
    const r = await api("GET", projectPath(name, "/services"));
    if (!S.loggedIn || !r.body.ok || typeof r.body.services !== "object") return;
    S.services[name] = r.body.services;
    if (name === S.current) render();
}

// What the broker says of a project, in words; and whether its sandbox runs.
const OP_WORDS = { create: "being made", start: "starting", stop: "stopping", recreate: "being recreated", destroy: "being destroyed" };

function stateWords(p) {
    const op = opOf(p.name);
    if (op) return { text: OP_WORDS[op.verb] || op.verb, cls: "busy" };
    if (p.wanted === "destroying") return { text: "being destroyed", cls: "busy" };
    switch (p.state) {
        case "running": {
            const setup = typeof p.setup === "string" && p.setup && p.setup !== "done" && p.setup !== "none" ? `, setup ${p.setup}` : "";
            return { text: "running" + setup, cls: "up" };
        }
        case "stopped": return { text: "stopped", cls: "" };
        case "missing": return { text: "no sandbox", cls: "warn" };
        default: return { text: p.state || "unknown", cls: "bad" };
    }
}

const isRunning = (name) => { const p = projectRow(name); return !!p && p.state === "running" && !opOf(name); };

function render() {
    if (!S.shell) return;
    renderRail();
    renderTabbar();
    showPane();
}

function renderRail() {
    const rows = S.projects.map((p) => {
        const st = stateWords(p);
        const op = opOf(p.name);
        return h("div", { class: "proj" + (p.name === S.current ? " sel" : "") },
            h("div", { class: "proj-row", onclick: () => selectProject(p.name) },
                h("span", { class: `dot ${st.cls}` }),
                h("span", { class: "proj-name" }, p.name,
                    h("span", { class: "proj-prog" }, st.text, op ? " · " : null,
                        op ? h("button", { class: "link", title: "Show the operation's progress",
                                           onclick: (ev) => { ev.stopPropagation(); followOp(op.op_id, `${p.name}: ${OP_WORDS[op.verb] || op.verb}`); } }, "log") : null)),
                h("button", { class: "gear", title: "Start, stop, limits, destroy",
                              onclick: (ev) => { ev.stopPropagation(); projectDialog(p.name); } }, "⚙")));
    });
    if (!rows.length) rows.push(h("div", { class: "none" }, "No project yet."));
    if (S.listError) rows.push(h("div", { class: "none bad" }, S.listError));
    S.shell.rail.replaceChildren(...rows);
}

function selectProject(name) {
    if (S.current === name) return;
    S.current = name;
    saveLayout();
    refreshServices(name);
    render();
}

// The tabs of a project: the Shell, the service rows in the broker's order, a tab whose app
// went away while its frame is kept, and the raw TCP rows (an address, not a tab).
function tabsOf(name) {
    const rows = S.services[name] || {};
    const tabs = [{ id: "shell", label: "Shell", type: "cli" }];
    const raw = [];
    for (const [id, row] of Object.entries(rows)) {
        if (!row || typeof row !== "object") continue;
        if (row.renderer === "launcher") { raw.push({ id, row }); continue; }
        if (row.renderer !== "iframe" || !Number.isInteger(row.default_port)) continue;
        // The files server serves single files (for what a lesson shows), not folders: it
        // gets no tab. The workspace is browsed in the Editor.
        if (id === "files") continue;
        const label = id === "editor" ? "Editor" : String(row.label || row.default_port);
        tabs.push({ id, label, type: id === "editor" ? "editor" : "other", row });
    }
    for (const [key, pane] of Object.entries(S.panes)) {
        const [project, id] = key.split("|");
        if (project === name && pane.kind === "web" && !rows[id]) tabs.push({ id, label: pane.label, type: "other", gone: true });
    }
    return { tabs, raw };
}

function renderTabbar() {
    const bar = S.shell.tabbar;
    const name = S.current;
    if (!name) { bar.replaceChildren(h("div", { class: "tb projchip" }, "clanker-learn")); return; }
    const { tabs, raw } = tabsOf(name);
    const shown = tabOf(name);
    // A tab that is not there any more falls back to the Shell, once the list is known.
    if (S.services[name] && shown !== "ports" && !tabs.some((t) => t.id === shown)) S.tabs[name] = "shell";
    const items = [h("div", { class: "tb projchip" }, name)];
    for (const t of tabs) {
        const pane = S.panes[paneKey(name, t.id)];
        const url = pane && pane.url;
        items.push(h("div", { class: `tb t-${t.type}` + (tabOf(name) === t.id ? " on" : "") + (t.gone ? " gone" : ""),
                              title: t.gone ? "This app no longer listens in the sandbox" : (t.row ? `port ${t.row.default_port} of the sandbox` : "a terminal in the sandbox"),
                              onclick: () => showTab(name, t.id) },
            t.label,
            t.type !== "cli" && url ? h("a", { class: "newtab", href: url, target: "_blank", rel: "noopener noreferrer",
                                                title: "Open in a new browser tab", onclick: (ev) => ev.stopPropagation() }, "↗") : null));
    }
    for (const { row } of raw) {
        const host = location.hostname;
        items.push(h("div", { class: "tb raw", title: `Raw TCP to port ${row.sandbox_port} of the sandbox. It has no login in front of it.` },
            h("span", { class: "rawlabel" }, String(row.label)), h("code", {}, `${host}:${row.passthrough_port}`)));
    }
    items.push(h("div", { class: "spacer" }));
    items.push(h("div", { class: "tb t-ports" + (tabOf(name) === "ports" ? " on" : ""), onclick: () => showTab(name, "ports") }, "Ports"));
    bar.replaceChildren(...items);
}

function showTab(name, id) {
    const had = !!S.panes[paneKey(name, id)];
    S.tabs[name] = id;
    saveLayout();
    render();
    if (id === "ports" && had) refreshPorts(name);   // a new Ports pane asks by itself
}

// The visible pane: made on first sight, kept (hidden) when another is shown.
function showPane() {
    const { panes, empty } = S.shell;
    const name = S.current;
    let visible = empty;
    if (!name) {
        empty.replaceChildren(h("div", { class: "page-inner" }, h("h1", {}, "No project shown"),
            h("p", { class: "sub" }, S.projects.length ? "Pick a project on the left." : "Make one with “+ New project” on the left.")));
    } else {
        const id = tabOf(name);
        const key = paneKey(name, id);
        let pane = S.panes[key];
        if (!pane) {
            if (id === "shell") pane = makeShellPane(name);
            else if (id === "ports") pane = makePortsPane(name);
            else {
                const row = (S.services[name] || {})[id];
                if (row) pane = makeWebPane(name, id, row);
            }
            if (pane) {
                S.panes[key] = pane;
                panes.append(pane.el);
                if (pane.kind === "ports") refreshPorts(name);
            }
        }
        if (pane) {
            visible = pane.el;
            if (pane.update) pane.update();
        } else {
            empty.replaceChildren(h("div", { class: "none" }, "Asking the sandbox for its tabs…"));
        }
    }
    for (const child of panes.children) child.hidden = child !== visible;
    refitVisible();
}

function dropPane(key) {
    const pane = S.panes[key];
    if (!pane) return;
    if (pane.close) pane.close();
    pane.el.remove();
    delete S.panes[key];
}

// ---- the Shell: RS's terminal -------------------------------------------------------------

// RS's dark terminal palette.
const XTERM_THEME = {
    background: "#000000", foreground: "#e0e0e0",
    cursor: "#e0e0e0", cursorAccent: "#000000",
    selectionBackground: "rgba(255,255,255,0.25)",
    black: "#000000", red: "#cc0403", green: "#19cb00", yellow: "#cecb00",
    blue: "#0d73cc", magenta: "#cb1ed1", cyan: "#0dcdcd", white: "#dddddd",
    brightBlack: "#767676", brightRed: "#f2201f", brightGreen: "#23fd00", brightYellow: "#fffd00",
    brightBlue: "#1a8fff", brightMagenta: "#fd28ff", brightCyan: "#14ffff", brightWhite: "#ffffff",
};

function makeShellPane(name) {
    const said = h("span", { class: "grow" });
    const again = h("button", { onclick: () => connect() }, "Reconnect");
    const startBtn = h("button", { class: "primary", onclick: () => runAction(name, "start") }, "Start the sandbox");
    const bar = h("div", { class: "appbar termbar" }, h("span", { class: "tdot" }), said, startBtn, again);
    const pad = h("div", { class: "term-pad" });
    const area = h("div", { class: "term-area" }, pad);
    const el = h("div", { class: "pane shell-pane" }, bar, area);
    const pane = { kind: "shell", el, term: null, fit: null, ws: null, state: "idle" };

    const set = (state, text) => {
        pane.state = state;
        bar.className = `appbar termbar s-${state}`;
        said.textContent = text;
        again.hidden = !(state === "closed" || state === "failed") || !isRunning(name);
        startBtn.hidden = !(state === "idle" || state === "closed" || state === "failed") || isRunning(name) || !!opOf(name) || S.creating === name;
    };

    function connect() {
        if (!isRunning(name)) return;
        if (pane.ws) { pane.ws.onclose = null; try { pane.ws.close(); } catch (_) {} }
        if (!pane.term) openTerminal(pane, pad);
        else pane.term.reset();
        set("connecting", "Connecting to the sandbox…");
        pane.ws = openSocket(name, pane, set);
    }

    pane.update = () => {
        if (pane.state === "closed" || pane.state === "failed") {
            // The sandbox went down (a stop, a recreate) and is back: connect again by itself.
            if (!isRunning(name)) pane.wasDown = true;
            else if (pane.wasDown) { pane.wasDown = false; return connect(); }
            set(pane.state, said.textContent);
            return;
        }
        if (pane.ws) return;
        if (isRunning(name)) return connect();
        const op = opOf(name);
        const p = projectRow(name);
        set("idle", op ? `The sandbox is ${OP_WORDS[op.verb] || op.verb}.`
                       : S.creating === name ? "The sandbox is being made."
                       : p && p.state === "missing" ? "This project has no sandbox. Start it to make one."
                       : "The sandbox is not running.");
    };
    pane.refit = () => { if (pane.fit) { try { pane.fit.fit(); } catch (_) {} } };
    pane.close = () => {
        if (pane.ws) { pane.ws.onclose = null; try { pane.ws.close(); } catch (_) {} }
        if (pane.term) pane.term.dispose();
        pane.ws = pane.term = null;
    };
    return pane;
}

// RS's openSshTerminal, without its search, links, WebGL, touch and key-bar parts.
function openTerminal(pane, pad) {
    const term = new Terminal({
        cursorBlink: true,
        fontFamily: getComputedStyle(document.documentElement).getPropertyValue("--mono").trim() || "monospace",
        fontSize: 14,
        theme: XTERM_THEME,
        scrollback: 5000,
    });
    const fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(pad);
    term.onSelectionChange(() => {
        const sel = term.getSelection();
        if (sel && navigator.clipboard) navigator.clipboard.writeText(sel).catch(() => {});
    });
    // OSC 52: tmux emits this after a copy, which lets a copy in tmux reach the clipboard.
    term.parser.registerOscHandler(52, (data) => {
        const semi = data.indexOf(";");
        if (semi < 0) return false;
        const payload = data.slice(semi + 1);
        if (payload === "?") return true; // a query: ignored, the page's clipboard is not read out
        try {
            const text = atob(payload);
            if (text && navigator.clipboard) navigator.clipboard.writeText(text).catch(() => {});
            return true;
        } catch (_) {
            return false;
        }
    });
    term.onData((d) => {
        const ws = pane.ws;
        if (ws && ws.readyState === WebSocket.OPEN) ws.send(new TextEncoder().encode(d));
    });
    term.onResize(({ rows, cols }) => {
        const ws = pane.ws;
        if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "resize", rows, cols }));
    });
    pane.term = term;
    pane.fit = fit;
    try { fit.fit(); } catch (_) {}
    term.focus();
}

// The terminal's WebSocket. The first frame says the size only (RS also sends the target and
// its credentials; here the server takes them from the broker).
function openSocket(name, pane, set) {
    const ws = new WebSocket(`wss://${location.host}${projectPath(name, "/terminal")}`);
    ws.binaryType = "arraybuffer";
    let told = false;   // a control message already said why it ends
    ws.onopen = () => {
        ws.send(JSON.stringify({ type: "connect", rows: pane.term.rows, cols: pane.term.cols }));
    };
    ws.onmessage = (ev) => {
        if (typeof ev.data === "string") {
            let ctrl;
            try { ctrl = JSON.parse(ev.data); } catch (_) { return; }
            if (ctrl && typeof ctrl === "object") told = handleControl(pane, ctrl, set) || told;
        } else {
            pane.term.write(new Uint8Array(ev.data));
        }
    };
    ws.onclose = (ev) => {
        if (pane.ws !== ws) return;
        pane.ws = null;
        pane.term.writeln("\r\n\x1b[90m[disconnected]\x1b[0m");
        if (!told) {
            set("closed", pane.state === "connected"
                ? "The connection to the sandbox ended."
                : `The connection was refused or dropped before the sandbox answered (${ev.code}).`);
            // A refusal may be the end of the login: asking the list finds out.
            api("GET", "/broker/projects");
        }
    };
    return ws;
}

// RS's handleControl, for the messages the server's _terminal speaks. Returns true when the
// message said why the terminal ends.
function handleControl(pane, ctrl, set) {
    const term = pane.term;
    if (ctrl.type === "connected") {
        term.writeln("\r\n\x1b[90m[connected]\x1b[0m");
        set("connected", "Connected to the sandbox.");
        pane.refit();
        return false;
    }
    if (ctrl.type === "fingerprint_mismatch") {
        term.writeln("\r\n\x1b[31m[host key mismatch: connection rejected]\x1b[0m");
        set("failed", "The sandbox answered with a host key other than the one recorded for it, so nothing was sent to it. If you have just recreated the project, reconnect.");
        return true;
    }
    if (ctrl.type === "auth_failed") {
        term.writeln("\r\n\x1b[31m[the sandbox refused the login]\x1b[0m");
        set("failed", "The sandbox refused the Shell's login.");
        return true;
    }
    if (ctrl.type === "error") {
        const msg = typeof ctrl.msg === "string" ? ctrl.msg : "refused";
        term.writeln(`\r\n\x1b[31m[error: ${msg.replace(/[\x00-\x1f\x7f]/g, " ")}]\x1b[0m`);
        set("failed", `The terminal could not be opened: ${msg}`);
        return true;
    }
    return false;
}

function refitVisible() {
    if (!S.shell || !S.current) return;
    const pane = S.panes[paneKey(S.current, tabOf(S.current))];
    if (pane && pane.refit) requestAnimationFrame(() => pane.refit());
}

// ---- a web tab: the editor, an app -----------------------------------------------------------

// The address the browser used, as it goes into a URL (an IPv6 address keeps its brackets).
const appUrl = (originPort) => `https://${location.hostname}:${originPort}/`;

function makeWebPane(name, id, row) {
    const label = id === "editor" ? "Editor" : String(row.label || row.default_port);
    const said = h("span", { class: "grow" }, "Opening…");
    const link = h("a", { class: "newtab", target: "_blank", rel: "noopener noreferrer", hidden: true }, "open in a new tab ↗");
    const reload = h("button", { hidden: true, title: "Load the frame again" }, "Reload");
    const retry = h("button", { hidden: true }, "Try again");
    const bar = h("div", { class: "appbar" }, h("span", { class: "applabel" }, label), said, link, reload, retry);
    const goneNote = h("div", { class: "gone-note", hidden: true },
        h("span", {}, "This app no longer listens in the sandbox. Its frame is kept until you close it."),
        h("button", { onclick: () => { dropPane(paneKey(name, id)); S.tabs[name] = "shell"; render(); } }, "Close the tab"));
    const body = h("div", { class: "frame-area" },
        h("div", { class: "frame-hint" }, "If the frame stays empty, the browser has not accepted this address's certificate yet: open it in a new tab once, accept it there, then reload the frame."));
    const el = h("div", { class: "pane web-pane" }, bar, goneNote, body);
    const pane = { kind: "web", el, label, url: null, iframe: null };

    async function open() {
        retry.hidden = true;
        said.textContent = "Opening…";
        said.className = "grow";
        const fail = (text) => { said.textContent = text; said.className = "grow bad"; retry.hidden = false; };
        const session = await api("POST", projectPath(name, "/session"));
        if (!session.body.ok) return fail("Could not open: " + errText(session.body));
        const origin = await api("POST", projectPath(name, "/origin"), { port: row.default_port });
        if (!origin.body.ok) return fail("Could not open: " + errText(origin.body));
        const port = origin.body.origin_port;
        if (!Number.isInteger(port) || port < 1 || port > 65535) return fail("The web server named no address for this app.");
        if (S.panes[paneKey(name, id)] !== pane) return;   // closed meanwhile
        pane.url = appUrl(port);
        link.href = pane.url;
        link.hidden = false;
        reload.hidden = false;
        said.textContent = `port ${row.default_port} of the sandbox, at ${pane.url}`;
        // RS's frame: the least the apps need (code-server: scripts, its own origin for
        // cookies, forms, popups, modals, downloads), no top navigation.
        pane.iframe = h("iframe", {
            src: pane.url,
            title: label,
            sandbox: "allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox allow-modals allow-downloads",
            allow: "fullscreen; clipboard-read; clipboard-write",
        });
        body.append(pane.iframe);
        renderTabbar();
    }
    reload.addEventListener("click", () => { if (pane.iframe) pane.iframe.src = pane.url; });
    retry.addEventListener("click", open);
    pane.update = () => { goneNote.hidden = !!(S.services[name] || {})[id]; };
    pane.close = () => { if (pane.iframe) pane.iframe.remove(); pane.iframe = null; };
    open();
    return pane;
}

// ---- the ports named by hand ---------------------------------------------------------------

function makePortsPane(name) {
    const list = h("div", { class: "card-body" }, h("div", { class: "none" }, "Asking…"));
    const port = h("input", { type: "number", min: 1, max: 65535, required: true });
    const label = h("input", { type: "text", maxlength: 64, required: true, placeholder: "notebook" });
    const kindWeb = h("input", { type: "radio", name: `kind-${name}`, value: "web", checked: true });
    const kindRaw = h("input", { type: "radio", name: `kind-${name}`, value: "raw" });
    const hostPort = h("input", { type: "number", min: 1, max: 65535, placeholder: "the lowest free one" });
    const hostRow = formRow("Port on this machine", hostPort, "leave empty for the lowest free one of the pass-through range");
    const rawWarn = h("p", { class: "warnbox" }, "A raw TCP port has no login in front of it: whoever can reach its address reaches the app, and the app's own login is the only lock.");
    const said = h("p", { class: "muted" });
    const showKind = () => { hostRow.hidden = rawWarn.hidden = !kindRaw.checked; };
    kindWeb.addEventListener("change", showKind);
    kindRaw.addEventListener("change", showKind);
    const add = h("button", { class: "primary", type: "submit" }, "Add");
    const form = h("form", { class: "form", method: "post", action: "#", onsubmit: async (ev) => {
        ev.preventDefault();
        const body = { port: Number(port.value), label: label.value.trim() };
        if (kindRaw.checked) {
            body.passthrough = true;
            if (hostPort.value) body.host_port = Number(hostPort.value);
        }
        add.disabled = true;
        const r = await api("POST", projectPath(name, "/ports"), body);
        add.disabled = false;
        if (!r.body.ok) { said.className = "bad"; said.textContent = errText(r.body); return; }
        said.className = "muted";
        said.textContent = kindRaw.checked ? `Added: raw TCP at ${location.hostname}:${r.body.result.port}.` : "Added.";
        port.value = label.value = hostPort.value = "";
        refreshPorts(name);
        refreshServices(name);
    } },
    formRow("Port in the sandbox", port),
    formRow("Label", label),
    h("div", { class: "field" }, h("span", {}, "Kind"),
        h("div", { class: "radios" }, h("label", {}, kindWeb, " web app (a tab, behind your login)"),
                                     h("label", {}, kindRaw, " raw TCP (an address, no login)"))),
    hostRow, rawWarn, said, h("div", { class: "btns" }, add));
    showKind();
    const el = h("div", { class: "pane page" }, h("div", { class: "page-inner narrow" },
        h("h1", {}, `Ports of ${name}`),
        h("p", { class: "sub" }, "Web apps the sandbox serves are found while they listen. Name a port here when one is not found, or to reach a port as raw TCP from this machine."),
        h("div", { class: "card" }, h("div", { class: "card-head" }, "Named by hand"), list),
        h("div", { class: "card" }, h("div", { class: "card-head" }, "Name a port"), form)));
    return { kind: "ports", el, list };
}

async function refreshPorts(name) {
    const pane = S.panes[paneKey(name, "ports")];
    if (!pane) return;
    const r = await api("GET", projectPath(name, "/ports"));
    if (!r.body.ok) { pane.list.replaceChildren(h("div", { class: "none bad" }, errText(r.body))); return; }
    const entries = (r.body.result && Array.isArray(r.body.result.ports)) ? r.body.result.ports : [];
    if (!entries.length) { pane.list.replaceChildren(h("div", { class: "none" }, "None.")); return; }
    pane.list.replaceChildren(...entries.map((e) => {
        const raw = e.passthrough === true;
        return h("div", { class: "rowline portrow" },
            h("span", { class: "plabel" }, String(e.label)),
            h("span", { class: "muted" }, `port ${e.port}`),
            raw ? h("span", { class: "kindp k-warn", title: "No login in front of it" }, "raw TCP, no login")
                : h("span", { class: "kindp" }, "web app"),
            raw ? h("code", {}, `${location.hostname}:${e.host_port}`) : h("span", {}),
            h("button", { onclick: async () => {
                const rm = await api("POST", projectPath(name, "/ports/remove"), raw ? { port: e.host_port, passthrough: true } : { port: e.port });
                if (!rm.body.ok) toast(errText(rm.body), true);
                refreshPorts(name);
                refreshServices(name);
            } }, "Remove"));
    }));
}

// ---- dialogs: a new project, a project's actions, an operation's progress -------------------

let modal = null;

function openModal(title, body, buttons) {
    closeModal();
    const box = h("div", { class: "modal", role: "dialog", "aria-label": title }, h("h2", {}, title), body,
        h("div", { class: "btns" }, buttons));
    const scrim = h("div", { class: "scrim", onclick: (ev) => { if (ev.target === scrim) closeModal(); } }, box);
    document.body.append(scrim);
    modal = scrim;
    return scrim;
}

function closeModal() {
    if (modal) modal.remove();
    modal = null;
}

document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && modal) closeModal(); });

function limitsInputs(current) {
    const fmtMemory = (v) => !Number.isFinite(v) ? "none" : v % 2 ** 30 === 0 ? `${v / 2 ** 30}g` : `${Math.round(v / 2 ** 20)}m`;
    const now = current || {};
    const shown = (k) => now[k] === null || now[k] === undefined ? "none" : k === "memory" ? fmtMemory(now[k]) : String(now[k]);
    const inputs = {
        pids: h("input", { type: "text", placeholder: current ? shown("pids") : "16384", spellcheck: "false" }),
        memory: h("input", { type: "text", placeholder: current ? shown("memory") : "none", spellcheck: "false" }),
        cpus: h("input", { type: "text", placeholder: current ? shown("cpus") : "none", spellcheck: "false" }),
    };
    const rows = [
        formRow("Processes", inputs.pids, "at most this many processes and threads"),
        formRow("Memory", inputs.memory, "as 512m or 2g"),
        formRow("Processors", inputs.cpus, "as 0.5 or 2"),
    ];
    // Only what was typed is sent: empty is unchanged, "none" is no limit.
    const values = () => {
        const out = {};
        for (const [k, input] of Object.entries(inputs)) {
            const v = input.value.trim();
            if (!v) continue;
            out[k] = v.toLowerCase() === "none" ? null : /^\d+(\.\d+)?$/.test(v) ? Number(v) : v;
        }
        return out;
    };
    return { rows, values };
}

function newProjectDialog() {
    const name = h("input", { type: "text", autocomplete: "off", spellcheck: "false", maxlength: 31, placeholder: "rust-ownership" });
    const limits = limitsInputs(null);
    const said = h("p", { class: "muted" }, NAME_RULE);
    const go = h("button", { class: "primary", type: "submit" }, "Create");
    const form = h("form", { method: "post", action: "#", onsubmit: async (ev) => {
        ev.preventDefault();
        go.disabled = true;
        const r = await api("POST", "/broker/project", { name: name.value.trim(), ...limits.values() });
        go.disabled = false;
        if (!r.body.ok) {
            said.className = "bad";
            const e = r.body.error || {};
            // A name the broker refuses is said with the form a name takes.
            said.textContent = e.kind === "validation" && !String(e.message || "").includes("lower-case")
                ? `“${name.value.trim()}” is not a project name. ${NAME_RULE}` : errText(r.body);
            return;
        }
        const project = name.value.trim();
        S.current = S.creating = project;
        saveLayout();
        render();
        refreshProjects();
        await followOp(r.body.op_id, `Creating ${project}`);
        if (S.creating === project) S.creating = null;
    } }, formRow("Name", name), h("details", { class: "more" }, h("summary", {}, "Limits (optional)"), limits.rows), said,
    h("div", { class: "btns" }, h("button", { type: "button", onclick: closeModal }, "Cancel"), go));
    openModal("New project", form, []);
    name.focus();
}

function projectDialog(name) {
    const p = projectRow(name);
    if (!p) return;
    const st = stateWords(p);
    const op = opOf(name);
    const said = h("p", { class: "muted" });
    const action = (verb, label, cls) => h("button", { class: cls || "", disabled: !!op, onclick: () => runAction(name, verb) }, label);
    const limits = limitsInputs(p.limits);
    const limitsGo = h("button", { type: "submit" }, "Change the limits");
    const limitsForm = h("form", { method: "post", action: "#", onsubmit: async (ev) => {
        ev.preventDefault();
        const values = limits.values();
        if (!Object.keys(values).length) { said.className = "muted"; said.textContent = "Type a new value first."; return; }
        limitsGo.disabled = true;
        const r = await api("POST", projectPath(name, "/limits"), values);
        limitsGo.disabled = false;
        said.className = r.body.ok ? "muted" : "bad";
        said.textContent = r.body.ok ? "The limits are changed." : errText(r.body);
        if (r.body.ok) refreshProjects();
    } }, limits.rows, h("div", { class: "btns" }, limitsGo));
    const pw = h("input", { type: "password", autocomplete: "current-password" });
    const destroyGo = h("button", { class: "danger", type: "submit", disabled: !!op }, "Destroy the project");
    const destroyForm = h("form", { method: "post", action: "#", onsubmit: async (ev) => {
        ev.preventDefault();
        if (!pw.value) { said.className = "bad"; said.textContent = "Destroying needs your password again."; return; }
        destroyGo.disabled = true;
        const proof = await deriveLoginProof(pw.value);
        pw.value = "";
        const r = await api("POST", projectPath(name, "/destroy"), { proof });
        destroyGo.disabled = false;
        if (!r.body.ok) { said.className = "bad"; said.textContent = errText(r.body); return; }
        followOp(r.body.op_id, `Destroying ${name}`);
    } }, h("p", { class: "note" }, "The sandbox, its workspace and its ports go, for good. Type your password to confirm."),
        formRow("Password", pw), h("div", { class: "btns" }, destroyGo));
    const running = p.state === "running";
    openModal(name, [
        h("div", { class: "kv" }, h("span", { class: "k" }, "Sandbox"),
            h("span", {}, h("span", { class: `dot inline ${st.cls}` }), " ", st.text, " ",
                op ? h("button", { class: "link", onclick: () => followOp(op.op_id, `${name}: ${OP_WORDS[op.verb] || op.verb}`) }, "show the log") : null)),
        h("div", { class: "kv" }, h("span", { class: "k" }, ""),
            h("span", { class: "row-btns" }, running ? action("stop", "Stop") : action("start", "Start", "primary"),
                action("recreate", "Recreate"))),
        h("p", { class: "note" }, "Recreate makes a new sandbox from the current image, with the same workspace and home folder; its setup recipe runs again."),
        p.problem ? h("p", { class: "bad" }, String(p.problem)) : null,
        h("details", { class: "more" }, h("summary", {}, "Limits"), limitsForm),
        h("details", { class: "more danger-zone" }, h("summary", {}, "Destroy"), destroyForm),
        said,
    ], [h("button", { onclick: closeModal }, "Close")]);
}

async function runAction(name, verb) {
    const r = await api("POST", projectPath(name, `/${verb}`));
    if (!r.body.ok) { toast(`${name}: ${errText(r.body)}`, true); refreshProjects(); return; }
    refreshProjects();
    await followOp(r.body.op_id, `${name}: ${OP_WORDS[verb] || verb}`);
    // A Shell the stop or recreate closed connects again once the sandbox runs (see its update).
    const shell = S.panes[paneKey(name, "shell")];
    if (shell && (shell.state === "closed" || shell.state === "failed")) { shell.wasDown = true; shell.update(); }
}

// Follow an operation: its log as it grows, then its result. The dialog can be closed; the
// operation goes on in the broker, and the rail says so.
async function followOp(opId, title) {
    const state = h("p", { class: "opstate" }, "Running…");
    const log = h("pre", { class: "oplog" });
    const scrim = openModal(title, [state, log], [h("button", { onclick: closeModal }, "Close")]);
    const add = (text) => {
        for (const line of text.split("\n")) {
            if (!line.trim()) continue;
            let rec;
            try { rec = JSON.parse(line); } catch (_) { rec = { step: "", msg: line }; }
            const when = Number.isFinite(rec.ts) ? new Date(rec.ts * 1000).toLocaleTimeString() + "  " : "";
            const words = [rec.step, rec.msg].filter((x) => typeof x === "string" && x).join(": ");
            log.append(h("div", { class: rec.status === "failed" ? "bad" : "" }, when + (words || String(rec.status || ""))));
        }
        log.scrollTop = log.scrollHeight;
    };
    let from = 0, unknown = 0, result = null;
    const readLog = async () => {
        const r = await api("GET", `/broker/op/${enc(opId)}/log?from=${from}`);
        if (r.body.ok) { if (r.body.data) add(r.body.data); from = r.body.next; }
    };
    while (S.loggedIn) {
        await readLog();
        const st = await api("GET", `/broker/op/${enc(opId)}`);
        if (st.body.ok && st.body.result) {
            if (st.body.result.state === "done" || st.body.result.state === "failed") { result = st.body.result; break; }
        } else if (++unknown > 10) {
            result = { state: "failed", msg: errText(st.body) };
            break;
        }
        await sleep(OP_EVERY_MS);
    }
    if (!S.loggedIn) return;
    await readLog();
    const ok = result.state === "done";
    const msg = typeof result.msg === "string" && result.msg ? `: ${result.msg}` : "";
    state.textContent = (ok ? "Done" : "Failed") + msg;
    state.className = "opstate " + (ok ? "good" : "bad");
    if (!scrim.isConnected) toast(`${title}: ${ok ? "done" : "failed"}${msg}`, !ok);
    await refreshProjects();
    if (S.current) refreshServices(S.current);
}

// ---- go ------------------------------------------------------------------------------------

window.addEventListener("DOMContentLoaded", start);
