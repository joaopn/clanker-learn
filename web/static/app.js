// clanker-learn's page: the login, the projects, a Shell, an Editor and the web apps of a
// project's sandbox. Written new after the accepted mock; two parts are RS's (webui/static/
// app.js at RS commit 62788a8): the login proof (deriveLoginProof) and the terminal
// (openTerminal, handleControl). Settings -> Software, the New project box's Agents and the
// Config box's Agents are RS's (renderSoftwareScreen, mgmtRefreshDialog, agentsBoxContent,
// mgmtAgentsUpdateDialog) adapted to this page's helpers. The lessons and their conversation
// follow the mock's lesson view; three pieces are ported from gaggiclanker's web client (said at
// their place: the stream client, the context line's wording, the Markdown rules). The tabs side
// by side (columns, splitters, tabs dragged between them) are RS's column layout, lifted (see
// "tabs side by side" below). The interface settings (Settings -> Interface: themes, editor zoom,
// CLI width and font size) and the sidebar's drag to resize are RS's too (see "the interface
// settings" below). Plain script, no framework, no build step.
//
// Every string that comes from the server or a sandbox goes into the page as text: the
// page is built with h() below, which makes text nodes, and never with innerHTML. The tutor's
// Markdown is parsed by markdown-it into tokens, and the page builds the elements from them
// (mdRender): markdown-it's own HTML output is never used.
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
const BUILD_ALL_EVERY_MS = 3000;   // the plan read again while Build all runs and its card is shown
const LAYOUT_KEY = "cl-layout";   // browser storage: which project, tabs, columns and lessons were shown, nothing else
const LESSON_ID_RE = /^[A-Za-z0-9][A-Za-z0-9-]{0,63}$/;   // what the tutor service makes ids of

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
    for (const kid of kids.flat(Infinity)) {
        if (kid === null || kid === undefined || kid === false) continue;
        e.append(kid instanceof Node ? kid : String(kid));
    }
    return e;
}

const enc = encodeURIComponent;
// replaceChildren without the null and false entries (which it would write as text).
const fill = (el, ...kids) => el.replaceChildren(...kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false));
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
    const none = { project: null, tab: null, page: null, kbView: null, lessons: {}, columns: {}, expanded: [] };
    try {
        const v = JSON.parse(localStorage.getItem(LAYOUT_KEY) || "{}");
        const lessons = {};
        if (v.lessons && typeof v.lessons === "object") {
            // null: the project's lesson tab was closed (Goals is not opened by itself again).
            for (const [k, id] of Object.entries(v.lessons)) if (id === null || (typeof id === "string" && LESSON_ID_RE.test(id))) lessons[k] = id;
        }
        // Per project: its columns of tab ids, their widths, the tabs last shown in them and the
        // focused one. Only ids the project has are ever used (reconcileTabLayout).
        const columns = {};
        const strs = (a) => (Array.isArray(a) ? a.filter((x) => typeof x === "string" && x.length <= 200) : []);
        if (v.columns && typeof v.columns === "object" && !Array.isArray(v.columns)) {
            for (const [p, c] of Object.entries(v.columns)) {
                if (!c || typeof c !== "object") continue;
                columns[p] = { columns: (Array.isArray(c.columns) ? c.columns : []).filter(Array.isArray).map(strs),
                               ratios: Array.isArray(c.ratios) ? c.ratios.filter((r) => typeof r === "number" && Number.isFinite(r) && r > 0) : undefined,
                               active: strs(c.active).slice(-32),
                               focus: typeof c.focus === "string" ? c.focus : undefined };
            }
        }
        return { project: typeof v.project === "string" ? v.project : null,
                 tab: typeof v.tab === "string" ? v.tab : null,
                 page: v.page === "settings" || v.page === "knowledge" ? v.page : null,
                 kbView: typeof v.kbView === "string" && v.kbView.length <= 300 ? v.kbView : null,
                 lessons, columns,
                 expanded: Array.isArray(v.expanded) ? v.expanded.filter((x) => typeof x === "string") : [] };
    } catch (_) {
        return none;
    }
}

function saveLayout() {
    try {
        localStorage.setItem(LAYOUT_KEY, JSON.stringify({ project: S.current, tab: S.current ? tabOf(S.current) : null, page: S.page, kbView: S.kb.view,
                                                         lessons: S.lessonOf, expanded: [...S.expanded], columns: S.cols }));
    } catch (_) { /* no storage: the page works without it */ }
}

// One request to the web server: {status, body}. status 0 is "no answer". A 401 on a route
// that needs the login ends the page's login.
async function api(method, path, body) {
    const opts = { method, credentials: "same-origin", cache: "no-store", headers: {} };
    if (body instanceof Blob) {   // a file, sent as it is (a project file to import)
        opts.headers["Content-Type"] = "application/octet-stream";
        opts.body = body;
    } else if (body !== undefined) {
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

const NAME_RE = /^[a-z0-9]([a-z0-9-]{0,29}[a-z0-9])?$/;
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
    page: null,            // "settings" or "knowledge" while that page is shown in place of a project
    kb: { view: "brief", raw: false, showRetired: false, showArchived: false, q: "", seq: 0, changes: null, next: null, navData: null },   // the Knowledge page
    kbOpen: 0,             // global insights proposed by a tutor that wait for the learner (the dot)
    software: null,        // Settings -> Software: the broker's rows, as last read
    softwareError: null,
    builds: {},            // a build in flight: target ("agent:claude" | "editor") -> {opId, verb}
    checking: new Set(),   // targets whose Refresh is asking upstream
    models: null,          // the catalog of Claude's tiers (/broker/models), once read
    expanded: new Set(),   // projects shown open into their lessons in the list
    railAsk: null,         // {name, verb}: the row drawn as the question of its Start/Stop button
    lessons: {},           // project -> {list: [lesson], error: sentence or null}, as last read
    lessonOf: {},          // project -> the id of its lesson tab (one at a time), null once closed
    activity: new Set(),   // ids of the lessons whose tutor is working (/broker/tutor/activity)
    tutorError: null,      // why the tutor service could not be asked, or null
    tutorSettings: null,   // Settings -> Tutor: {claude_token_set}, as last read
    tutorSettingsError: null,
    filesOrigins: new Map(), // project -> the promise of its files server's address (what a lesson shows)
    editorFile: {},        // project -> {path, line}: the file its Editor is to open next
    cols: {},              // project -> {columns, ratios, active, focus}: its tabs side by side, as kept
    tabLayout: null,       // the shown project's reconciled {project, columns, ratios} (RS's state.tabLayout)
    dragging: false,       // a tab or a splitter is being dragged: the strip and the panes are not rebuilt
    renderPending: false,  // a render was asked for during the drag
    wasNarrow: false,      // the window was at phone width at the last render
    mode: null,            // the way clanker-learn runs, "sysbox" or "privileged" (/broker/mode), once read
    cert: null,            // the certificate in use, {provider, not_after, state} (/broker/cert), once read
    backups: null,         // Settings -> Backup: the broker's rows, as last read
    backupsError: null,
    backupOp: null,        // the op_id of the backup being made, or null
    ctxWindow: {},         // project -> the context window its tutor last reported (the context line)
    // The interface settings (Settings -> Interface), per browser, set by applyInterfaceSettings
    // before the first render; teardownApp keeps them.
    theme: null,           // THEMES id
    termTheme: null,       // TERM_THEMES id, or null = follow the interface theme
    railWidth: null,       // px
    railPinned: false,     // kept: the sidebar stays beside the page (RS's state.railPinned)
    railExpanded: false,   // in memory: the sidebar is shown (floating over the page while unpinned)
    iframeZoom: null,      // the scale of the Editor and web-app frames
    cliMaxWidth: null,     // px, 0 = full width
    termFontSize: null,    // px
};

const tabOf = (project) => S.tabs[project] || "shell";
const paneKey = (project, tab) => `${project}|${tab}`;
const projectRow = (name) => S.projects.find((p) => p.name === name) || null;
// The projects the sidebar shows: an archived one is listed only in ▤ All projects.
const shownProjects = () => S.projects.filter((p) => !p.archived);
const shownRow = (name) => { const p = projectRow(name); return p && !p.archived ? p : null; };
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
    const settings = makeSettingsPane();
    const knowledge = makeKnowledgePane();
    panes.append(empty, settings.el, knowledge.el);
    const railSplitter = h("div", { class: "rail-splitter", title: "Drag to resize" });
    installRailSplitterDrag(railSplitter);
    document.getElementById("app").replaceChildren(h("div", { class: "shell" },
        h("nav", { class: "rail" },
            railSplitter,
            h("div", { class: "rail-head" }, h("span", {}, "PROJECTS"), makePinButton()),
            rail,
            h("div", { class: "rail-foot" },
                h("button", { class: "foot", onclick: newProjectDialog }, "+ New project"),
                h("button", { class: "foot", onclick: allProjectsDialog }, "▤ All projects"),
                h("button", { class: "foot knowledge-btn", onclick: () => togglePage("knowledge", showKnowledge) }, h("span", {}, "◆ Knowledge"), h("span", { class: "kdot", hidden: true })),
                h("button", { class: "foot settings-btn", onclick: () => togglePage("settings", showSettings) }, "⚙ Settings"),
                h("button", { class: "foot", onclick: logout }, "Log out"))),
        h("div", { class: "main" }, tabbar, panes)));
    S.shell = { rail, tabbar, panes, empty, settings, knowledge };
    applyRailState();
    const layout = loadLayout();
    S.page = layout.page;
    if (layout.kbView) S.kb.view = layout.kbView;
    S.lessonOf = layout.lessons;
    S.cols = layout.columns;
    for (const [p, c] of Object.entries(S.cols)) if (c.focus && p !== layout.project) S.tabs[p] = c.focus;
    S.wasNarrow = narrowScreen();
    S.expanded = new Set(layout.expanded.filter((n) => shownRow(n)));
    if (layout.project && shownRow(layout.project)) {
        S.current = layout.project;
        if (layout.tab) S.tabs[layout.project] = layout.tab;
        const lid = lessonTabId(layout.tab);
        if (lid) S.lessonOf[layout.project] = lid;
    } else if (shownProjects().length) {
        S.current = shownProjects()[0].name;
    }
    if (S.current) S.expanded.add(S.current);
    S.timers.push(setInterval(refreshProjects, PROJECTS_EVERY_MS));
    S.timers.push(setInterval(() => { if (S.current) refreshServices(S.current); }, SERVICES_EVERY_MS));
    S.timers.push(setInterval(() => { if (S.page === "settings") { refreshSoftware(); refreshBackups(); } }, SERVICES_EVERY_MS));
    window.addEventListener("resize", onWindowResize);
    refreshProjects();
    if (S.current) refreshServices(S.current);
    if (S.page === "settings") { refreshSoftware(); refreshTutorSettings(); refreshBackups(); }
    if (S.page === "knowledge") renderKnowledge();
    seedBuilds();
    seedBackup();
    refreshMode();
    // The default agent, named in each lesson's choice of agent.
    if (S.page !== "settings") refreshTutorSettings();
    render();
}

// Everything of a login goes: timers, terminals (their sockets closed), frames, dialogs.
function teardownApp() {
    S.timers.forEach(clearInterval);
    S.timers = [];
    for (const key of Object.keys(S.panes)) dropPane(key);
    closeModal();
    window.removeEventListener("resize", onWindowResize);
    Object.assign(S, { loggedIn: false, projects: [], ops: [], current: null, creating: null, tabs: {}, services: {}, panes: {}, shell: null, listError: null,
                       page: null, software: null, softwareError: null, builds: {}, checking: new Set(), models: null,
                       expanded: new Set(), railAsk: null, lessons: {}, lessonOf: {}, activity: new Set(), tutorError: null,
                       tutorSettings: null, tutorSettingsError: null, filesOrigins: new Map(), editorFile: {},
                       cols: {}, tabLayout: null, dragging: false, renderPending: false,
                       kb: { view: "brief", raw: false, showRetired: false, showArchived: false, q: "", seq: 0, changes: null, next: null, navData: null }, kbOpen: 0,
                       mode: null, backups: null, backupsError: null, backupOp: null, ctxWindow: {} });
    // RS's lock: the sidebar comes back as pinned or hidden, never left floating.
    S.railExpanded = S.railPinned;
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
        // A project the broker no longer lists, or that is archived, loses its panes and its lessons.
        for (const key of Object.keys(S.panes)) {
            if (!shownRow(key.split("|")[0])) dropPane(key);
        }
        if (list.body.ok) {
            for (const n of [...S.expanded]) if (!shownRow(n) && n !== S.creating) S.expanded.delete(n);
            for (const n of Object.keys(S.lessons)) if (!shownRow(n)) delete S.lessons[n];
            for (const n of Object.keys(S.cols)) if (!shownRow(n) && n !== S.creating) delete S.cols[n];
        }
        refreshTutor();
        // A lesson's choice of agent offers what its project has now (pi added from the gear).
        for (const pane of Object.values(S.panes)) if (pane.kind === "lesson" && pane.drawAgent) pane.drawAgent();
        if (S.current && S.current !== S.creating && !shownRow(S.current) && list.body.ok) {
            const first = shownProjects()[0];
            S.current = first ? first.name : null;
            if (S.current) refreshServices(S.current);
        }
        render();
        if (allProjectsRedraw) allProjectsRedraw();
    } finally {
        refreshProjects.busy = false;
    }
}

async function refreshServices(name) {
    if (!S.loggedIn) return;
    const r = await api("GET", projectPath(name, "/services"));
    if (!S.loggedIn || !r.body.ok || typeof r.body.services !== "object") return;
    S.services[name] = r.body.services;
    markFileLinks(name);
    if (name === S.current) render();
}

// What the broker says of a project, in words; and whether its sandbox runs.
const OP_WORDS = { create: "being made", start: "starting", stop: "stopping", recreate: "being recreated", destroy: "being destroyed",
                   agents_add: "adding an agent", agents_update: "updating its agents", archive: "archiving", unarchive: "unarchiving" };

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

// ---- Start and Stop on a project's row (RS's makeRailPowerButton, openRailConfirm) ----------
// The button asks in the row; the answer runs the ⚙ box's runAction without its dialog, so the
// row's state line is the feedback and a corner notice comes only when it fails.

// Requests sent from a row's button and not over yet (name -> verb): its button stays busy.
const railPending = new Map();

// RS's RAIL_ICON_SVG (stop, start, busy). Icon geometry from Lucide (https://lucide.dev, ISC licence).
const RAIL_ICON_SVG = (() => {
    const svg = (body, fill) =>
        '<svg viewBox="0 0 24 24" aria-hidden="true" xmlns="http://www.w3.org/2000/svg"'
        + (fill ? ' fill="currentColor" stroke="none">'
                : ' fill="none" stroke="currentColor" stroke-width="2"'
                  + ' stroke-linecap="round" stroke-linejoin="round">')
        + body + '</svg>';
    return {
        stop: svg('<rect x="6" y="6" width="12" height="12" rx="1.5"/>', true),
        start: svg('<polygon points="7 4 20 12 7 20 7 4"/>', true),
        busy: svg('<path d="M21 12a9 9 0 1 1-6.22-8.56"/>'),
    };
})();

// Stop on a running project, Start on any other (the broker's start makes what is missing);
// a disabled spinner while an operation runs, a request is pending, or it is being destroyed.
function railPowerButton(p) {
    const op = opOf(p.name);
    const busyWords = op ? (OP_WORDS[op.verb] || op.verb)
        : railPending.has(p.name) ? OP_WORDS[railPending.get(p.name)]
        : p.wanted === "destroying" ? OP_WORDS.destroy : null;
    const verb = p.state === "running" ? "stop" : "start";
    const title = busyWords || `${verb === "stop" ? "Stop" : "Start"} ${p.name}`;
    const btn = h("button", { class: "power-btn" + (busyWords ? " busy" : verb === "start" ? " go" : ""), type: "button",
                              title, "aria-label": title, "data-name": p.name, disabled: !!busyWords,
                              onclick: (ev) => {
                                  ev.stopPropagation();
                                  if (btn.disabled) return;
                                  S.railAsk = { name: p.name, verb };
                                  renderRail();
                                  const no = S.shell.rail.querySelector('.rail-confirm [data-ask="no"]');
                                  if (no) no.focus();
                              } });
    btn.innerHTML = RAIL_ICON_SVG[busyWords ? "busy" : verb];
    return btn;
}

// The asked row, drawn as RS's inline question. Clicks and presses in it never reach the row.
function railAskRow(name, verb) {
    const stop = verb === "stop";
    const cancel = () => {
        S.railAsk = null;
        renderRail();
        const back = [...S.shell.rail.querySelectorAll(".power-btn")].find((b) => b.dataset.name === name);
        if (back) back.focus();
    };
    const go = h("button", { class: stop ? "danger" : "primary", type: "button", "data-ask": "go",
                             onclick: (ev) => { ev.stopPropagation(); railAnswer(name, verb); } }, stop ? "Stop" : "Start");
    const no = h("button", { type: "button", "data-ask": "no", onclick: (ev) => { ev.stopPropagation(); cancel(); } }, "Cancel");
    return h("div", { class: "proj-row rail-confirming", onclick: (ev) => ev.stopPropagation(),
                      onpointerdown: (ev) => ev.stopPropagation(),
                      onkeydown: (ev) => { if (ev.key !== "Escape") return; ev.stopPropagation(); cancel(); } },
        h("div", { class: "rail-confirm" },
            h("div", { class: "rail-confirm-q" }, stop ? ["Stop ", h("b", {}, name), "? Running work is interrupted."] : ["Start ", h("b", {}, name), "?"]),
            h("div", { class: "rail-confirm-btns" }, go, no)));
}

// The answer: the ⚙ box's runAction, quiet (no dialog, a notice only on failure); the button is
// busy from here until the operation is over.
async function railAnswer(name, verb) {
    S.railAsk = null;
    railPending.set(name, verb);
    renderRail();
    try {
        await runAction(name, verb, { quiet: true });
    } finally {
        railPending.delete(name);
        if (S.shell) renderRail();
    }
}

function renderRail() {
    if (S.railAsk && !shownRow(S.railAsk.name)) S.railAsk = null;
    // Which answer of the question had the focus, so a rebuild (the poll) keeps it there.
    const active = document.activeElement;
    const hadAsk = S.railAsk && active && S.shell.rail.contains(active) ? active.dataset.ask : null;
    const rows = shownProjects().map((p) => {
        const st = stateWords(p);
        const op = opOf(p.name);
        const open = S.expanded.has(p.name);
        const asked = S.railAsk && S.railAsk.name === p.name;
        return h("div", { class: "proj" + (p.name === S.current && !S.page ? " sel" : "") },
            asked ? railAskRow(p.name, S.railAsk.verb) : h("div", { class: "proj-row", onclick: () => selectProject(p.name) },
                h("span", { class: `dot ${st.cls}` }),
                h("span", { class: "proj-name" }, p.name,
                    h("span", { class: "proj-prog" }, st.text, op ? " · " : null,
                        op ? h("button", { class: "link", title: "Show the operation's progress",
                                           onclick: (ev) => { ev.stopPropagation(); followOp(op.op_id, `${p.name}: ${OP_WORDS[op.verb] || op.verb}`); } }, "log") : null)),
                railPowerButton(p),
                h("button", { class: "gear", title: "Start, stop, limits, destroy",
                              onclick: (ev) => { ev.stopPropagation(); projectDialog(p.name); } }, "⚙"),
                h("button", { class: "chev", title: open ? "Hide the lessons" : "Show the lessons",
                              onclick: (ev) => { ev.stopPropagation(); toggleLessons(p.name); } }, open ? "▾" : "▸")),
            open ? lessonRows(p.name) : null);
    });
    if (!rows.length) rows.push(h("div", { class: "none" }, S.projects.length ? "Every project is archived: ▤ All projects lists them." : "No project yet."));
    if (S.listError) rows.push(h("div", { class: "none bad" }, S.listError));
    // In privileged mode, a quiet line at the bottom of the list, leading to what that means.
    if (S.mode === "privileged") {
        rows.push(h("button", { class: "mode-line", title: "What privileged mode means: Settings", onclick: () => showSettings("mode") }, "privileged mode"));
    }
    S.shell.rail.replaceChildren(...rows);
    if (hadAsk) {
        const again = S.shell.rail.querySelector(`.rail-confirm [data-ask="${hadAsk}"]`);
        if (again) again.focus();
    }
    S.shell.rail.parentNode.querySelector(".settings-btn").classList.toggle("sel", S.page === "settings");
    renderKbFoot();
}

// Knowledge or Settings in the sidebar: open the page, or, when it is the one shown, go back
// to the project you were in (its tab as you left it), or the empty page with none.
function togglePage(page, show) {
    if (S.page !== page) { show(); return; }
    collapseRailAfterUse();
    S.page = null;
    saveLayout();
    render();
}

// A click on the project you are in shows or hides its lessons; on another one, opens it with them shown.
function selectProject(name) {
    if (S.current === name && !S.page) { toggleLessons(name); return; }
    if (!S.expanded.has(name)) { S.expanded.add(name); refreshLessons(name); }
    collapseRailAfterUse();
    S.page = null;
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
    const lid = S.lessonOf[name];
    if (lid) tabs.unshift({ id: lessonTab(lid), label: lessonTitle(name, lid), type: "lesson" });
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
    tabs.push({ id: "insights", label: "Insights", type: "kb" });
    return { tabs, raw };
}

// The strip: the Projects tab, the project's chip, then its tabs (the Ports page among them) in their columns'
// order. Side by side (desktop, two or three columns), each column's tabs sit in a group above
// its pane, as RS's renderServiceTabs; at phone width it is one flat strip, as before.
function renderTabbar() {
    if (S.dragging) { S.renderPending = true; return; }
    const bar = S.shell.tabbar;
    const name = S.current;
    bar.classList.remove("cols");
    if (S.page === "settings") { S.tabLayout = null; bar.replaceChildren(makeProjectsTab(), h("div", { class: "tb projchip" }, "Settings")); return; }
    if (S.page === "knowledge") { S.tabLayout = null; bar.replaceChildren(makeProjectsTab(), h("div", { class: "tb projchip" }, "Knowledge")); return; }
    if (!name) { S.tabLayout = null; bar.replaceChildren(makeProjectsTab(), h("div", { class: "tb projchip" }, "clanker-learn")); return; }
    const { tabs, raw } = tabsOf(name);
    tabs.push({ id: "ports", label: "Ports", type: "ports" });
    // A tab that is not there any more (once the list is known) gives the focus to another tab
    // of its column, else to the Shell.
    if (S.services[name] && !tabs.some((t) => t.id === tabOf(name))) {
        const gone = tabOf(name);
        const before = S.tabLayout && S.tabLayout.project === name ? S.tabLayout.columns : (S.cols[name] && S.cols[name].columns) || [];
        const col = before.find((c) => c.includes(gone)) || [];
        S.tabs[name] = col.find((id) => id !== gone && tabs.some((t) => t.id === id)) || "shell";
    }
    const byId = new Map(tabs.map((t) => [t.id, t]));
    const L = columnLayout(name, tabs.map((t) => t.id));
    const narrow = narrowScreen();
    const multi = L.columns.length > 1 && !narrow;
    const on = new Set(narrow ? [tabOf(name)] : L.columns.map((_, i) => columnActive(name, i)));
    const tabEl = (id, col) => makeTab(name, byId.get(id), col, on.has(id));
    const rawEls = raw.map(({ row }) => h("div", { class: "tb raw", title: `Raw TCP to port ${row.sandbox_port} of the sandbox. It has no login in front of it.` },
        h("span", { class: "rawlabel" }, String(row.label)), h("code", {}, `${location.hostname}:${row.passthrough_port}`)));
    // The Projects tab first in every branch (RS's ensureProjectsTab: it is the only way to open
    // a hidden sidebar). With columns it stays before the divider, so the first group starts after it.
    const items = [makeProjectsTab(), h("div", { class: "tb projchip" }, name)];
    if (multi) {
        bar.classList.add("cols");
        items.push(h("div", { class: "tab-group-divider" }));
        L.columns.forEach((col, i) => items.push(h("div", { class: "col-group" + (i === 0 ? " first" : "") },
            col.map((id) => tabEl(id, i)), i === L.columns.length - 1 ? rawEls : null)));
    } else {
        // The Ports tab keeps its place at the right while it is the last one.
        const flat = L.columns.flatMap((col, i) => col.map((id) => [id, i]));
        flat.forEach(([id, i], k) => {
            if (k === flat.length - 1 && id === "ports") items.push(...rawEls, h("div", { class: "spacer" }));
            items.push(tabEl(id, i));
        });
        if (!flat.length || flat[flat.length - 1][0] !== "ports") items.push(...rawEls, h("div", { class: "spacer" }));
    }
    bar.replaceChildren(...items);
    if (multi) positionStripGroups(L.ratios);
}

function makeTab(name, t, col, on) {
    const pane = S.panes[paneKey(name, t.id)];
    const url = pane && pane.url;
    const tab = h("div", { class: `tb t-${t.type}` + (on ? " on" : "") + (t.gone ? " gone" : ""), "data-tab": t.id, "data-column": String(col),
                           title: t.gone ? "This app no longer listens in the sandbox" : t.type === "lesson" ? "the lesson's conversation with the tutor"
                               : t.type === "kb" ? "what this project's tutor has learned about how you learn"
                               : t.type === "ports" ? null : (t.row ? `port ${t.row.default_port} of the sandbox` : "a terminal in the sandbox"),
                           // consumeTabDragClick swallows the one click the browser makes on the tab after a drag.
                           onclick: () => { if (!consumeTabDragClick()) showTab(name, t.id); } },
        isWebTab(t) ? refreshTabButton(name, t.id) : null,
        h("span", { class: "tl" }, t.label),
        t.type === "lesson" ? h("button", { class: "tclose", title: "Close the tab (the lesson stays in the list)", "aria-label": "Close the tab",
                                           onclick: (ev) => { ev.stopPropagation(); closeLessonTab(name, lessonTabId(t.id)); } }, "×") : null,
        t.type !== "cli" && t.type !== "ports" && t.type !== "kb" && url ? h("a", { class: "newtab", href: url, target: "_blank", rel: "noopener noreferrer",
                                                                  title: "Open in a new browser tab", onclick: (ev) => ev.stopPropagation() }, "↗") : null);
    armTabDrag(tab, name, t.id);
    return tab;
}

// A tab whose pane is a frame (the Editor, an app in the sandbox).
const isWebTab = (t) => !!t.row;

// RS's per-tab refresh glyph (webui/static/app.js REFRESH_TAB_SVG): a constant, so innerHTML
// carries no server string.
const REFRESH_TAB_SVG =
    '<svg viewBox="0 0 16 16" fill="currentColor" xmlns="http://www.w3.org/2000/svg">' +
    '<path d="M8 1.5A6.5 6.5 0 1 1 1.5 8h2A4.5 4.5 0 1 0 8 3.5z"/>' +
    '<path d="M8 0l4.5 2.5L8 5z"/></svg>';

// RS's refresh button on a tab: reloads the tab's frame without selecting or dragging the tab
// (the drag ignores a button). A tab not opened yet is opened.
function refreshTabButton(name, id) {
    const b = h("button", { class: "refresh-tab-btn", title: "Refresh tab", "aria-label": "Refresh tab",
                            onclick: (ev) => {
                                ev.stopPropagation();
                                const pane = S.panes[paneKey(name, id)];
                                if (pane && pane.refresh) pane.refresh(); else showTab(name, id);
                            } });
    b.innerHTML = REFRESH_TAB_SVG;
    return b;
}

function showTab(name, id) {
    const had = !!S.panes[paneKey(name, id)];
    // The tab shown before stays its column's tab; this one becomes its own column's.
    noteActive(name, tabOf(name));
    noteActive(name, id);
    S.tabs[name] = id;
    (S.cols[name] || (S.cols[name] = { columns: [], active: [] })).focus = id;
    saveLayout();
    render();
    if (id === "ports" && had) refreshPorts(name);   // a new Ports pane asks by itself
}

// The pane of a tab, made on first sight and kept (hidden) while no column shows it.
function ensurePane(name, id) {
    const key = paneKey(name, id);
    let pane = S.panes[key];
    if (pane) return pane;
    if (id === "shell") pane = makeShellPane(name);
    else if (id === "ports") pane = makePortsPane(name);
    else if (id === "insights") pane = makeInsightsPane(name);
    else if (lessonTabId(id)) pane = makeLessonPane(name, lessonTabId(id));
    else {
        const row = (S.services[name] || {})[id];
        if (row) pane = makeWebPane(name, id, row);
    }
    if (pane) {
        S.panes[key] = pane;
        S.shell.panes.append(pane.el);
        if (pane.kind === "ports") refreshPorts(name);
    }
    return pane;
}

// The visible panes: one per column (one in all at phone width). Every one of them is live; a
// pane that no column shows lets go of what it follows (a lesson closes its stream).
function showPane() {
    if (S.dragging) { S.renderPending = true; return; }
    const { panes, empty } = S.shell;
    const name = S.current;
    sweepColumnChrome();
    const shown = [];   // {el, col, pane}
    const L = name && !S.page && S.tabLayout && S.tabLayout.project === name ? S.tabLayout : null;
    const multi = !!L && L.columns.length > 1 && !narrowScreen();
    if (S.page === "settings") {
        shown.push({ el: S.shell.settings.el, col: 0 });
    } else if (S.page === "knowledge") {
        shown.push({ el: S.shell.knowledge.el, col: 0 });
    } else if (!name) {
        empty.replaceChildren(h("div", { class: "page-inner" }, h("h1", {}, "No project shown"),
            h("p", { class: "sub" }, shownProjects().length ? "Pick a project on the left."
                : S.projects.length ? "Every project is archived: “▤ All projects” on the left brings one back."
                : "Make one with “+ New project” on the left.")));
        shown.push({ el: empty, col: 0 });
    } else {
        const ids = multi ? L.columns.map((_, i) => columnActive(name, i)) : [tabOf(name)];
        ids.forEach((id, col) => {
            const pane = ensurePane(name, id);
            if (pane) { shown.push({ el: pane.el, col, pane }); return; }
            if (shown.some((s) => s.el === empty)) return;
            empty.replaceChildren(h("div", { class: "none" }, "Asking the sandbox for its tabs…"));
            shown.push({ el: empty, col });
        });
    }
    const where = new Map(shown.map((s) => [s.el, s.col]));
    for (const child of panes.children) {
        if (!child.classList.contains("pane")) continue;
        child.hidden = !where.has(child);
        if (multi && where.has(child)) { child.dataset.col = String(where.get(child)); continue; }
        delete child.dataset.col;
        child.style.left = child.style.width = "";
    }
    for (const s of shown) if (s.pane && s.pane.update) s.pane.update();
    for (const pane of Object.values(S.panes)) if (!where.has(pane.el) && pane.hide) pane.hide();
    if (multi) {
        for (let i = 1; i < L.columns.length; i++) {
            const sp = h("div", { class: "column-splitter", title: "Drag to change the widths" });
            installColumnSplitterDrag(sp, i - 1);
            panes.append(sp);
        }
        positionColumnChrome(L.ratios);
    }
    refitVisible();
}

function dropPane(key) {
    const pane = S.panes[key];
    if (!pane) return;
    if (pane.close) pane.close();
    pane.el.remove();
    delete S.panes[key];
}

// ---- the interface settings: RS's ---------------------------------------------------------
//
// RS's (webui/static/app.js at RS commit 9c995b6): THEMES, DEFAULT_THEME, TERM_THEMES,
// applyTheme, applyTermTheme, currentXtermTheme, repaintTerminals, the iframe zoom, the CLI
// width and the CLI font size (their load/apply/set functions and selectors, syncTermFontSize),
// lifted. Kept per browser in its storage as RS keeps them, under this page's own keys, and
// every read and write of it may fail (a private window, blocked site data): the page then
// works with the defaults. Adapted: each theme also carries the colours this page has and RS's
// does not (--bg-rail, --warn-fg, the tab colours); the defaults are the maintainer's RS
// settings (Editor 100%, CLI width 1000 px, CLI font size 16 px; RS's own: 90%, full width,
// 13 px); RS's `state` is S; RS's Layout selector is left out (this page has no phone layout).
// Shown in Settings -> Interface (RS: its floating Settings box, renderSettingsInto).

const THEME_KEY = "cl-theme";
// Terminal palette, chosen apart from the interface theme. Absent = follow the interface theme's own.
const TERM_THEME_KEY = "cl-term-theme";
const IFRAME_ZOOM_KEY = "cl-iframe-zoom";
const IFRAME_ZOOMS = [0.7, 0.8, 0.9, 1.0, 1.1, 1.2];
const IFRAME_ZOOM_DEFAULT = 1.0;
// A px cap on the Shell's terminal, for line length on wide windows; applied window-side (a
// max-width on .term-pad, which the fit add-on measures). 0 = full width.
const CLI_WIDTH_KEY = "cl-cli-max-width";
const CLI_MAX_WIDTHS = [0, 800, 1000, 1200];
const CLI_MAX_WIDTH_DEFAULT = 1000;
const TERM_FONT_SIZE_KEY = "cl-term-font-size";
const TERM_FONT_SIZES = [11, 12, 13, 14, 16, 18];
const TERM_FONT_SIZE_DEFAULT = 16;
// The sidebar's width (RS's rail): dragged at its right edge, kept, clamped as RS's.
const RAIL_WIDTH_KEY = "cl-rail-width";
const RAIL_WIDTH_DEFAULT = 250;
const RAIL_WIDTH_MIN = 80;
const RAIL_WIDTH_MAX = 480;
// Whether the sidebar is pinned beside the page (RS's RAIL_PINNED_KEY); unpinned by default.
const RAIL_PINNED_KEY = "cl-rail-pinned";

// The browser's storage, which may refuse.
function storeGet(key) { try { return localStorage.getItem(key); } catch (_) { return null; } }
function storeSet(key, v) { try { localStorage.setItem(key, v); } catch (_) { /* the page works without it */ } }
function storeDel(key) { try { localStorage.removeItem(key); } catch (_) { /* the page works without it */ } }

const THEMES = {
    dark: {
        label: "Dark",
        xterm: {
            background: "#000000", foreground: "#e0e0e0",
            cursor: "#e0e0e0", cursorAccent: "#000000",
            selectionBackground: "rgba(255,255,255,0.25)",
            black: "#000000", red: "#cc0403", green: "#19cb00", yellow: "#cecb00",
            blue: "#0d73cc", magenta: "#cb1ed1", cyan: "#0dcdcd", white: "#dddddd",
            brightBlack: "#767676", brightRed: "#f2201f", brightGreen: "#23fd00",
            brightYellow: "#fffd00", brightBlue: "#1a8fff", brightMagenta: "#fd28ff",
            brightCyan: "#14ffff", brightWhite: "#ffffff",
        },
        css: {
            "--bg-base": "#1e1e1e", "--bg-card": "#2a2a2a", "--bg-active": "#1e1e1e",
            "--bg-input": "#1a1a1a", "--bg-input-focus-border": "#6c9",
            "--fg-base": "#e0e0e0", "--fg-muted": "#aaa", "--fg-faint": "#777",
            "--fg-accent": "#6c9",
            "--border": "#444", "--border-strong": "#555",
            "--btn-bg": "#4a7c4e", "--btn-bg-hover": "#5a8c5e",
            "--btn-secondary-bg": "#444", "--btn-secondary-bg-hover": "#555",
            "--btn-danger-bg": "#7c3a3a", "--btn-danger-bg-hover": "#8c4a4a",
            "--error-fg": "#e88",
            "--status-up": "#6c6", "--status-down": "#555", "--status-error": "#e66",
            "--pane-bg": "#000",
            "--bg-rail": "#252525", "--warn-fg": "#d29922",
            "--tab-type-cli": "#2e7d32", "--tab-type-editor": "#17a2b8", "--tab-type-other": "#8a8a8a",
            "--tab-type-lesson": "#a371f7", "--tab-type-kb": "#d29922",
        },
    },
    light: {
        label: "Light",
        xterm: {
            background: "#ffffff", foreground: "#2a2a2a",
            cursor: "#2a2a2a", cursorAccent: "#ffffff",
            selectionBackground: "rgba(0,0,0,0.18)",
            black: "#2a2a2a", red: "#c91b00", green: "#00c200", yellow: "#c7c400",
            blue: "#0225c7", magenta: "#ca30c7", cyan: "#00c5c7", white: "#c7c7c7",
            brightBlack: "#676767", brightRed: "#ff6e67", brightGreen: "#5ffa68",
            brightYellow: "#fffc67", brightBlue: "#6871ff", brightMagenta: "#ff77ff",
            brightCyan: "#60fdff", brightWhite: "#ffffff",
        },
        css: {
            "--bg-base": "#fafafa", "--bg-card": "#ececec", "--bg-active": "#ffffff",
            "--bg-input": "#ffffff", "--bg-input-focus-border": "#3a8a3a",
            "--fg-base": "#1f1f1f", "--fg-muted": "#555", "--fg-faint": "#888",
            "--fg-accent": "#3a8a3a",
            "--border": "#d0d0d0", "--border-strong": "#bbb",
            "--btn-bg": "#3a8a3a", "--btn-bg-hover": "#4a9a4a",
            "--btn-secondary-bg": "#d0d0d0", "--btn-secondary-bg-hover": "#bbb",
            "--btn-danger-bg": "#b03a3a", "--btn-danger-bg-hover": "#c04a4a",
            "--error-fg": "#a33",
            "--status-up": "#3a8a3a", "--status-down": "#aaa", "--status-error": "#c04040",
            "--pane-bg": "#ffffff",
            "--bg-rail": "#f0f0f0", "--warn-fg": "#9a6700",
            "--tab-type-cli": "#2e7d32", "--tab-type-editor": "#138496", "--tab-type-other": "#8a8a8a",
            "--tab-type-lesson": "#8250df", "--tab-type-kb": "#b7791f",
        },
    },
    "solarized-dark": {
        label: "Solarized Dark",
        xterm: {
            background: "#002b36", foreground: "#839496",
            cursor: "#93a1a1", cursorAccent: "#002b36",
            selectionBackground: "rgba(147,161,161,0.25)",
            black: "#073642", red: "#dc322f", green: "#859900", yellow: "#b58900",
            blue: "#268bd2", magenta: "#d33682", cyan: "#2aa198", white: "#eee8d5",
            brightBlack: "#002b36", brightRed: "#cb4b16", brightGreen: "#586e75",
            brightYellow: "#657b83", brightBlue: "#839496", brightMagenta: "#6c71c4",
            brightCyan: "#93a1a1", brightWhite: "#fdf6e3",
        },
        css: {
            "--bg-base": "#002b36", "--bg-card": "#073642", "--bg-active": "#002b36",
            "--bg-input": "#001f27", "--bg-input-focus-border": "#268bd2",
            "--fg-base": "#93a1a1", "--fg-muted": "#839496", "--fg-faint": "#657b83",
            "--fg-accent": "#2aa198",
            "--border": "#0a4452", "--border-strong": "#0e5a6f",
            "--btn-bg": "#268bd2", "--btn-bg-hover": "#3a9be0",
            "--btn-secondary-bg": "#0a4452", "--btn-secondary-bg-hover": "#0e5a6f",
            "--btn-danger-bg": "#dc322f", "--btn-danger-bg-hover": "#ec4240",
            "--error-fg": "#dc322f",
            "--status-up": "#859900", "--status-down": "#586e75", "--status-error": "#dc322f",
            "--pane-bg": "#002b36",
            "--bg-rail": "#04313d", "--warn-fg": "#b58900",
            "--tab-type-cli": "#859900", "--tab-type-editor": "#2aa198", "--tab-type-other": "#586e75",
            "--tab-type-lesson": "#6c71c4", "--tab-type-kb": "#b58900",
        },
    },
    dracula: {
        label: "Dracula",
        xterm: {
            background: "#282a36", foreground: "#f8f8f2",
            cursor: "#f8f8f2", cursorAccent: "#282a36",
            selectionBackground: "rgba(68,71,90,0.7)",
            black: "#21222c", red: "#ff5555", green: "#50fa7b", yellow: "#f1fa8c",
            blue: "#bd93f9", magenta: "#ff79c6", cyan: "#8be9fd", white: "#f8f8f2",
            brightBlack: "#6272a4", brightRed: "#ff6e6e", brightGreen: "#69ff94",
            brightYellow: "#ffffa5", brightBlue: "#d6acff", brightMagenta: "#ff92df",
            brightCyan: "#a4ffff", brightWhite: "#ffffff",
        },
        css: {
            "--bg-base": "#282a36", "--bg-card": "#343746", "--bg-active": "#282a36",
            "--bg-input": "#21222c", "--bg-input-focus-border": "#bd93f9",
            "--fg-base": "#f8f8f2", "--fg-muted": "#bdbdc8", "--fg-faint": "#6272a4",
            "--fg-accent": "#bd93f9",
            "--border": "#44475a", "--border-strong": "#5c5f74",
            "--btn-bg": "#50fa7b", "--btn-bg-hover": "#69ff94",
            "--btn-secondary-bg": "#44475a", "--btn-secondary-bg-hover": "#5c5f74",
            "--btn-danger-bg": "#ff5555", "--btn-danger-bg-hover": "#ff6e6e",
            "--error-fg": "#ff5555",
            "--status-up": "#50fa7b", "--status-down": "#6272a4", "--status-error": "#ff5555",
            "--pane-bg": "#282a36",
            "--bg-rail": "#2e303e", "--warn-fg": "#ffb86c",
            "--tab-type-cli": "#50fa7b", "--tab-type-editor": "#8be9fd", "--tab-type-other": "#6272a4",
            "--tab-type-lesson": "#bd93f9", "--tab-type-kb": "#ffb86c",
        },
    },
    nord: {
        label: "Nord",
        xterm: {
            background: "#2e3440", foreground: "#d8dee9",
            cursor: "#d8dee9", cursorAccent: "#2e3440",
            selectionBackground: "rgba(76,86,106,0.7)",
            black: "#3b4252", red: "#bf616a", green: "#a3be8c", yellow: "#ebcb8b",
            blue: "#81a1c1", magenta: "#b48ead", cyan: "#88c0d0", white: "#e5e9f0",
            brightBlack: "#4c566a", brightRed: "#bf616a", brightGreen: "#a3be8c",
            brightYellow: "#ebcb8b", brightBlue: "#81a1c1", brightMagenta: "#b48ead",
            brightCyan: "#8fbcbb", brightWhite: "#eceff4",
        },
        css: {
            "--bg-base": "#2e3440", "--bg-card": "#3b4252", "--bg-active": "#2e3440",
            "--bg-input": "#272c36", "--bg-input-focus-border": "#88c0d0",
            "--fg-base": "#d8dee9", "--fg-muted": "#a8b2c1", "--fg-faint": "#7884a0",
            "--fg-accent": "#88c0d0",
            "--border": "#434c5e", "--border-strong": "#4c566a",
            "--btn-bg": "#5e81ac", "--btn-bg-hover": "#7592b8",
            "--btn-secondary-bg": "#434c5e", "--btn-secondary-bg-hover": "#4c566a",
            "--btn-danger-bg": "#bf616a", "--btn-danger-bg-hover": "#cf717a",
            "--error-fg": "#bf616a",
            "--status-up": "#a3be8c", "--status-down": "#4c566a", "--status-error": "#bf616a",
            "--pane-bg": "#2e3440",
            "--bg-rail": "#353b49", "--warn-fg": "#ebcb8b",
            "--tab-type-cli": "#a3be8c", "--tab-type-editor": "#88c0d0", "--tab-type-other": "#7884a0",
            "--tab-type-lesson": "#b48ead", "--tab-type-kb": "#ebcb8b",
        },
    },
};

const DEFAULT_THEME = "nord";

// Terminal-only palettes (Settings → Terminal theme). The interface themes'
// own palettes are REFERENCED, never copied, so "Same as interface theme"
// and naming that same theme explicitly can never drift apart. `tone` only
// groups the dropdown. The added palettes follow each project's published
// terminal colors; Monokai Dimmed is the terminal panel of the default editor
// theme (code-server-settings.json) — the bundled theme gives its ANSI
// colors but no terminal foreground, cursor or selection, so those are
// code-server's own fallbacks (foreground/cursor #cccccc, selection = the
// editor selection #676b7180), and its ANSI black equals its background
// exactly as it does in the editor.
const TERM_THEMES = {
    dark: { label: "Dark", tone: "dark", xterm: THEMES.dark.xterm },
    "solarized-dark": { label: "Solarized Dark", tone: "dark", xterm: THEMES["solarized-dark"].xterm },
    dracula: { label: "Dracula", tone: "dark", xterm: THEMES.dracula.xterm },
    nord: { label: "Nord", tone: "dark", xterm: THEMES.nord.xterm },
    "gruvbox-dark": {
        label: "Gruvbox Dark", tone: "dark",
        xterm: {
            background: "#282828", foreground: "#ebdbb2",
            cursor: "#ebdbb2", cursorAccent: "#282828",
            selectionBackground: "rgba(146,131,116,0.4)",
            black: "#282828", red: "#cc241d", green: "#98971a", yellow: "#d79921",
            blue: "#458588", magenta: "#b16286", cyan: "#689d6a", white: "#a89984",
            brightBlack: "#928374", brightRed: "#fb4934", brightGreen: "#b8bb26",
            brightYellow: "#fabd2f", brightBlue: "#83a598", brightMagenta: "#d3869b",
            brightCyan: "#8ec07c", brightWhite: "#ebdbb2",
        },
    },
    "one-dark": {
        label: "One Dark", tone: "dark",
        xterm: {
            background: "#282c34", foreground: "#abb2bf",
            cursor: "#528bff", cursorAccent: "#282c34",
            selectionBackground: "rgba(103,118,150,0.4)",
            black: "#3f4451", red: "#e05561", green: "#8cc265", yellow: "#d18f52",
            blue: "#4aa5f0", magenta: "#c162de", cyan: "#42b3c2", white: "#e6e6e6",
            brightBlack: "#4f5666", brightRed: "#ff616e", brightGreen: "#a5e075",
            brightYellow: "#f0a45d", brightBlue: "#4dc4ff", brightMagenta: "#de73ff",
            brightCyan: "#4cd1e0", brightWhite: "#ffffff",
        },
    },
    "tokyo-night": {
        label: "Tokyo Night", tone: "dark",
        xterm: {
            background: "#1a1b26", foreground: "#c0caf5",
            cursor: "#c0caf5", cursorAccent: "#1a1b26",
            selectionBackground: "#283457",
            black: "#15161e", red: "#f7768e", green: "#9ece6a", yellow: "#e0af68",
            blue: "#7aa2f7", magenta: "#bb9af7", cyan: "#7dcfff", white: "#a9b1d6",
            brightBlack: "#414868", brightRed: "#f7768e", brightGreen: "#9ece6a",
            brightYellow: "#e0af68", brightBlue: "#7aa2f7", brightMagenta: "#bb9af7",
            brightCyan: "#7dcfff", brightWhite: "#c0caf5",
        },
    },
    "catppuccin-mocha": {
        label: "Catppuccin Mocha", tone: "dark",
        xterm: {
            background: "#1e1e2e", foreground: "#cdd6f4",
            cursor: "#f5e0dc", cursorAccent: "#1e1e2e",
            selectionBackground: "rgba(88,91,112,0.6)",
            black: "#45475a", red: "#f38ba8", green: "#a6e3a1", yellow: "#f9e2af",
            blue: "#89b4fa", magenta: "#f5c2e7", cyan: "#94e2d5", white: "#bac2de",
            brightBlack: "#585b70", brightRed: "#f38ba8", brightGreen: "#a6e3a1",
            brightYellow: "#f9e2af", brightBlue: "#89b4fa", brightMagenta: "#f5c2e7",
            brightCyan: "#94e2d5", brightWhite: "#a6adc8",
        },
    },
    "monokai-dimmed": {
        label: "Monokai Dimmed", tone: "dark",
        xterm: {
            background: "#1e1e1e", foreground: "#cccccc",
            cursor: "#cccccc", cursorAccent: "#1e1e1e",
            selectionBackground: "#676b7180",
            black: "#1e1e1e", red: "#c4265e", green: "#86b42b", yellow: "#b3b42b",
            blue: "#6a7ec8", magenta: "#8c6bc8", cyan: "#56adbc", white: "#e3e3dd",
            brightBlack: "#666666", brightRed: "#f92672", brightGreen: "#a6e22e",
            brightYellow: "#e2e22e", brightBlue: "#819aff", brightMagenta: "#ae81ff",
            brightCyan: "#66d9ef", brightWhite: "#f8f8f2",
        },
    },
    light: { label: "Light", tone: "light", xterm: THEMES.light.xterm },
    "solarized-light": {
        label: "Solarized Light", tone: "light",
        xterm: {
            background: "#fdf6e3", foreground: "#657b83",
            cursor: "#586e75", cursorAccent: "#fdf6e3",
            selectionBackground: "rgba(7,54,66,0.15)",
            black: "#073642", red: "#dc322f", green: "#859900", yellow: "#b58900",
            blue: "#268bd2", magenta: "#d33682", cyan: "#2aa198", white: "#eee8d5",
            brightBlack: "#002b36", brightRed: "#cb4b16", brightGreen: "#586e75",
            brightYellow: "#657b83", brightBlue: "#839496", brightMagenta: "#6c71c4",
            brightCyan: "#93a1a1", brightWhite: "#fdf6e3",
        },
    },
    "catppuccin-latte": {
        label: "Catppuccin Latte", tone: "light",
        xterm: {
            background: "#eff1f5", foreground: "#4c4f69",
            cursor: "#dc8a78", cursorAccent: "#eff1f5",
            selectionBackground: "rgba(172,176,190,0.6)",
            black: "#5c5f77", red: "#d20f39", green: "#40a02b", yellow: "#df8e1d",
            blue: "#1e66f5", magenta: "#ea76cb", cyan: "#179299", white: "#acb0be",
            brightBlack: "#6c6f85", brightRed: "#d20f39", brightGreen: "#40a02b",
            brightYellow: "#df8e1d", brightBlue: "#1e66f5", brightMagenta: "#ea76cb",
            brightCyan: "#179299", brightWhite: "#bcc0cc",
        },
    },
    "github-light": {
        label: "GitHub Light", tone: "light",
        xterm: {
            background: "#ffffff", foreground: "#1f2328",
            cursor: "#0969da", cursorAccent: "#ffffff",
            selectionBackground: "rgba(9,105,218,0.2)",
            black: "#24292f", red: "#cf222e", green: "#116329", yellow: "#4d2d00",
            blue: "#0969da", magenta: "#8250df", cyan: "#1b7c83", white: "#6e7781",
            brightBlack: "#57606a", brightRed: "#a40e26", brightGreen: "#1a7f37",
            brightYellow: "#633c01", brightBlue: "#218bff", brightMagenta: "#a475f9",
            brightCyan: "#3192aa", brightWhite: "#8c959f",
        },
    },
};

function loadStoredTheme() {
    const id = storeGet(THEME_KEY);
    return THEMES[id] ? id : DEFAULT_THEME;
}

// null = follow the interface theme (the stored key is absent).
function loadStoredTermTheme() {
    const id = storeGet(TERM_THEME_KEY);
    return TERM_THEMES[id] ? id : null;
}

function applyTheme(id) {
    const valid = THEMES[id] ? id : DEFAULT_THEME;
    for (const [k, v] of Object.entries(THEMES[valid].css)) {
        document.documentElement.style.setProperty(k, v);
    }
    S.theme = valid;
    storeSet(THEME_KEY, valid);
    repaintTerminals();
}

function applyTermTheme(id) {
    S.termTheme = TERM_THEMES[id] ? id : null;
    if (S.termTheme) storeSet(TERM_THEME_KEY, S.termTheme);
    else storeDel(TERM_THEME_KEY);
    repaintTerminals();
}

function currentXtermTheme() {
    const pinned = S.termTheme && TERM_THEMES[S.termTheme];
    return pinned ? pinned.xterm : THEMES[S.theme || DEFAULT_THEME].xterm;
}

// Live recolor of every open terminal, plus --xterm-bg, which paints the Shell's own frame
// (style.css .shell-pane .term-area) in the terminal palette rather than the interface's.
function repaintTerminals() {
    const x = currentXtermTheme();
    document.documentElement.style.setProperty("--xterm-bg", x.background);
    for (const pane of Object.values(S.panes)) {
        if (pane.term) pane.term.options.theme = x;
    }
}

function loadIframeZoom() {
    const v = parseFloat(storeGet(IFRAME_ZOOM_KEY) || "");
    return IFRAME_ZOOMS.includes(v) ? v : IFRAME_ZOOM_DEFAULT;
}

function applyIframeZoomVar(z) {
    document.documentElement.style.setProperty("--iframe-zoom", String(z));
}

function setIframeZoom(z) {
    S.iframeZoom = z;
    applyIframeZoomVar(z);
    storeSet(IFRAME_ZOOM_KEY, String(z));
}

function loadCliMaxWidth() {
    const v = parseInt(storeGet(CLI_WIDTH_KEY) || "", 10);
    return CLI_MAX_WIDTHS.includes(v) ? v : CLI_MAX_WIDTH_DEFAULT;
}

// The cap rides a root variable read by .term-pad; off removes it (max-width: none).
function applyCliMaxWidthVar(v) {
    if (v > 0) document.documentElement.style.setProperty("--cli-max-width", `${v}px`);
    else document.documentElement.style.removeProperty("--cli-max-width");
}

function setCliMaxWidth(v) {
    S.cliMaxWidth = v;
    applyCliMaxWidthVar(v);
    storeSet(CLI_WIDTH_KEY, String(v));
    refitVisible();
}

function loadTermFontSize() {
    const v = parseInt(storeGet(TERM_FONT_SIZE_KEY) || "", 10);
    return TERM_FONT_SIZES.includes(v) ? v : TERM_FONT_SIZE_DEFAULT;
}

// RS's DEFER-APPLY: the setter stores only; the size reaches a terminal through
// syncTermFontSize at its next refit while it is visible (Settings hides every Shell, and
// xterm keeps stale cell metrics when its font changes while hidden).
function setTermFontSize(v) {
    S.termFontSize = v;
    if (v !== TERM_FONT_SIZE_DEFAULT) storeSet(TERM_FONT_SIZE_KEY, String(v));
    else storeDel(TERM_FONT_SIZE_KEY);
    refitVisible();
}

// The stored font size onto one Shell's terminal, only while its pane is visible.
function syncTermFontSize(pane) {
    if (!pane || !pane.term || pane.el.hidden || pane.el.offsetParent === null) return;
    if (pane.term.options.fontSize !== S.termFontSize) pane.term.options.fontSize = S.termFontSize;
}

function makeIframeZoomSelector() {
    const sel = document.createElement("select");
    sel.className = "theme-select";
    sel.title = "Editor zoom (the Editor and every web-app tab)";
    for (const z of IFRAME_ZOOMS) {
        const opt = document.createElement("option");
        opt.value = String(z);
        opt.textContent = `Editor ${Math.round(z * 100)}%`;
        if (Math.abs(z - S.iframeZoom) < 1e-6) opt.selected = true;
        sel.appendChild(opt);
    }
    sel.onchange = () => setIframeZoom(parseFloat(sel.value));
    return sel;
}

function makeCliWidthSelector() {
    const sel = document.createElement("select");
    sel.className = "theme-select";
    sel.title = "Max width of the Shell's terminal (readability on wide windows)";
    // RS's labels: the column counts are approximations at RS's 13 px terminal font.
    const labels = {
        0: "Full width",
        800: "800 px (~100 cols)",
        1000: "1000 px (~125 cols)",
        1200: "1200 px (~150 cols)",
    };
    for (const w of CLI_MAX_WIDTHS) {
        const opt = document.createElement("option");
        opt.value = String(w);
        opt.textContent = labels[w] || `${w} px`;
        if (w === S.cliMaxWidth) opt.selected = true;
        sel.appendChild(opt);
    }
    sel.onchange = () => setCliMaxWidth(parseInt(sel.value, 10));
    return sel;
}

function makeTermFontSizeSelector() {
    const sel = document.createElement("select");
    sel.className = "theme-select";
    sel.title = "Font size of the Shell's terminal (applies to open tabs at once)";
    for (const n of TERM_FONT_SIZES) {
        const opt = document.createElement("option");
        opt.value = String(n);
        opt.textContent = n === TERM_FONT_SIZE_DEFAULT ? `${n} px (default)` : `${n} px`;
        if (n === S.termFontSize) opt.selected = true;
        sel.appendChild(opt);
    }
    sel.onchange = () => setTermFontSize(parseInt(sel.value, 10));
    return sel;
}

function makeThemeSelector() {
    const sel = document.createElement("select");
    sel.className = "theme-select";
    sel.title = "Interface theme";
    for (const [id, t] of Object.entries(THEMES)) {
        const opt = document.createElement("option");
        opt.value = id;
        opt.textContent = t.label;
        if (id === S.theme) opt.selected = true;
        sel.appendChild(opt);
    }
    sel.onchange = () => applyTheme(sel.value);
    return sel;
}

// Terminal theme: "" = follow the interface theme; the named palettes sit in dark/light groups.
function makeTermThemeSelector() {
    const sel = document.createElement("select");
    sel.className = "theme-select";
    sel.title = "Terminal theme (the Shell only)";
    const follow = document.createElement("option");
    follow.value = "";
    follow.textContent = "Same as interface theme";
    if (!S.termTheme) follow.selected = true;
    sel.appendChild(follow);
    for (const [tone, label] of [["dark", "Dark palettes"], ["light", "Light palettes"]]) {
        const group = document.createElement("optgroup");
        group.label = label;
        for (const [id, t] of Object.entries(TERM_THEMES)) {
            if (t.tone !== tone) continue;
            const opt = document.createElement("option");
            opt.value = id;
            opt.textContent = t.label;
            if (id === S.termTheme) opt.selected = true;
            group.appendChild(opt);
        }
        sel.appendChild(group);
    }
    sel.onchange = () => applyTermTheme(sel.value || null);
    return sel;
}

// Settings -> Interface: RS's renderSettingsInto, without Layout. Changes apply at once.
function makeInterfaceSettings() {
    const row = (label, sel) => h("label", { class: "field if-row" }, h("span", {}, label), sel);
    return h("div", { class: "card-body if-set" },
        row("Interface theme", makeThemeSelector()),
        row("Terminal theme", makeTermThemeSelector()),
        row("Editor zoom", makeIframeZoomSelector()),
        row("CLI width", makeCliWidthSelector()),
        row("CLI font size", makeTermFontSizeSelector()));
}

// ---- the sidebar's width: RS's rail splitter ------------------------------------------------

function loadRailWidth() {
    const v = parseInt(storeGet(RAIL_WIDTH_KEY) || "", 10);
    if (!isFinite(v)) return RAIL_WIDTH_DEFAULT;
    return Math.max(RAIL_WIDTH_MIN, Math.min(RAIL_WIDTH_MAX, v));
}

function applyRailWidth(px) {
    document.documentElement.style.setProperty("--rail-width", `${px}px`);
}

// RS's installRailSplitterDrag: a pointer-captured drag on the rail's right edge (so moving
// over a frame or a terminal does not freeze it); lostpointercapture is the cancel path.
// Adapted: the terminals are refitted through refitVisible.
function installRailSplitterDrag(splitter) {
    let dragging = false;
    let pointerId = null;
    const onMove = (ev) => {
        if (!dragging) return;
        const rail = splitter.parentElement;
        if (!rail) return;
        const rect = rail.getBoundingClientRect();
        let w = ev.clientX - rect.left;
        w = Math.max(RAIL_WIDTH_MIN, Math.min(RAIL_WIDTH_MAX, w));
        applyRailWidth(w);
        S.railWidth = w;
    };
    const onLost = (ev) => { if (ev.pointerId === pointerId) onUp(); };
    const onUp = () => {
        if (!dragging) return;
        dragging = false;
        splitter.removeEventListener("lostpointercapture", onLost);
        document.removeEventListener("lostpointercapture", onLost);
        splitter.classList.remove("dragging");
        try { if (pointerId != null) splitter.releasePointerCapture(pointerId); } catch (_) {}
        pointerId = null;
        splitter.removeEventListener("pointermove", onMove);
        splitter.removeEventListener("pointerup", onUp);
        splitter.removeEventListener("pointercancel", onUp);
        document.body.style.userSelect = "";
        storeSet(RAIL_WIDTH_KEY, String(Math.round(S.railWidth)));
        refitVisible();
    };
    splitter.onpointerdown = (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        dragging = true;
        pointerId = ev.pointerId;
        splitter.classList.add("dragging");
        try { splitter.setPointerCapture(ev.pointerId); } catch (_) {}
        splitter.addEventListener("pointermove", onMove);
        splitter.addEventListener("pointerup", onUp);
        splitter.addEventListener("pointercancel", onUp);
        splitter.addEventListener("lostpointercapture", onLost);
        document.addEventListener("lostpointercapture", onLost);
        document.body.style.userSelect = "none";
    };
}

// ---- the sidebar hidden, floating or pinned: RS's rail expand / pin ------------------------------

function loadRailPinned() {
    return storeGet(RAIL_PINNED_KEY) === "1";
}

// RS's makeProjectsTab: the first item of the tab strip on every page, which shows and hides
// the sidebar.
function makeProjectsTab() {
    const expanded = S.railPinned || S.railExpanded;
    return h("div", { class: "tb projects-tab", title: expanded ? "Hide projects" : "Show projects",
                      onclick: (ev) => { ev.stopPropagation(); toggleRailExpanded(); } },
        h("span", { class: "projects-chevron" }, expanded ? "◀" : "▶"), h("span", {}, "Projects"));
}

function makePinButton() {
    const btn = h("button", { class: "pin-btn", title: "Pin sidebar", onclick: (ev) => { ev.stopPropagation(); togglePinned(); } });
    btn.innerHTML = '<svg viewBox="0 0 16 16" fill="currentColor" xmlns="http://www.w3.org/2000/svg"><path d="M9.828.722a.5.5 0 0 1 .354.146l4.95 4.95a.5.5 0 0 1 0 .707c-.48.48-1.072.588-1.503.588-.177 0-.335-.018-.46-.039l-3.134 3.134a5.927 5.927 0 0 1 .16 1.013c.046.702-.032 1.687-.72 2.375a.5.5 0 0 1-.707 0l-2.829-2.828-3.182 3.182c-.195.195-1.219.902-1.414.707-.195-.195.512-1.22.707-1.414l3.182-3.182-2.828-2.829a.5.5 0 0 1 0-.707c.688-.688 1.673-.767 2.375-.72a5.922 5.922 0 0 1 1.013.16l3.134-3.133a2.772 2.772 0 0 1-.04-.461c0-.43.108-1.022.589-1.503A.5.5 0 0 1 9.828.722z"/></svg>';
    return btn;
}

// RS's applyRailState without its phone branch (this page has no phone layout). Adapted: the
// terminals are refitted through refitVisible.
function applyRailState() {
    const shell = document.querySelector(".shell");
    if (!shell) return;
    const expanded = S.railPinned || S.railExpanded;
    shell.classList.toggle("pinned", S.railPinned);
    shell.classList.toggle("expanded", expanded);
    const pinBtn = shell.querySelector(".rail-head .pin-btn");
    if (pinBtn) {
        pinBtn.classList.toggle("pinned", S.railPinned);
        pinBtn.title = S.railPinned ? "Unpin sidebar" : "Pin sidebar";
    }
    const chevron = shell.querySelector(".projects-tab .projects-chevron");
    if (chevron) chevron.textContent = expanded ? "◀" : "▶";
    const projectsTab = shell.querySelector(".projects-tab");
    if (projectsTab) projectsTab.title = expanded ? "Hide projects" : "Show projects";
    // The page's width changes only when the pin does; the terminals measure themselves again.
    setTimeout(refitVisible, 0);
}

function toggleRailExpanded() {
    // The tab is the one show/hide control. If pinned, hiding also unpins (RS).
    if (S.railPinned) {
        S.railPinned = false;
        storeSet(RAIL_PINNED_KEY, "0");
        S.railExpanded = false;
    } else {
        S.railExpanded = !S.railExpanded;
    }
    applyRailState();
}

// RS's auto-collapse of the floating sidebar on any interaction outside it. Two paths, because
// a click in a frame does not bubble out of it: a pointerdown on this document (a terminal, the
// tab strip, a page), and the window's blur with a frame as the active element (a click inside
// the Editor or a web-app tab). A pinned sidebar stays.
function installRailOutsideClickHandlers() {
    document.addEventListener("pointerdown", (ev) => {
        if (!S.railExpanded || S.railPinned) return;
        // A dialog in front (the New project box, a project's ⚙ box, every openModal) owns the interaction.
        if (document.querySelector(".scrim")) return;
        const path = ev.composedPath ? ev.composedPath() : [];
        for (const node of path) {
            if (!node || !node.classList) continue;
            // Inside the sidebar itself: its own handlers run.
            if (node.classList.contains("rail")) return;
            // The Projects tab toggles by itself; closing here too would open and close it at once.
            if (node.classList.contains("projects-tab")) return;
        }
        S.railExpanded = false;
        applyRailState();
    });

    window.addEventListener("blur", () => {
        if (!S.railExpanded || S.railPinned) return;
        // A tick for the focus to settle into the frame before activeElement is read.
        setTimeout(() => {
            if (document.activeElement && document.activeElement.tagName === "IFRAME") {
                S.railExpanded = false;
                applyRailState();
            }
        }, 0);
    });
}

function togglePinned() {
    S.railPinned = !S.railPinned;
    storeSet(RAIL_PINNED_KEY, S.railPinned ? "1" : "0");
    // Pinning shows it; unpinning hides it, giving the width back in one click.
    S.railExpanded = S.railPinned;
    applyRailState();
}

// RS collapses the floating sidebar after a project switch; here after something is opened
// from it (a lesson, a project's tab, Settings, Knowledge), never after a project's lessons
// are only shown or hidden. A pinned sidebar stays.
function collapseRailAfterUse() {
    if (!S.railPinned && S.railExpanded) {
        S.railExpanded = false;
        applyRailState();
    }
}

// RS's bootstrap of these settings, before the first render. Terminal choice first: applyTheme's
// repaint reads it.
function applyInterfaceSettings() {
    S.termTheme = loadStoredTermTheme();
    applyTheme(loadStoredTheme());
    S.railPinned = loadRailPinned();
    S.railExpanded = S.railPinned;
    S.railWidth = loadRailWidth();
    applyRailWidth(S.railWidth);
    S.iframeZoom = loadIframeZoom();
    applyIframeZoomVar(S.iframeZoom);
    S.cliMaxWidth = loadCliMaxWidth();
    applyCliMaxWidthVar(S.cliMaxWidth);
    S.termFontSize = loadTermFontSize();
}

// ---- the Shell: RS's terminal -------------------------------------------------------------

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
    // RS's scheduleColumnRefit: the stored font size first (it changes the cell metrics), then the fit.
    pane.refit = () => { syncTermFontSize(pane); if (pane.fit) { try { pane.fit.fit(); } catch (_) {} } };
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
        fontSize: S.termFontSize,
        theme: currentXtermTheme(),
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

// ---- tabs side by side: RS's column layout ---------------------------------------------------
//
// RS's (webui/static/app.js at RS commit 62788a8): MAX_TAB_COLUMNS, COLUMN_MIN_FRAC,
// COLUMN_EDGE_BAND_FRAC, reconcileTabLayout, reconcileColumnRatios, moveTabInLayout,
// positionColumnChrome, positionStripGroups, installColumnSplitterDrag, armTabDrag and its drag
// chrome, lifted. A tab dragged to the side of a pane splits a new column out of it (up to three),
// onto a strip it moves there or reorders; a column whose last tab leaves closes; the splitters
// change the widths. Adapted: the layout is kept per project in the browser's storage (RS: its
// vault), with the tab each column last showed and the focused tab (RS keeps those for the
// session only); a never-arranged project's order is tabsOf's (RS: editors first); a drag holds
// the page's polls back instead of being cut by them (RS re-renders only when the tabs change);
// the frames are also covered while a drag runs. Arrangement is geometry only: a pane is never
// moved in the document (an iframe moved there reloads). Desktop only: at phone width it is one
// strip and one pane, as before.

const MAX_TAB_COLUMNS = 3;
const COLUMN_MIN_FRAC = 0.15;
const COLUMN_EDGE_BAND_FRAC = 0.2;
const TAB_DRAG_SLOP_PX = 5;
const narrowScreen = () => window.matchMedia("(max-width: 700px)").matches;
const pctOf = (f) => `${(f * 100).toFixed(4)}%`;

// The kept columns against the tabs the project has now: ids that went are dropped (kept in the
// store until the next arrangement, so an app that comes back finds its column), duplicates
// collapse, new ids join the first column's end, empty columns fold away. Never writes.
function reconcileTabLayout(stored, visibleIds) {
    const kept = stored && Array.isArray(stored.columns) ? stored.columns : [];
    const seen = new Set();
    const columns = [];
    for (const col of kept) {
        if (!Array.isArray(col)) continue;
        const ids = [];
        for (const id of col) {
            if (typeof id === "string" && visibleIds.includes(id) && !seen.has(id)) { seen.add(id); ids.push(id); }
        }
        if (ids.length) columns.push(ids);
    }
    if (columns.length > MAX_TAB_COLUMNS) {
        const extra = columns.splice(MAX_TAB_COLUMNS);
        columns[MAX_TAB_COLUMNS - 1].push(...extra.flat());
    }
    if (columns.length === 0) columns.push([]);
    for (const id of visibleIds) if (!seen.has(id)) columns[0].push(id);
    return columns;
}

// The kept widths against the column count: junk dropped, missing ones refilled, floored at
// COLUMN_MIN_FRAC, summing to one.
function reconcileColumnRatios(stored, nColumns) {
    const kept = stored && Array.isArray(stored.ratios) ? stored.ratios : [];
    const ratios = kept.filter((r) => typeof r === "number" && r > 0).slice(0, nColumns);
    while (ratios.length < nColumns) ratios.push(1 / nColumns);
    const sum = ratios.reduce((a, b) => a + b, 0);
    const floored = ratios.map((r) => Math.max(COLUMN_MIN_FRAC, r / sum));
    const sum2 = floored.reduce((a, b) => a + b, 0);
    return floored.map((r) => r / sum2);
}

// The shown project's layout, stashed as the working copy of a drag (RS's state.tabLayout).
function columnLayout(name, ids) {
    const stored = S.cols[name];
    const columns = reconcileTabLayout(stored, ids);
    S.tabLayout = { project: name, columns, ratios: reconcileColumnRatios(stored, columns.length) };
    return S.tabLayout;
}

// The tab a column shows: the focused tab when it is in the column, else the one it showed
// last, else its first.
function columnActive(name, col) {
    const ids = S.tabLayout.columns[col];
    if (ids.includes(tabOf(name))) return tabOf(name);
    const last = (S.cols[name] && S.cols[name].active) || [];
    return ids.find((id) => last.includes(id)) || ids[0];
}

// A tab becomes the one its column shows (the others of its column leave the list).
function noteActive(name, id) {
    const L = S.tabLayout && S.tabLayout.project === name ? S.tabLayout : null;
    const col = L ? L.columns.find((c) => c.includes(id)) : null;
    const entry = S.cols[name] || (S.cols[name] = { columns: [], active: [] });
    entry.active = (entry.active || []).filter((x) => x !== id && !(col && col.includes(x)));
    entry.active.push(id);
    if (entry.active.length > 32) entry.active = entry.active.slice(-32);
}

function persistTabLayout(name, columns, ratios) {
    const entry = S.cols[name] || (S.cols[name] = { columns: [], active: [] });
    entry.columns = columns.map((c) => [...c]);
    if (columns.length > 1 && Array.isArray(ratios)) entry.ratios = [...ratios];
    else delete entry.ratios;
    saveLayout();
}

// A project has one lesson tab at a time: the lesson opened takes the place of the one before.
function renameLessonTab(name, newId) {
    const entry = S.cols[name];
    if (!entry) return;
    const swap = (id) => (lessonTabId(id) ? newId : id);
    entry.columns = (entry.columns || []).map((c) => c.map(swap));
    entry.active = (entry.active || []).map(swap);
}

// Apply a drop target to the working layout {columns, ratios}. Targets:
//   {type:"strip", col, index}: insert at `index` in column `col`
//   {type:"append", col}: to column `col`'s end (a drop on the middle of its pane)
//   {type:"split", col, side}: a new column "before" or "after" column `col`, taking half its width
// True when the layout changed. An emptied column closes, its width going to a neighbour. At
// MAX_TAB_COLUMNS every split is refused.
function moveTabInLayout(layout, id, target) {
    const { columns, ratios } = layout;
    const src = columns.findIndex((c) => c.includes(id));
    if (src === -1) return false;
    const srcIdx = columns[src].indexOf(id);
    if (target.type === "strip" || target.type === "append") {
        const dst = target.col;
        if (dst < 0 || dst >= columns.length) return false;
        let index = target.type === "append" ? columns[dst].length : target.index;
        columns[src].splice(srcIdx, 1);
        if (dst === src && srcIdx < index) index -= 1;
        index = Math.max(0, Math.min(index, columns[dst].length));
        if (dst === src && index === srcIdx) { columns[src].splice(srcIdx, 0, id); return false; }
        columns[dst].splice(index, 0, id);
    } else if (target.type === "split") {
        if (columns.length >= MAX_TAB_COLUMNS) return false;
        const at = target.col + (target.side === "after" ? 1 : 0);
        // A column's only tab split beside itself makes the same shape.
        if (columns[src].length === 1 && (at === src || at === src + 1)) return false;
        columns[src].splice(srcIdx, 1);
        const half = ratios[target.col] / 2;
        ratios[target.col] = half;
        columns.splice(at, 0, [id]);
        ratios.splice(at, 0, half);
    } else {
        return false;
    }
    for (let i = columns.length - 1; i >= 0; i--) {
        if (columns[i].length) continue;
        const neighbor = i > 0 ? i - 1 : 1;
        if (ratios[neighbor] !== undefined) ratios[neighbor] += ratios[i];
        columns.splice(i, 1);
        ratios.splice(i, 1);
    }
    return true;
}

function sweepColumnChrome() {
    S.shell.panes.querySelectorAll(".column-splitter, .column-drop-overlay").forEach((n) => n.remove());
}

// Position only: the splitters, the strip's groups and the shown panes from a set of widths.
// The splitter drag runs it per frame, with no rebuild (which would drop its pointer capture).
function positionColumnChrome(ratios) {
    const lefts = [];
    let x = 0;
    for (const r of ratios) { lefts.push(x); x += r; }
    S.shell.panes.querySelectorAll(".column-splitter").forEach((s, i) => { s.style.left = `calc(${pctOf(lefts[i + 1])} - 3px)`; });
    positionStripGroups(ratios);
    for (const child of S.shell.panes.children) {
        if (child.hidden || child.dataset.col === undefined) continue;
        const col = Number(child.dataset.col);
        child.style.left = pctOf(lefts[col]);
        child.style.width = pctOf(ratios[col]);
    }
}

// Each column's tabs above its pane: the first group from the end of the chip to the first
// boundary, each further one exactly over its pane. Tabs in a narrow group shrink, their
// labels cut short.
function positionStripGroups(ratios) {
    const strip = S.shell.tabbar;
    const groups = strip.querySelectorAll(".col-group");
    if (!groups.length) return;
    const divider = strip.querySelector(".tab-group-divider");
    const chromeEnd = divider ? divider.offsetLeft + divider.offsetWidth : 0;
    let x = 0;
    groups.forEach((g, i) => {
        if (i === 0) { g.style.left = `${chromeEnd}px`; g.style.width = `calc(${pctOf(ratios[0])} - ${chromeEnd}px)`; }
        else { g.style.left = pctOf(x); g.style.width = pctOf(ratios[i]); }
        x += ratios[i];
    });
}

// A terminal measures itself again after its pane's size changed (a frame reflows by itself).
function refitVisible() {
    if (!S.shell) return;
    requestAnimationFrame(() => {
        for (const pane of Object.values(S.panes)) if (pane.refit && !pane.el.hidden) pane.refit();
    });
}

function onWindowResize() {
    const narrow = narrowScreen();
    if (narrow !== S.wasNarrow) { S.wasNarrow = narrow; render(); } else refitVisible();
}

// A drag has ended: the polls' renders held back meanwhile run now.
function endDrag() {
    S.dragging = false;
    document.body.classList.remove("col-dragging");
    if (S.renderPending) { S.renderPending = false; render(); }
}

// The splitter between columns `boundary` and `boundary + 1`: a pointer-captured drag (without
// the capture, a move over a frame goes to the frame and the drag freezes), the two widths kept
// above COLUMN_MIN_FRAC; geometry only while it moves, kept and the terminals refitted on release.
function installColumnSplitterDrag(splitter, boundary) {
    splitter.onpointerdown = (ev) => {
        const layout = S.tabLayout;
        const area = S.shell.panes;
        if (!layout || ev.button !== 0) return;
        const name = layout.project;
        ev.preventDefault();
        const pair = layout.ratios[boundary] + layout.ratios[boundary + 1];
        const onMove = (mv) => {
            const rect = area.getBoundingClientRect();
            if (rect.width <= 0) return;
            let leftEdge = 0;
            for (let i = 0; i < boundary; i++) leftEdge += layout.ratios[i];
            let share = (mv.clientX - rect.left) / rect.width - leftEdge;
            share = Math.max(COLUMN_MIN_FRAC, Math.min(pair - COLUMN_MIN_FRAC, share));
            layout.ratios[boundary] = share;
            layout.ratios[boundary + 1] = pair - share;
            positionColumnChrome(layout.ratios);
        };
        const onUp = () => {
            splitter.removeEventListener("lostpointercapture", onUp);
            document.removeEventListener("lostpointercapture", onDocLost);
            splitter.removeEventListener("pointermove", onMove);
            splitter.removeEventListener("pointerup", onUp);
            splitter.removeEventListener("pointercancel", onUp);
            try { splitter.releasePointerCapture(ev.pointerId); } catch (_) {}
            splitter.classList.remove("dragging");
            persistTabLayout(name, layout.columns, layout.ratios);
            endDrag();
            refitVisible();
        };
        const onDocLost = (lv) => { if (lv.pointerId === ev.pointerId) onUp(); };
        splitter.classList.add("dragging");
        S.dragging = true;
        document.body.classList.add("col-dragging");
        try { splitter.setPointerCapture(ev.pointerId); } catch (_) {}
        splitter.addEventListener("pointermove", onMove);
        splitter.addEventListener("pointerup", onUp);
        splitter.addEventListener("pointercancel", onUp);
        splitter.addEventListener("lostpointercapture", onUp);
        document.addEventListener("lostpointercapture", onDocLost);
    };
}

// After a drag with pointer capture, the browser makes a click on the captured tab: the tab's
// onclick swallows exactly that one.
let tabDragClickPending = false;
function consumeTabDragClick() {
    const pending = tabDragClickPending;
    tabDragClickPending = false;
    return pending;
}

// A tab's drag (desktop only): pointer-captured, never HTML5 drag-and-drop, so it goes on over
// frames. Below the slop a press is a click. The drop is worked out from the geometry: on the
// strip, before the tab whose middle is right of the pointer; over a pane, its outer bands split
// a new column out (below MAX_TAB_COLUMNS), its middle moves the tab to the column's end.
function armTabDrag(tab, name, id) {
    tab.onpointerdown = (ev) => {
        if (narrowScreen() || ev.button !== 0) return;
        if (ev.target.closest("a, button")) return;   // the ↗ keeps its click
        const strip = S.shell.tabbar;
        const area = S.shell.panes;
        const startX = ev.clientX, startY = ev.clientY;
        let dragging = false;
        let ghost = null, indicator = null, overlay = null;
        let target = null;

        const resolveTarget = (mv) => {
            const layout = S.tabLayout;
            if (!layout || layout.project !== name) return null;
            const r = strip.getBoundingClientRect();
            if (mv.clientX >= r.left && mv.clientX <= r.right && mv.clientY >= r.top && mv.clientY <= r.bottom) {
                const tabs = Array.from(strip.querySelectorAll(".tb[data-tab]"));
                if (!tabs.length) return null;
                let flatIndex = tabs.length;
                for (let i = 0; i < tabs.length; i++) {
                    const tr = tabs[i].getBoundingClientRect();
                    if (mv.clientX < tr.left + tr.width / 2) { flatIndex = i; break; }
                }
                let col, index;
                if (flatIndex < tabs.length) {
                    col = parseInt(tabs[flatIndex].getAttribute("data-column") || "0", 10);
                    index = tabs.slice(0, flatIndex).filter((t) => t.getAttribute("data-column") === String(col)).length;
                } else {
                    col = parseInt(tabs[tabs.length - 1].getAttribute("data-column") || "0", 10);
                    index = (layout.columns[col] || []).length;
                }
                return { type: "strip", col, index, tabs, flatIndex };
            }
            const ar = area.getBoundingClientRect();
            if (mv.clientX < ar.left || mv.clientX > ar.right || mv.clientY < ar.top || mv.clientY > ar.bottom) return null;
            const frac = (mv.clientX - ar.left) / ar.width;
            let x = 0, col = 0;
            for (let i = 0; i < layout.ratios.length; i++) {
                col = i;
                if (frac < x + layout.ratios[i] || i === layout.ratios.length - 1) break;
                x += layout.ratios[i];
            }
            const within = (frac - x) / layout.ratios[col];
            if (layout.columns.length < MAX_TAB_COLUMNS) {
                if (within < COLUMN_EDGE_BAND_FRAC) return { type: "split", col, side: "before" };
                if (within > 1 - COLUMN_EDGE_BAND_FRAC) return { type: "split", col, side: "after" };
            }
            return { type: "append", col };
        };

        // An insertion mark in the strip, or a tint over the pane: the whole column to move into
        // it, half of it for the new column a split makes.
        const showFeedback = (t) => {
            if (indicator) { indicator.remove(); indicator = null; }
            if (overlay) { overlay.remove(); overlay = null; }
            if (!t) return;
            if (t.type === "strip") {
                const anchor = t.tabs[Math.min(t.flatIndex, t.tabs.length - 1)];
                if (!anchor) return;
                const sr = strip.getBoundingClientRect();
                const tr = anchor.getBoundingClientRect();
                const edgeX = (t.flatIndex < t.tabs.length ? tr.left : tr.right) - sr.left + strip.scrollLeft;
                indicator = h("div", { class: "tab-drop-indicator" });
                indicator.style.left = `${edgeX}px`;
                strip.append(indicator);
            } else if (S.tabLayout) {
                const { ratios } = S.tabLayout;
                let left = 0;
                for (let i = 0; i < t.col; i++) left += ratios[i];
                let w = ratios[t.col];
                if (t.type === "split") { w /= 2; if (t.side === "after") left += w; }
                overlay = h("div", { class: "column-drop-overlay" });
                overlay.style.left = pctOf(left);
                overlay.style.width = pctOf(w);
                area.append(overlay);
            }
        };

        const onMove = (mv) => {
            if (!dragging) {
                if (Math.abs(mv.clientX - startX) < TAB_DRAG_SLOP_PX && Math.abs(mv.clientY - startY) < TAB_DRAG_SLOP_PX) return;
                dragging = true;
                S.dragging = true;
                tab.classList.add("dragging");
                document.body.classList.add("col-dragging");
                ghost = tab.cloneNode(true);
                ghost.className = "tab-drag-ghost";
                document.body.append(ghost);
            }
            ghost.style.left = `${mv.clientX + 10}px`;
            ghost.style.top = `${mv.clientY + 10}px`;
            target = resolveTarget(mv);
            showFeedback(target);
        };
        const cleanup = () => {
            // lostpointercapture first: the release below fires it at once.
            tab.removeEventListener("lostpointercapture", onCancel);
            document.removeEventListener("lostpointercapture", onDocLost);
            tab.removeEventListener("pointermove", onMove);
            tab.removeEventListener("pointerup", onUp);
            tab.removeEventListener("pointercancel", onCancel);
            try { tab.releasePointerCapture(ev.pointerId); } catch (_) {}
            if (ghost) ghost.remove();
            if (indicator) indicator.remove();
            if (overlay) overlay.remove();
            tab.classList.remove("dragging");
        };
        const onCancel = () => { cleanup(); if (dragging) endDrag(); };
        const onDocLost = (lv) => { if (lv.pointerId === ev.pointerId) onCancel(); };
        const onUp = () => {
            const t = target;
            cleanup();
            if (!dragging) return;                // a click: let it through
            tabDragClickPending = true;
            // The browser's click comes at once if it comes at all; never swallow a later one.
            setTimeout(() => { tabDragClickPending = false; }, 0);
            const layout = S.tabLayout;
            const moved = !!t && !!layout && layout.project === name
                && moveTabInLayout(layout, id, t.type === "strip" ? { type: "strip", col: t.col, index: t.index } : t);
            if (moved) persistTabLayout(name, layout.columns, layout.ratios);
            S.renderPending = false;
            endDrag();
            if (S.current !== name) return;
            // The moved tab becomes its column's shown tab and takes the focus.
            if (moved) showTab(name, id); else render();
        };
        try { tab.setPointerCapture(ev.pointerId); } catch (_) {}
        tab.addEventListener("pointermove", onMove);
        tab.addEventListener("pointerup", onUp);
        tab.addEventListener("pointercancel", onCancel);
        tab.addEventListener("lostpointercapture", onCancel);
        document.addEventListener("lostpointercapture", onDocLost);
    };
}

// ---- a web tab: the editor, an app -----------------------------------------------------------

// The address the browser used, as it goes into a URL (an IPv6 address keeps its brackets).
const appUrl = (originPort) => `https://${location.hostname}:${originPort}/`;

function makeWebPane(name, id, row) {
    const label = id === "editor" ? "Editor" : String(row.label || row.default_port);
    // No bar above the frame (RS has none): the tab carries ↗ and the refresh button. While there
    // is no frame, a note in the frame area says it is opening, or why it failed.
    const said = h("span", {}, "Opening…");
    const retry = h("button", { hidden: true }, "Try again");
    const status = h("div", { class: "frame-status" }, said, retry);
    const goneNote = h("div", { class: "gone-note", hidden: true },
        h("span", {}, "This app no longer listens in the sandbox. Its frame is kept until you close it."),
        h("button", { onclick: () => { dropPane(paneKey(name, id)); render(); } }, "Close the tab"));
    const body = h("div", { class: "frame-area" }, status,
        h("div", { class: "frame-hint" }, "If the frame stays empty, the browser has not accepted this address's certificate yet: open it in a new tab once with the ↗ on its tab, accept it there, then press the tab's refresh button."));
    const el = h("div", { class: "pane web-pane" }, goneNote, body);
    const pane = { kind: "web", el, label, url: null, iframe: null, opening: false };
    const takeFile = () => {
        const file = id === "editor" ? S.editorFile[name] : null;
        if (!file) return null;
        delete S.editorFile[name];
        return file;
    };

    async function open() {
        pane.opening = true;
        retry.hidden = true;
        status.hidden = false;
        said.textContent = "Opening…";
        said.className = "";
        const fail = (text) => { pane.opening = false; said.textContent = text; said.className = "bad"; retry.hidden = false; };
        const session = await api("POST", projectPath(name, "/session"));
        if (!session.body.ok) return fail("Could not open: " + errText(session.body));
        const origin = await api("POST", projectPath(name, "/origin"), { port: row.default_port });
        if (!origin.body.ok) return fail("Could not open: " + errText(origin.body));
        const port = origin.body.origin_port;
        if (!Number.isInteger(port) || port < 1 || port > 65535) return fail("The web server named no address for this app.");
        pane.opening = false;
        if (S.panes[paneKey(name, id)] !== pane) return;   // closed meanwhile
        pane.url = appUrl(port);
        status.hidden = true;
        pane.iframe = h("iframe", { src: editorAddress(pane.url, takeFile()), title: label, sandbox: WEB_FRAME_SANDBOX, allow: WEB_FRAME_ALLOW });
        body.append(pane.iframe);
        renderTabbar();
    }
    retry.addEventListener("click", open);
    // The tab's refresh button (RS's): reload the frame, or open again when it never opened.
    pane.refresh = () => { if (pane.iframe) pane.iframe.src = pane.url; else if (!pane.opening) open(); };
    pane.update = () => { goneNote.hidden = !!(S.services[name] || {})[id]; };
    pane.close = () => { if (pane.iframe) pane.iframe.remove(); pane.iframe = null; };
    // Opened again at the file (the editor reloads); a frame still opening takes it when it opens.
    pane.openFile = () => { if (pane.iframe) { const file = takeFile(); if (file) pane.iframe.src = editorAddress(pane.url, file); } };
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

// ---- the tutor: lessons under a project ---------------------------------------------------

const tutorPath = (tail) => `/broker/tutor${tail}`;
// The agents a lesson can be taught by, as the conversation names them.
const AGENT_NAMES = { claude: "Claude Code", pi: "pi" };

// The tutor service numbers its lessons, runs and messages; the page keeps every id as text
// (a tab's name, the layout in browser storage), so the ids of an answer become text here.
const TUTOR_ID_KEYS = new Set(["id", "lesson_id", "run_id", "running", "deleted", "current", "lesson_ids"]);
const idText = (x) => (Number.isInteger(x) ? String(x) : x);
function idsAsText(v) {
    if (Array.isArray(v)) return v.map(idsAsText);
    if (!v || typeof v !== "object") return v;
    const out = {};
    for (const [k, x] of Object.entries(v)) out[k] = !TUTOR_ID_KEYS.has(k) ? idsAsText(x) : Array.isArray(x) ? x.map(idText) : idText(x);
    return out;
}
async function tutorApi(method, tail, body) {
    const r = await api(method, tutorPath(tail), body);
    if (r.body && r.body.ok) r.body.result = idsAsText(r.body.result);
    return r;
}
// The tutor's built-in skills (`/quiz`, `/hint`, …) as a lesson's box lists them: asked of the
// tutor service once per page load, when a learner first types `/`; none when it cannot say.
let chatSkills = null;
function loadChatSkills() {
    if (!chatSkills) chatSkills = tutorApi("GET", "/skills").then((r) => {
        const list = r.body && r.body.ok && Array.isArray(r.body.result) ? r.body.result : [];
        return list.filter((s) => s && typeof s.name === "string" && s.name);
    }, () => []);
    return chatSkills;
}
const lessonTab = (id) => `lesson:${id}`;
const lessonTabId = (tab) => {
    if (typeof tab !== "string" || !tab.startsWith("lesson:")) return null;
    const id = tab.slice(7);
    return LESSON_ID_RE.test(id) ? id : null;
};
const lessonRow = (name, id) => ((S.lessons[name] && S.lessons[name].list) || []).find((l) => l.id === id) || null;

function lessonTitle(name, id) {
    const row = lessonRow(name, id);
    if (row && typeof row.title === "string" && row.title) return row.title;
    const pane = S.panes[paneKey(name, lessonTab(id))];
    return pane && pane.title ? pane.title : "Lesson";
}

// Whether a lesson's tutor is working: the activity list, the lesson's own `running`, or the
// open conversation following an answer.
function lessonWorking(name, l) {
    if (S.activity.has(l.id) || l.running) return true;
    const pane = S.panes[paneKey(name, lessonTab(l.id))];
    return !!(pane && pane.turn);
}

function toggleLessons(name) {
    if (S.expanded.has(name)) S.expanded.delete(name);
    else { S.expanded.add(name); refreshLessons(name); }
    saveLayout();
    renderRail();
}

// A project's lessons are read as its plan (GET .../plan): Goals, the modules with their
// lessons, the lessons added by hand, the dropped ones. `list` is all of them in that order.
const isLesson = (l) => l && typeof l === "object" && typeof l.id === "string";
function setPlan(name, plan) {
    const modules = (Array.isArray(plan.modules) ? plan.modules : []).filter((m) => m && typeof m === "object")
        .map((m) => ({ id: String(m.id), title: String(m.title || ""), lessons: (Array.isArray(m.lessons) ? m.lessons : []).filter(isLesson) }));
    const other = (Array.isArray(plan.other) ? plan.other : []).filter(isLesson);
    const dropped = (Array.isArray(plan.dropped) ? plan.dropped : []).filter(isLesson);
    const goals = isLesson(plan.goals) ? plan.goals : null;
    const list = [goals, ...modules.flatMap((m) => m.lessons), ...other, ...dropped].filter(Boolean);
    // Build all, while it runs: {lesson_ids, done, current}.
    const buildAll = plan.build_all && typeof plan.build_all === "object" ? plan.build_all : null;
    S.lessons[name] = { list, goals, modules, other, dropped, buildAll, error: null };
    // A project shown for the first time opens on Goals (not once its lesson tab was closed).
    if (goals && !(name in S.lessonOf)) S.lessonOf[name] = goals.id;
    // The conversations where a card waits for the learner (an open plan or change).
    S.lessons[name].waiting = new Set((Array.isArray(plan.proposals) ? plan.proposals : [])
        .map((p) => p && typeof p === "object" && p.state !== "approved" && p.state !== "dismissed" ? p.lesson_id : null).filter((x) => typeof x === "string"));
    for (const l of list) {
        const pane = S.panes[paneKey(name, lessonTab(l.id))];
        if (pane && pane.setLesson) pane.setLesson(l);
    }
}

async function refreshLessons(name) {
    if (!S.loggedIn) return;
    const r = await tutorApi("GET", `/project/${enc(name)}/plan`);
    if (!S.loggedIn) return;
    const res = r.body.ok && r.body.result;
    if (res && typeof res === "object" && Array.isArray(res.modules)) setPlan(name, res);
    else S.lessons[name] = { ...(S.lessons[name] || { list: [], modules: [], other: [], dropped: [], goals: null }), error: errText(r.body) };
    renderRail();
    renderTabbar();
}

// Goals of a project, once its lessons are read: the lesson a new project opens on.
async function openGoals(name) {
    await refreshLessons(name);
    const L = S.lessons[name];
    if (L && L.goals) { S.expanded.add(name); openLesson(name, L.goals.id); }
}

// With the page's other polls: who is working, and the lessons of the projects shown open.
async function refreshTutor() {
    if (!S.loggedIn) return;
    const r = await tutorApi("GET", "/activity");
    if (!S.loggedIn) return;
    const res = r.body.ok && r.body.result;
    if (res && Array.isArray(res.running)) {
        S.tutorError = null;
        S.activity = new Set(res.running.map((x) => x && x.lesson_id).filter((x) => typeof x === "string"));
        // An open conversation whose tutor started working elsewhere (another browser tab)
        // joins the answer.
        for (const pane of Object.values(S.panes)) {
            // A conversation that could not be read (the tutor service was away) is read again.
            if (pane.kind === "lesson" && !pane.turn && !pane.loading && !pane.el.hidden && (pane.failed || S.activity.has(pane.lessonId))) pane.reload();
        }
    } else {
        S.tutorError = errText(r.body);
        S.activity = new Set();
    }
    await Promise.all([...S.expanded].filter((n) => projectRow(n)).map(refreshLessons));
    await refreshKbOpen();
    renderRail();
}

// A lesson's state: its mark in the list (the mock's), and in words.
const LESSON_MARKS = { not_started: "○", in_progress: "▸", done: "✓", dropped: "×" };
const LESSON_STATES = { not_started: "not started", in_progress: "in progress", done: "done", dropped: "dropped" };
const lessonState = (l) => (l && LESSON_STATES[l.state] ? l.state : "not_started");
// A lesson the tutor built and the learner has not begun: "not started · built".
const lessonStateWords = (l) => LESSON_STATES[lessonState(l)] + (lessonState(l) === "not_started" && l && l.built === true ? " · built" : "");
const lessonTip = (l) => [String(l.title || ""), l.kind === "goals" ? GOALS_LINE : l.objective ? String(l.objective) : null,
    lessonStateWords(l)].filter(Boolean).join("\n");
// A lesson the tutor can still build: not Goals, not begun, not built, not dropped, no build running.
const canBuild = (l) => isLesson(l) && l.kind !== "goals" && l.state === "not_started" && l.built !== true
    && !(l.build && l.build.status === "running");
const GOALS_LINE = "What you want to learn, and the study plan";

function lessonRows(name) {
    const L = S.lessons[name];
    if (!L) return h("div", { class: "lessons" }, h("div", { class: "lnote" }, "Asking for the lessons…"));
    const shown = S.current === name && !S.page ? lessonTabId(tabOf(name)) : null;
    const row = (l) => {
        const working = lessonWorking(name, l);
        const st = lessonState(l);
        return h("div", { class: `lesson-row st-${st}` + (l.id === shown ? " sel" : ""), title: lessonTip(l), onclick: () => openLesson(name, l.id) },
            working ? h("span", { class: "pulse", title: "The tutor is working" }) : h("span", { class: `lst ${st}` }, LESSON_MARKS[st]),
            h("span", { class: "ltitle" }, String(l.title || "Lesson")),
            L.waiting && L.waiting.has(l.id) ? h("span", { class: "lwait", title: "A card waits for you here: a plan or a change to approve or dismiss" }, "●") : null,
            // Goals keeps its name and its place: no ⋯ for it.
            l.kind === "goals" ? null : h("button", { class: "gear lgear", title: "Rename, change the objective, or delete",
                                                      onclick: (ev) => { ev.stopPropagation(); lessonDialog(name, l); } }, "⋯"));
    };
    const rows = [];
    if (L.error) rows.push(h("div", { class: "lnote bad", title: L.error }, L.error));
    if (L.goals) rows.push(row(L.goals));
    for (const m of L.modules || []) {
        rows.push(h("div", { class: "lmod", title: m.title }, m.title));
        rows.push(...m.lessons.map(row));
    }
    if ((L.other || []).length && (L.modules || []).length) rows.push(h("div", { class: "lmod" }, "Other lessons"));
    rows.push(...(L.other || []).map(row));
    rows.push(...(L.dropped || []).map(row));
    rows.push(h("div", { class: "lesson-row lnew", onclick: () => newLessonDialog(name) }, "+ New lesson"));
    return h("div", { class: "lessons" }, rows);
}

function openLesson(name, id) {
    const prev = S.lessonOf[name];
    if (prev && prev !== id) dropPane(paneKey(name, lessonTab(prev)));
    renameLessonTab(name, lessonTab(id));
    S.lessonOf[name] = id;
    S.tabs[name] = lessonTab(id);
    S.page = null;
    collapseRailAfterUse();
    if (S.current !== name) { S.current = name; refreshServices(name); }
    saveLayout();
    render();
}

// The lesson tab's close button: the tab goes, the lesson stays in the list (RS's closable
// tabs). The tab shown next is another of its column's, else the Shell.
function closeLessonTab(name, id) {
    const tab = lessonTab(id);
    const col = (S.tabLayout && S.tabLayout.project === name ? S.tabLayout.columns : (S.cols[name] && S.cols[name].columns) || []).find((c) => c.includes(tab)) || [];
    dropPane(paneKey(name, tab));
    S.lessonOf[name] = null;
    if (tabOf(name) === tab) S.tabs[name] = col.find((t) => t !== tab && !lessonTabId(t)) || "shell";
    saveLayout();
    render();
}

// The lesson is not there any more (deleted here or elsewhere): its tab goes.
function lessonGone(name, id) {
    dropPane(paneKey(name, lessonTab(id)));
    if (S.lessonOf[name] === id) delete S.lessonOf[name];
    if (tabOf(name) === lessonTab(id)) S.tabs[name] = "shell";
    if (S.lessons[name]) S.lessons[name].list = S.lessons[name].list.filter((l) => l.id !== id);
    saveLayout();
    render();
}

function newLessonDialog(name) {
    const title = h("input", { type: "text", maxlength: 200, autocomplete: "off", placeholder: "Ownership and borrowing" });
    const objective = h("input", { type: "text", maxlength: 1000, autocomplete: "off", placeholder: "Explain who frees a value, and when" });
    const said = h("p", { class: "bad" });
    const go = h("button", { class: "primary", type: "submit" }, "Create");
    const form = h("form", { method: "post", action: "#", onsubmit: async (ev) => {
        ev.preventDefault();
        if (!title.value.trim()) { said.textContent = "Give the lesson a title."; return; }
        go.disabled = true;
        const body = { title: title.value.trim() };
        if (objective.value.trim()) body.objective = objective.value.trim();
        const r = await tutorApi("POST", `/project/${enc(name)}/lessons`, body);
        go.disabled = false;
        if (!r.body.ok || !r.body.result || typeof r.body.result.id !== "string") { said.textContent = errText(r.body); return; }
        closeModal();
        const L = S.lessons[name] || (S.lessons[name] = { list: [], modules: [], other: [], dropped: [], goals: null, error: null });
        if (!L.list.some((l) => l.id === r.body.result.id)) { L.list.push(r.body.result); (L.other || (L.other = [])).push(r.body.result); }
        S.expanded.add(name);
        openLesson(name, r.body.result.id);
        refreshLessons(name);
    } }, h("p", { class: "muted" }, `A new conversation with the tutor in ${name}, outside the study plan.`), formRow("Title", title),
        formRow("Objective", objective, "optional: what you can do once it is done"), said,
        h("div", { class: "btns" }, h("button", { type: "button", onclick: closeModal }, "Cancel"), go));
    openModal("New lesson", form, []);
    title.focus();
}

function lessonDialog(name, lesson) {
    const title = h("input", { type: "text", maxlength: 200, autocomplete: "off" });
    title.value = String(lesson.title || "");
    const objective = h("input", { type: "text", maxlength: 1000, autocomplete: "off" });
    objective.value = String(lesson.objective || "");
    const said = h("p", { class: "bad" });
    const save = h("button", { class: "primary", type: "submit" }, "Save");
    const form = h("form", { method: "post", action: "#", onsubmit: async (ev) => {
        ev.preventDefault();
        if (!title.value.trim()) { said.textContent = "Give the lesson a title."; return; }
        const body = { title: title.value.trim() };
        if (objective.value.trim() && objective.value.trim() !== String(lesson.objective || "")) body.objective = objective.value.trim();
        save.disabled = true;
        const r = await tutorApi("PATCH", `/lessons/${enc(lesson.id)}`, body);
        save.disabled = false;
        if (!r.body.ok) { said.textContent = errText(r.body); return; }
        closeModal();
        const row = lessonRow(name, lesson.id);
        if (row && r.body.result && typeof r.body.result.title === "string") Object.assign(row, r.body.result);
        const pane = S.panes[paneKey(name, lessonTab(lesson.id))];
        if (pane && row) pane.setLesson(row);
        render();
        refreshLessons(name);
    } }, formRow("Title", title), formRow("Objective", objective), h("div", { class: "btns" }, save));
    const del = h("button", { class: "danger" }, "Delete the lesson");
    const zone = h("div", {}, h("p", { class: "note" }, "The lesson and its conversation go, for good. The files the tutor made in the sandbox stay."), h("div", { class: "btns" }, del));
    del.addEventListener("click", () => {
        const sure = h("button", { class: "danger", onclick: async () => {
            sure.disabled = true;
            const r = await tutorApi("DELETE", `/lessons/${enc(lesson.id)}`);
            sure.disabled = false;
            if (!r.body.ok) { said.textContent = errText(r.body); return; }
            closeModal();
            lessonGone(name, lesson.id);
            refreshLessons(name);
        } }, "Delete");
        zone.replaceChildren(h("p", {}, `Delete “${lesson.title}” and its conversation?`),
            h("div", { class: "btns" }, h("button", { onclick: () => zone.replaceChildren(h("div", { class: "btns" }, del)) }, "Keep it"), sure));
    });
    openModal(`Lesson: ${lesson.title}`, [form, h("details", { class: "more danger-zone" }, h("summary", {}, "Delete"), zone), said],
        [h("button", { onclick: closeModal }, "Close")]);
    title.focus();
}

// ---- the context line (ported from gaggiclanker, web/src/lib/chatUsage.ts) ----------------

// Tokens, short: under 1000 as is ("842"), then thousands ("9.6k" below 10k with one
// decimal, "55k" above with none), then millions by the same rule ("1.5M", "12M").
function formatTokens(count) {
    if (count < 1000) return String(Math.round(count));
    const round = (v) => (v < 10 ? Math.round(v * 10) / 10 : Math.round(v));
    const thousands = round(count / 1000);
    if (thousands < 1000) return `${thousands}k`;   // 999,500 rounds to "1000k": that is a million
    return `${round(count / 1000000)}M`;
}

const usageNum = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);

// "Context 55k of 200k tokens (92% cached)": "of N" only with a window, the share only with
// the last request's cache read; null (no line) when the size is unknown or zero. A part that is
// missing is left out, never shown as zero.
function contextFooter(usage) {
    if (!usage || typeof usage !== "object") return null;
    const context = usageNum(usage.context_tokens);
    if (context === null || context <= 0) return null;   // no request was made (a refused turn)
    const win = usageNum(usage.context_window);
    let text = `Context ${formatTokens(context)}`;
    if (win !== null && win > 0) text += ` of ${formatTokens(win)}`;
    text += " tokens";
    const read = usageNum(usage.cache_read);
    if (read !== null && context > 0 && read <= context) text += ` (${Math.round((read / context) * 100)}% cached)`;
    return text;
}

// ---- the tutor's text: Markdown to elements ------------------------------------------------
//
// gaggiclanker's rules (web/src/components/chat/markdown.tsx), over markdown-it's tokens:
// no raw HTML (markdown-it with html: false, so a <script> stays text); links only with a safe
// scheme, opened in a new tab without an opener or a referrer, the words kept when the link
// is not safe; an image is never fetched and becomes a link to its address; a fenced block is
// plain text in a <pre>; a single newline is a line break; a wide table scrolls in its
// message. Only the elements named below are ever made, and only text goes into them.
// Departures, for what a conversation shows (the next section): a `mermaid`, `vega-lite` or
// `page` block, a picture and a link whose address is a relative path in the workspace.
//
// ctx: {project, blocks, draft}. `blocks` keeps the elements of diagrams, plots, pictures and
// pages across the redraws of one answer (it is redrawn at each frame while it is written);
// `draft`: the answer is still being written, so a block not closed yet stays code.

const MD = window.markdownit ? window.markdownit({ html: false, linkify: true, breaks: true, typographer: false }) : null;
const MD_BLOCK_TAGS = new Set(["p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "blockquote", "table", "thead", "tbody", "tr", "th", "td"]);

function safeHref(href) {
    if (typeof href !== "string") return null;
    let u;
    try { u = new URL(href); } catch (_) { return null; }   // relative addresses are refused too
    return u.protocol === "http:" || u.protocol === "https:" || u.protocol === "mailto:" ? u.href : null;
}

function mdRender(text, ctx) {
    const frag = document.createDocumentFragment();
    if (!MD) { frag.append(h("p", { class: "plain-text" }, text)); return frag; }
    let tokens;
    try { tokens = MD.parse(String(text), {}); } catch (_) { frag.append(h("p", { class: "plain-text" }, text)); return frag; }
    ctx = ctx ? { ...ctx, used: new Map(), lines: ctx.draft ? String(text).split("\n") : null } : null;
    const stack = [frag];
    const top = () => stack[stack.length - 1];
    for (const t of tokens) {
        if (t.type === "inline") { mdInline(top(), t.children || [], ctx); continue; }
        if (t.nesting === 1) {
            if (t.hidden) { stack.push(top()); continue; }   // a tight list's paragraph: its text goes in the item
            const tag = MD_BLOCK_TAGS.has(t.tag) ? t.tag : "div";
            const el = document.createElement(tag);
            if (tag === "ol" && t.attrGet("start") !== null) {
                const start = Number(t.attrGet("start"));
                if (Number.isInteger(start)) el.setAttribute("start", String(start));
            }
            if (tag === "th" || tag === "td") {
                const m = /text-align:\s*(left|right|center)/.exec(t.attrGet("style") || "");
                if (m) el.className = `al-${m[1]}`;
            }
            if (tag === "table") top().append(h("div", { class: "md-table" }, el));
            else top().append(el);
            stack.push(el);
        } else if (t.nesting === -1) {
            if (stack.length > 1) stack.pop();
        } else if (t.type === "fence" || t.type === "code_block") {
            top().append(fenceEl(t, ctx));
        } else if (t.type === "hr") {
            top().append(document.createElement("hr"));
        } else if (t.content) {
            top().append(h("p", {}, t.content));   // anything else (html_block cannot occur): as text
        }
    }
    return frag;
}

function mdInline(parent, children, ctx) {
    const stack = [parent];
    const top = () => stack[stack.length - 1];
    let inLink = 0;
    for (const t of children) {
        switch (t.type) {
            case "text": top().append(t.content); break;
            case "softbreak": case "hardbreak": top().append(document.createElement("br")); break;
            case "code_inline": top().append(h("code", {}, t.content)); break;
            case "strong_open": case "em_open": case "s_open": {
                const el = document.createElement(t.tag === "strong" ? "strong" : t.tag === "s" ? "s" : "em");
                top().append(el);
                stack.push(el);
                break;
            }
            case "link_open": {
                const raw = t.attrGet("href");
                const href = safeHref(raw);
                // Not a safe address: a file of the workspace (opened in the Editor), or the words only.
                const file = !href && ctx && ctx.project ? workspaceTarget(raw, true) : null;
                const el = href ? h("a", { href, target: "_blank", rel: "noopener noreferrer" })
                    : file ? fileLinkEl(ctx.project, file) : h("span", { class: "md-nolink" });
                top().append(el);
                stack.push(el);
                inLink++;
                break;
            }
            case "strong_close": case "em_close": case "s_close": case "link_close":
                if (t.type === "link_close") inLink = Math.max(0, inLink - 1);
                if (stack.length > 1) stack.pop();
                break;
            case "image": {
                // A relative path in the workspace: the picture, from the project's files server.
                const file = ctx && ctx.project && !inLink ? workspaceTarget(t.attrGet("src"), false) : null;
                if (file) {
                    top().append(kept(ctx, `img\n${file.path}\n${t.content}`, () => pictureEl(ctx.project, file, t.content)));
                    break;
                }
                // Anything else is never fetched: a link to it, its alt text as the words (inside a link, the words only).
                const label = t.content || t.attrGet("src") || "image";
                const href = safeHref(t.attrGet("src"));
                top().append(href && !inLink ? h("a", { href, target: "_blank", rel: "noopener noreferrer", class: "md-img", title: "An image, not shown here: open it in a new tab" }, `image: ${label}`)
                                            : label);
                break;
            }
            default:
                if (t.content) top().append(t.content);   // html_inline cannot occur; anything else as text
        }
    }
}

// ---- what a conversation shows: diagrams, plots, pictures, pages, files to read --------------
//
// What the tutor writes is data shown on this origin, never code run on it (a sandbox whose
// content steers its tutor decides what the tutor writes):
// - a `mermaid` block is drawn by Mermaid at its strict level, without HTML labels, and the
//   picture it makes is copied here element by element from an allow-list: no script, no
//   handler, no link, no foreign content, no address but the picture's own `#` references;
// - a `vega-lite` block is drawn by Vega with its expression interpreter (no code generated at
//   run time) and a loader that refuses every address: only the data inside the specification;
// - a picture `![alt](path)` and a `page` block name a file of the workspace by a relative path,
//   fetched from the project's files server on the project's own origin port (a page is framed
//   there as a web-app tab is): never from this origin, never from an address the tutor wrote;
// - a link to a relative path opens that file in the project's Editor.
// The libraries are loaded the first time a conversation needs one, not with the page.

const FILES_PORT = 7070;   // sandbox/files_server.py
// RS's frame for a sandbox's web app (the web-app tabs and a lesson's pages): what the apps
// need (code-server: scripts, its own origin for cookies, forms, popups, modals, downloads),
// no top navigation.
const WEB_FRAME_SANDBOX = "allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox allow-modals allow-downloads";
const WEB_FRAME_ALLOW = "fullscreen; clipboard-read; clipboard-write";

const vendorLoads = new Map();
function loadScript(file) {
    if (!vendorLoads.has(file)) {
        vendorLoads.set(file, new Promise((resolve, reject) => {
            const s = document.createElement("script");
            s.src = `/static/vendor/${file}`;
            s.async = false;
            s.onload = () => resolve();
            s.onerror = () => { vendorLoads.delete(file); s.remove(); reject(new Error(`${file} could not be loaded`)); };
            document.head.append(s);
        }));
    }
    return vendorLoads.get(file);
}
async function loadScripts(...files) { for (const f of files) await loadScript(f); }

const firstLine = (e) => String((e && e.message) || e || "unknown error").split("\n").find((l) => l.trim()) || "unknown error";

// A relative path inside the workspace as the tutor wrote it: {path, line} (line from "#L<n>"
// when withLine), or null. Refused: a scheme, a host, a leading "/" or "\", an empty segment
// ("//"), a ".." that leaves the workspace, a query, control characters.
function workspaceTarget(raw, withLine) {
    if (typeof raw !== "string" || !raw || raw.length > 1024) return null;
    let path = raw, line = null;
    const hash = raw.indexOf("#");
    if (hash >= 0) {
        const m = withLine ? /^L(\d{1,7})(?:-L?\d{1,7})?$/.exec(raw.slice(hash + 1)) : null;
        if (!m) return null;
        line = Number(m[1]) || null;
        path = raw.slice(0, hash);
    }
    if (/^[A-Za-z][A-Za-z0-9+.-]*:/.test(path) || /^[/\\]/.test(path) || /[\\?]/.test(path)) return null;
    let decoded;
    try { decoded = decodeURIComponent(path); } catch (_) { return null; }
    if (/[\x00-\x1f\x7f\\?#]/.test(decoded) || decoded.startsWith("/")) return null;
    const out = [];
    for (const seg of decoded.split("/")) {
        if (seg === "") return null;
        if (seg === ".") continue;
        if (seg === "..") { if (!out.length) return null; out.pop(); continue; }
        out.push(seg);
    }
    return out.length ? { path: out.join("/"), line } : null;
}

// The project's files server at its own origin port, as an address ending in "/": asked once
// per project (the project's session minted first, as a web tab does), asked again after a
// failure; null when the project does not run.
function filesBase(name) {
    if (!isRunning(name)) return Promise.resolve(null);
    if (!S.filesOrigins.has(name)) {
        const asked = (async () => {
            const session = await api("POST", projectPath(name, "/session"));
            if (!session.body.ok) throw new Error(errText(session.body));
            const origin = await api("POST", projectPath(name, "/origin"), { port: FILES_PORT });
            if (!origin.body.ok) throw new Error(errText(origin.body));
            const port = origin.body.origin_port;
            if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error("the web server named no address for the files");
            return appUrl(port);
        })();
        S.filesOrigins.set(name, asked);
        asked.catch(() => { if (S.filesOrigins.get(name) === asked) S.filesOrigins.delete(name); });
    }
    return S.filesOrigins.get(name);
}
const filesUrl = (base, path) => base + path.split("/").map(enc).join("/");

// The element of a diagram, plot, picture or page, made once per answer: kept in ctx.blocks
// under its kind and source, the n-th of the same in one answer as its own.
function kept(ctx, key, make) {
    if (!ctx || !ctx.blocks) return make();
    const n = ctx.used.get(key) || 0;
    ctx.used.set(key, n + 1);
    if (!ctx.blocks.has(key)) ctx.blocks.set(key, []);
    const list = ctx.blocks.get(key);
    if (!list[n]) list[n] = make();
    return list[n];
}

// A fenced block whose closing line has been written (while an answer is being written, the
// last block may not be closed yet).
function fenceClosed(t, lines) {
    if (!lines || !t.map) return true;
    const [start, end] = t.map;
    if (end - 1 <= start) return false;
    const m = /^\s*([`~]{3,})\s*$/.exec(lines[end - 1] || "");
    return !!m && m[1][0] === t.markup[0] && m[1].length >= t.markup.length;
}

const codeEl = (src) => h("pre", { class: "md-pre" }, src.replace(/\n$/, ""));
const notShown = (src, why) => h("div", { class: "md-failed" }, codeEl(src), h("div", { class: "md-why" }, why));

function fenceEl(t, ctx) {
    const src = t.content;
    const lang = t.type === "fence" ? (t.info || "").trim().split(/\s+/)[0].toLowerCase() : "";
    if (!ctx || !ctx.project || !["mermaid", "vega-lite", "page"].includes(lang)) return codeEl(src);
    if (ctx.draft && !fenceClosed(t, ctx.lines)) return codeEl(src);
    const make = lang === "mermaid" ? () => diagramEl(src) : lang === "vega-lite" ? () => plotEl(src) : () => pageEl(ctx.project, src);
    return kept(ctx, `${lang}\n${src}`, make);
}

// ---- diagrams: Mermaid at its strict level

const MERMAID_SECURE = ["secure", "securityLevel", "startOnLoad", "maxTextSize", "suppressErrorRendering", "maxEdges",
                        "htmlLabels", "flowchart", "dompurifyConfig", "themeCSS", "theme"];   // what a diagram's own directive may not change
let mermaidReady = null;
let mermaidQueue = Promise.resolve();
let mermaidSeq = 0;

function mermaidLib() {
    if (!mermaidReady) {
        mermaidReady = loadScripts("mermaid.min.js").then(() => {
            const m = window.mermaid;
            if (!m || typeof m.render !== "function") throw new Error("Mermaid did not load");
            m.initialize({ startOnLoad: false, securityLevel: "strict", htmlLabels: false, flowchart: { htmlLabels: false },
                           theme: "dark", suppressErrorRendering: true, maxTextSize: 50000, secure: MERMAID_SECURE });
            return m;
        });
        mermaidReady.catch(() => { mermaidReady = null; });
    }
    return mermaidReady;
}

function diagramEl(src) {
    const box = h("div", { class: "md-diagram" }, codeEl(src), h("div", { class: "md-why" }, "Drawing the diagram…"));
    // One at a time: Mermaid draws in a scratch element of the document.
    mermaidQueue = mermaidQueue.then(async () => {
        const id = `cl-mermaid-${++mermaidSeq}`;
        try {
            const m = await mermaidLib();
            const { svg } = await m.render(id, src);
            const pic = cleanSvg(svg);
            if (!pic) throw new Error("it made no picture");
            box.replaceChildren(pic);
        } catch (e) {
            for (const left of [document.getElementById(`d${id}`), document.getElementById(id)]) if (left) left.remove();
            box.replaceChildren(notShown(src, `This diagram could not be drawn: ${firstLine(e)}`));
        }
    });
    return box;
}

const SVG_NS = "http://www.w3.org/2000/svg";
const XLINK_NS = "http://www.w3.org/1999/xlink";
const SVG_KEEP = new Set(["svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "text", "tspan", "defs", "marker",
    "style", "title", "desc", "clipPath", "linearGradient", "radialGradient", "stop", "pattern", "mask", "symbol", "use", "filter",
    "feDropShadow", "feGaussianBlur", "feOffset", "feFlood", "feComposite", "feMerge", "feMergeNode", "feBlend", "feColorMatrix", "feMorphology"]);
// CSS that could make the browser fetch something: any url() but a "#" reference, @import,
// image-set(), and escapes (which could spell either).
const cssFetches = (css) => /url\s*\(|@import|image-set\s*\(|\\/i.test(css.replace(/url\(\s*(['"]?)#[\w:.-]*\1\s*\)/g, ""));

// Mermaid's picture, rebuilt from an inert parse: only the elements above, no on* attribute,
// href only to "#", no attribute or style that names an address. null when there is no <svg>.
function cleanSvg(markup) {
    const doc = new DOMParser().parseFromString(String(markup), "text/html");   // inert: nothing in it runs or loads
    const root = doc.querySelector("svg");
    if (!root) return null;
    const copy = (src) => {
        if (src.nodeType === Node.TEXT_NODE) return document.createTextNode(src.data);
        if (src.nodeType !== Node.ELEMENT_NODE || src.namespaceURI !== SVG_NS) return null;
        if (src.localName === "a") {   // a link: its content without the link
            const frag = document.createDocumentFragment();
            for (const c of src.childNodes) { const k = copy(c); if (k) frag.append(k); }
            return frag;
        }
        if (!SVG_KEEP.has(src.localName)) return null;
        const el = document.createElementNS(SVG_NS, src.localName);
        if (src.localName === "style") {
            if (cssFetches(src.textContent)) return null;
            el.textContent = src.textContent;
            return el;
        }
        for (const a of src.attributes) {
            const n = a.name.toLowerCase();
            if (n.startsWith("on") || n.startsWith("xmlns") || cssFetches(a.value)) continue;
            if (n === "href" || n === "xlink:href") {
                if (a.value.startsWith("#")) el.setAttributeNS(n === "href" ? null : XLINK_NS, n, a.value);
                continue;
            }
            try { el.setAttribute(a.name, a.value); } catch (_) { /* a name the DOM refuses: left out */ }
        }
        for (const c of src.childNodes) { const k = copy(c); if (k) el.append(k); }
        return el;
    };
    return copy(root);
}

// ---- plots: Vega-Lite, the data inside the specification only

const PLOT_CONFIG = {   // for the page's dark background; a specification's own config wins
    background: "transparent",
    view: { stroke: "#555" },
    axis: { labelColor: "#bbb", titleColor: "#ddd", gridColor: "#3a3a3a", domainColor: "#777", tickColor: "#777" },
    legend: { labelColor: "#bbb", titleColor: "#ddd" },
    header: { labelColor: "#bbb", titleColor: "#ddd" },
    title: { color: "#e0e0e0", subtitleColor: "#aaa" },
};
let vegaReady = null;

function vegaLib() {
    if (!vegaReady) {
        vegaReady = loadScripts("vega.min.js", "vega-lite.min.js", "vega-interpreter.js").then(() => {
            const { vega, vegaLite, vegaInterpreter } = window;
            if (!vega || !vegaLite || !vegaInterpreter) throw new Error("Vega did not load");
            return { vega, vegaLite, interpreter: vegaInterpreter.expressionInterpreter };
        });
        vegaReady.catch(() => { vegaReady = null; });
    }
    return vegaReady;
}

// A loader that loads nothing: no data file, no picture, no link followed. Each address it
// was asked for is told to onRefuse.
function refusingLoader(vega, onRefuse) {
    const loader = vega.loader();
    const refuse = (uri) => {
        onRefuse(typeof uri === "string" ? uri : "an address");
        return Promise.reject(new Error("a plot in a conversation loads nothing"));
    };
    loader.load = loader.sanitize = loader.http = loader.file = refuse;
    return loader;
}

// A control bound to an element elsewhere on the page ("bind": {"element": selector}) would be
// put there: every control stays under its plot.
function unbindElsewhere(v) {
    if (Array.isArray(v)) { v.forEach(unbindElsewhere); return; }
    if (!v || typeof v !== "object") return;
    if (v.bind && typeof v.bind === "object") delete v.bind.element;
    Object.values(v).forEach(unbindElsewhere);
}

let plotTip = null;
function plotTooltip(handler, event, item, value) {
    if (!plotTip) { plotTip = h("div", { class: "plot-tip", hidden: true }); document.body.append(plotTip); }
    if (value === null || value === undefined || value === "") { plotTip.hidden = true; return; }
    const say = (v) => (v !== null && typeof v === "object" ? JSON.stringify(v) : String(v));
    fill(plotTip, typeof value === "object" && !Array.isArray(value)
        ? Object.entries(value).map(([k, v]) => h("div", {}, h("span", { class: "k" }, k), " ", say(v)))
        : h("div", {}, say(value)));
    plotTip.hidden = false;
    const x = Math.min(event.clientX + 14, window.innerWidth - plotTip.offsetWidth - 8);
    const y = event.clientY + 14 + plotTip.offsetHeight > window.innerHeight ? event.clientY - plotTip.offsetHeight - 10 : event.clientY + 14;
    plotTip.style.left = `${Math.max(4, x)}px`;
    plotTip.style.top = `${Math.max(4, y)}px`;
}

function plotEl(src) {
    const box = h("div", { class: "md-plot" }, codeEl(src), h("div", { class: "md-why" }, "Drawing the plot…"));
    const failed = (why) => box.replaceChildren(notShown(src, `This plot could not be drawn: ${why}`));
    let view = null;
    // An address asked for, now or later (a picture mark asks when it is drawn): the plot goes.
    const onRefuse = (uri) => {
        if (view) { view.finalize(); view = null; }
        failed(`it names an address to load (${uri.slice(0, 120)}), and a plot here shows only the data inside its specification.`);
    };
    (async () => {
        let spec;
        try { spec = JSON.parse(src); } catch (e) { throw new Error(`its specification is not JSON (${firstLine(e)})`); }
        if (!spec || typeof spec !== "object" || Array.isArray(spec)) throw new Error("its specification is not a JSON object");
        unbindElsewhere(spec);
        const { vega, vegaLite, interpreter } = await vegaLib();
        const compiled = vegaLite.compile(spec, { config: PLOT_CONFIG }).spec;
        let refused = false;
        const loader = refusingLoader(vega, (uri) => { refused = true; onRefuse(uri); });
        const holder = h("div", { class: "md-plot-view" });
        view = new vega.View(vega.parse(compiled, null, { ast: true }), {
            expr: interpreter, loader, logLevel: vega.Error, renderer: "canvas", container: holder, hover: true, tooltip: plotTooltip });
        await view.runAsync();
        if (!refused && view) box.replaceChildren(holder);
    })().catch((e) => { if (view) { view.finalize(); view = null; } failed(firstLine(e)); });
    return box;
}

// ---- pictures and pages: files of the workspace, from the project's own origin

function pictureEl(name, file, alt) {
    const box = h("span", { class: "md-picture" });
    const said = (why) => box.replaceChildren(h("span", { class: "md-gone" }, `picture ${file.path}: ${why}`));
    if (!isRunning(name)) { said("the project is stopped"); return box; }
    box.append(h("span", { class: "md-gone" }, `picture ${file.path}…`));
    filesBase(name).then((base) => {
        if (!base) return said("the project is stopped");
        const url = filesUrl(base, file.path);
        const img = h("img", { alt: alt || file.path, title: alt || file.path, referrerpolicy: "no-referrer" });
        img.addEventListener("load", () => box.replaceChildren(h("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, img)));
        img.addEventListener("error", () => said(isRunning(name) ? "not found in the workspace" : "the project is stopped"));
        img.src = url;
    }, (e) => said(`not shown (${firstLine(e)})`));
    return box;
}

function pageEl(name, src) {
    const lines = src.split("\n").map((l) => l.trim()).filter(Boolean);
    const file = lines.length === 1 ? workspaceTarget(lines[0], false) : null;
    if (!file || !/\.html?$/i.test(file.path)) return notShown(src, "This page is not shown: a page block holds one relative path to an .html file in /workspace.");
    const said = h("span", { class: "grow" }, "Opening…");
    const link = h("a", { class: "newtab", target: "_blank", rel: "noopener noreferrer", hidden: true }, "open in a new tab ↗");
    const area = h("div", { class: "md-page-area" });
    const box = h("div", { class: "md-page" }, h("div", { class: "md-page-bar" }, h("code", {}, file.path), said, link), area);
    const stopped = () => { said.textContent = ""; area.replaceChildren(h("div", { class: "md-gone" }, "the project is stopped: its page is shown while it runs")); };
    if (!isRunning(name)) { stopped(); return box; }
    filesBase(name).then((base) => {
        if (!base) return stopped();
        const url = filesUrl(base, file.path);
        link.href = url;
        link.hidden = false;
        said.textContent = "";
        area.replaceChildren(h("iframe", { src: url, title: file.path, sandbox: WEB_FRAME_SANDBOX, allow: WEB_FRAME_ALLOW }));
    }, (e) => { said.textContent = `not shown (${firstLine(e)})`; });
    return box;
}

// ---- a file to read: opened in the project's Editor

// A project without the editor shows the path after the words instead (class noeditor, set
// again when its services are known: markFileLinks).
function fileLinkEl(name, file) {
    const where = `/workspace/${file.path}${file.line ? `:${file.line}` : ""}`;
    const el = h("a", { class: "md-file", href: "#", "data-path": where, "data-project": name, title: `Open ${where} in the Editor`,
                        onclick: (ev) => { ev.preventDefault(); openInEditor(name, file); } });
    const rows = S.services[name];
    if (rows && !rows.editor) el.classList.add("noeditor");
    return el;
}

function markFileLinks(name) {
    const none = !(S.services[name] || {}).editor;
    for (const a of document.querySelectorAll("a.md-file")) {
        if (a.dataset.project === name) a.classList.toggle("noeditor", none);
    }
}

function openInEditor(name, file) {
    if (!(S.services[name] || {}).editor) { toast(`This project has no editor. The file is /workspace/${file.path}`); return; }
    S.editorFile[name] = file;
    if (S.current !== name) selectProject(name);
    const pane = S.panes[paneKey(name, "editor")];
    if (pane && pane.openFile) pane.openFile();
    showTab(name, "editor");
}

// code-server (VS Code for the web) opens a file named in its address: `payload` holds
// ["openFile", <the file's vscode-remote URI>], and with ["gotoLineMode", "true"] a ":<line>"
// at its end is the line to show (VS Code's workbench, filesToOpenOrCreate).
function editorAddress(base, file) {
    if (!file) return base;
    const authority = new URL(base).host;
    const uri = `vscode-remote://${authority}/workspace/${file.path.split("/").map(enc).join("/")}${file.line ? `:${file.line}` : ""}`;
    const payload = [["openFile", uri]];
    if (file.line) payload.push(["gotoLineMode", "true"]);
    return `${base}?folder=${enc("/workspace")}&payload=${enc(JSON.stringify(payload))}`;
}

// ---- the stream of an answer (ported from gaggiclanker, web/src/lib/sse.ts) ----------------
//
// Server-sent events over fetch and a stream reader. A 401 ends it (the login is gone); every
// other failure is retried with a back-off from 1 s doubling up to 15 s, and the back-off starts
// again once a stream opens. The web server does not pass Last-Event-ID on, so each attempt
// asks with its own address (url(), built from the last event seen); beforeRetry() may end it.

const STREAM_BASE_DELAY_MS = 1000;
const STREAM_MAX_DELAY_MS = 15000;
const SSE_FRAME_SEPARATOR = /\r\n\r\n|\n\n|\r\r/;   // CRLF, LF and CR, as the format allows

function takeFrame(buffer) {
    const m = SSE_FRAME_SEPARATOR.exec(buffer);
    return m ? { frame: buffer.slice(0, m.index), rest: buffer.slice(m.index + m[0].length) } : null;
}

function parseSseFrame(frame) {
    const data = [];
    let event = "message", id;
    for (const line of frame.split(/\r?\n/)) {
        if (line.startsWith(":")) continue;   // a comment: the keep-alive ping
        if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
        else if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("id:")) id = line.slice(3).trim();
    }
    return data.length ? { event, data: data.join("\n"), id } : null;
}

function abortableDelay(ms, signal) {
    return new Promise((resolve) => {
        if (signal.aborted) return resolve();
        const timer = setTimeout(resolve, ms);
        signal.addEventListener("abort", () => { clearTimeout(timer); resolve(); }, { once: true });
    });
}

// handlers: onEvent(data), onOpen(), onTrouble(status, body) (each failed attempt; status 0
// is no answer or a dropped stream), onGone() (404: there is no such answer), beforeRetry()
// (async; false ends it). Returns the function that closes it.
function subscribeStream(url, handlers) {
    const controller = new AbortController();
    let closed = false;
    (async () => {
        let wait = STREAM_BASE_DELAY_MS;
        let first = true;
        while (!closed) {
            if (!first) {
                await abortableDelay(wait, controller.signal);
                wait = Math.min(wait * 2, STREAM_MAX_DELAY_MS);
                if (closed) return;
                if (handlers.beforeRetry && !(await handlers.beforeRetry())) return;
                if (closed) return;
            }
            first = false;
            try {
                const res = await fetch(url(), { credentials: "same-origin", cache: "no-store", headers: { Accept: "text/event-stream" }, signal: controller.signal });
                const type = res.headers.get("Content-Type") || "";
                if (!res.ok || !res.body || !type.startsWith("text/event-stream")) {
                    let body = null;
                    try { body = await res.json(); } catch (_) { /* not JSON */ }
                    if (res.status === 401) { loggedOut("Your login has ended. Log in again."); return; }
                    if (res.status === 404) { if (handlers.onGone) handlers.onGone(); return; }
                    if (handlers.onTrouble) handlers.onTrouble(res.status, body);
                    continue;
                }
                wait = STREAM_BASE_DELAY_MS;
                if (handlers.onOpen) handlers.onOpen();
                const decoder = new TextDecoder();
                const reader = res.body.getReader();
                let buffer = "";
                try {
                    while (!closed) {
                        const { done, value } = await reader.read();
                        if (done) break;
                        buffer += decoder.decode(value, { stream: true });
                        let next = takeFrame(buffer);
                        while (next && !closed) {
                            buffer = next.rest;
                            const parsed = parseSseFrame(next.frame);
                            if (parsed) {
                                let data = null;
                                try { data = JSON.parse(parsed.data); } catch (_) { /* a malformed frame must not end the stream */ }
                                if (data && typeof data === "object") handlers.onEvent(data);
                            }
                            next = takeFrame(buffer);
                        }
                    }
                } finally {
                    try { await reader.cancel(); } catch (_) { /* already closed */ }
                }
                if (!closed && handlers.onTrouble) handlers.onTrouble(0, null);
            } catch (_) {
                if (closed || controller.signal.aborted) return;
                if (handlers.onTrouble) handlers.onTrouble(0, null);
            }
        }
    })();
    return () => { closed = true; controller.abort(); };
}

// ---- a lesson's conversation (after the mock's lesson view) ----------------------------------

const TOOL_WORDS = { shell: "ran", read: "read", write: "wrote", edit: "edited", search: "searched" };

// A tool the tutor used, as the mock's trace line: a tick or a cross (… while it works), the
// label, and what it gave when opened.
// The tutor's own tools (kind "learn"), in plain words.
const LEARN_WORDS = { plan_get: "looked at the plan", lesson_get: "looked at the lesson", lesson_history: "read the lessons done so far",
                      lesson_close: "closed the lesson", plan_propose: "proposed a study plan", plan_change_propose: "proposed a change to the plan",
                      brief_get: "read the brief", records_search: "searched what it knows about you",
                      insight_record: "recorded an insight", insight_replace: "replaced an insight", insight_retire: "retired an insight",
                      preference_record: "recorded a preference", goal_record: "recorded a goal",
                      global_insight_propose: "proposed a global insight", exercise_define: "set an exercise",
                      exercise_check: "ran the exercise's check", quiz_ask: "asked a quick question", answer_grade: "graded your answer" };

// What was sent to a "learn" tool, readable: its JSON arguments indented, or as they came.
function learnDetail(detail) {
    if (typeof detail !== "string" || !detail || detail === "{}") return "";
    try { return JSON.stringify(JSON.parse(detail), null, 2); } catch (_) { return detail; }
}

function toolLine(tool, ended) {
    const mark = h("span", {});
    const pre = h("pre", {});
    const learn = tool.kind === "learn";
    const label = String(tool.label || tool.kind || "tool");
    const el = h("details", { class: "trace" + (learn ? " learn" : "") },
        h("summary", {}, mark, " ", TOOL_WORDS[tool.kind] ? h("span", { class: "tkind" }, TOOL_WORDS[tool.kind] + " ") : null,
            learn ? h("span", { class: "tlearn" }, LEARN_WORDS[label] || `used its tool ${label}`) : h("span", { class: "tlabel" }, label)),
        pre);
    const note = h("span", { class: "tnote" });
    el.querySelector("summary").append(note);
    // unfinished: no result will come (the answer ended while it ran: stopped, failed, or the
    // tutor service restarted); shown as such, not as still running.
    const set = (ok, output, unfinished) => {
        const never = (ok === null || ok === undefined) && !!unfinished;
        line.pending = (ok === null || ok === undefined) && !never;
        mark.className = ok === true ? "okc" : ok === false ? "badc" : never ? "warnc" : "pendc";
        mark.textContent = ok === true ? "✓" : ok === false ? "✗" : never ? "–" : "…";
        note.textContent = never ? " — did not finish" : "";
        const detail = learn ? learnDetail(tool.detail) : typeof tool.detail === "string" && tool.detail && tool.detail !== tool.label ? tool.detail : "";
        const out = typeof output === "string" ? output : "";
        if (learn) {
            pre.textContent = [`Sent: ${detail || "nothing"}`, out ? `Answer: ${out}` : never ? "(not finished: the answer ended while this was running)" : line.pending ? "Still working…" : null]
                .filter(Boolean).join("\n\n");
            return;
        }
        pre.textContent = [detail, out || (never ? "(not finished: the answer ended while this was running)" : null)].filter(Boolean).join("\n\n")
            || (line.pending ? "Still working…" : "(no output)");
    };
    const line = { el, set, pending: false };
    set(tool.ok, tool.output, tool.unfinished === true || ended);
    return line;
}

// ---- a proposal: the plan card and the change card (after the mock's planHtml) --------------
//
// A plan is modules of lessons, each a title and an objective. While the card is open the
// learner edits it in place (titles, objectives, lessons removed, added, moved; modules renamed,
// removed, added); Approve sends their version ({} when nothing was changed), Dismiss sends
// nothing. A change names the project's existing lessons by id and new ones by title and
// objective; an existing lesson it leaves out is dropped. Once decided the card is read-only and
// says what happened. The card keeps its edits across the redraws of its conversation: there is
// one card per proposal id, made once and moved into each new drawing of its message.

const cleanText = (s) => String(s || "").trim();
const copyModules = (modules) => (Array.isArray(modules) ? modules : []).filter((m) => m && typeof m === "object").map((m) => ({
    title: String(m.title || ""),
    lessons: (Array.isArray(m.lessons) ? m.lessons : []).filter((l) => l && typeof l === "object").map((l) => ({
        id: l.id === undefined || l.id === null ? null : String(l.id), title: String(l.title || ""), objective: String(l.objective || ""),
        state: typeof l.state === "string" ? l.state : null })) }));
// What is sent for a plan's or a change's modules: an existing lesson by its id.
const wireModules = (modules) => modules.map((m) => ({ title: cleanText(m.title), lessons: m.lessons.map((l) => l.id
    ? { id: Number(l.id), title: cleanText(l.title), objective: cleanText(l.objective) }
    : { title: cleanText(l.title), objective: cleanText(l.objective) }) }));

function proposalCard(name, p) {
    const card = { p: null, draft: null, said: "", busy: false, sure: false };
    const el = h("div", { class: "blk plan" });
    card.el = el;
    const isChange = () => card.p.kind === "change";
    const open = () => card.p.state === "open";

    // The project's lessons a change knows of: those it places and those it drops.
    const known = () => {
        const all = new Map();
        for (const m of copyModules(card.p.proposed && card.p.proposed.modules)) for (const l of m.lessons) if (l.id) all.set(l.id, l);
        for (const d of (card.p.proposed && Array.isArray(card.p.proposed.dropped) ? card.p.proposed.dropped : [])) {
            if (d && d.id !== undefined) all.set(String(d.id), { id: String(d.id), title: String(d.title || ""), objective: "", state: d.state });
        }
        return all;
    };
    const droppedOf = (modules) => {
        const placed = new Set(modules.flatMap((m) => m.lessons.map((l) => l.id)).filter(Boolean));
        return [...known().values()].filter((l) => !placed.has(l.id));
    };

    // An objective's box grows with its text (field-sizing does it where the browser has it).
    const autosize = (ta) => {
        if (!ta.isConnected || CSS.supports("field-sizing", "content")) return;
        ta.style.height = "auto";
        if (ta.scrollHeight) ta.style.height = `${ta.scrollHeight + 2}px`;
    };
    // A title is one line that wraps (Enter does not break it); an objective may have several.
    const edit = (cls, value, placeholder, onInput, multi) => {
        const area = cls !== "pm";
        const input = area ? h("textarea", { class: cls, rows: 1, placeholder, maxlength: multi ? 1000 : 200, "aria-label": placeholder })
                           : h("input", { type: "text", class: cls, placeholder, maxlength: 200, "aria-label": placeholder });
        if (area && !multi) input.addEventListener("keydown", (ev) => { if (ev.key === "Enter") ev.preventDefault(); });
        input.value = value;
        input.addEventListener("input", () => { onInput(input.value); if (area) autosize(input); });
        if (area) { requestAnimationFrame(() => autosize(input)); input.addEventListener("focus", () => autosize(input)); }
        return input;
    };
    const small = (text, title, onclick, disabled) => h("button", { type: "button", class: "mini", title, "aria-label": title, disabled: !!disabled, onclick }, text);
    let focusNext = null;   // a selector inside the card to focus after the next drawing
    const redraw = (focus) => { focusNext = focus || null; draw(); };

    function moveLesson(mi, li, dir) {
        const mods = card.draft;
        const [l] = mods[mi].lessons.splice(li, 1);
        if (dir < 0) {
            if (li > 0) mods[mi].lessons.splice(li - 1, 0, l);
            else mods[mi - 1].lessons.push(l);           // up from a module's first place: to the end of the module above
        } else if (li < mods[mi].lessons.length) mods[mi].lessons.splice(li + 1, 0, l);
        else mods[mi + 1].lessons.unshift(l);            // down from its last place: to the start of the module below
        redraw();
    }

    function lessonEl(l, mi, li, editing) {
        const mods = editing ? card.draft : null;
        const num = h("span", { class: "pnum" }, `${mi + 1}.${li + 1}`);
        const isNew = isChange() && !l.id;
        const mark = isChange() ? (isNew ? h("span", { class: "pnew", title: "A new lesson" }, "new")
            : h("span", { class: `lst ${lessonState(l)}`, title: lessonStateWords(l) }, LESSON_MARKS[lessonState(l)])) : null;
        if (!editing) {
            return h("div", { class: "plan-les" + (isNew ? " is-new" : "") }, num,
                h("span", { class: "ptitle" }, mark, mark ? " " : null, l.title),
                h("span", { class: "pobj" }, l.objective || ""));
        }
        const first = mi === 0 && li === 0;
        const last = mi === mods.length - 1 && li === mods[mi].lessons.length - 1;
        return h("div", { class: "plan-les editing" + (isNew ? " is-new" : ""), "data-at": `${mi}.${li}` }, num,
            h("span", { class: "ptitle" }, mark, edit("pt", l.title, "the lesson's title", (v) => { l.title = v; })),
            h("span", { class: "pobj" }, edit("po", l.objective, "its objective: what you can do once it is done", (v) => { l.objective = v; }, true)),
            h("span", { class: "pact" },
                small("↑", "Move up", () => moveLesson(mi, li, -1), first),
                small("↓", "Move down", () => moveLesson(mi, li, 1), last),
                small("✕", "Remove this lesson", () => { mods[mi].lessons.splice(li, 1); redraw(); })));
    }

    function modulesEl(modules, editing) {
        return modules.map((m, mi) => h("div", { class: "plan-modblock" },
            editing
                ? h("div", { class: "plan-mod editing", "data-mod": String(mi) },
                    edit("pm", m.title, "the module's title", (v) => { m.title = v; }),
                    small("✕", "Remove this module and its lessons", () => { card.draft.splice(mi, 1); redraw(); }))
                : h("div", { class: "plan-mod" }, m.title),
            m.lessons.map((l, li) => lessonEl(l, mi, li, editing)),
            editing ? h("div", { class: "plan-add" }, h("button", { type: "button", class: "link", onclick: () => {
                m.lessons.push({ id: null, title: "", objective: "", state: null });
                redraw(`[data-at="${mi}.${m.lessons.length - 1}"] .pt`);
            } }, "+ Add a lesson")) : null,
            editing && !m.lessons.length ? h("div", { class: "plan-warn" }, "This module has no lesson: add one or remove the module.") : null));
    }

    function droppedEl(modules, editing) {
        if (!isChange()) return null;
        const gone = droppedOf(modules);
        if (!gone.length) return null;
        return h("div", { class: "plan-dropped" },
            h("div", { class: "plan-mod" }, open() ? "Would be dropped" : "Dropped"),
            gone.map((l) => h("div", { class: "plan-les dropped" + (editing ? " editing" : "") }, h("span", { class: "pnum" }, ""),
                h("span", { class: "ptitle" }, h("s", {}, l.title)),
                h("span", { class: "pobj" }, l.state && l.state !== "not_started"
                    ? `${LESSON_STATES[lessonState(l)]}: kept in the list, marked dropped` : "never started: removed"),
                editing ? h("span", { class: "pact" }, h("button", { type: "button", class: "mini wide", title: "Keep this lesson in the plan", onclick: () => {
                    if (!card.draft.length) card.draft.push({ title: "", lessons: [] });
                    // A dropped lesson comes with no objective: the project's own row has it.
                    const row = lessonRow(name, l.id);
                    card.draft[card.draft.length - 1].lessons.push({ ...l, objective: l.objective || (row && row.objective ? String(row.objective) : "") });
                    redraw();
                } }, "Keep it")) : null)));
    }

    const changed = () => JSON.stringify(wireModules(card.draft)) !== JSON.stringify(wireModules(copyModules(card.p.proposed && card.p.proposed.modules)));

    async function decide(verb) {
        if (card.busy) return;
        let body = {};
        if (verb === "approve" && changed()) {
            const empty = card.draft.find((m) => !m.lessons.length);
            if (empty) { card.said = `The module “${cleanText(empty.title) || "without a title"}” has no lesson: add one or remove the module.`; draw(); return; }
            body = { modules: wireModules(card.draft) };
        }
        card.busy = true;
        card.said = "";
        draw();
        const r = await tutorApi("POST", `/proposals/${enc(card.p.id)}/${verb}`, body);
        card.busy = false;
        const res = r.body.ok && r.body.result;
        if (!res || !res.proposal) {
            card.said = errText(r.body);
            // Decided elsewhere (another browser tab): show it as it is now.
            const again = await tutorApi("GET", `/proposals/${enc(card.p.id)}`);
            if (again.body.ok && again.body.result && again.body.result.proposal) card.update(again.body.result.proposal);
            draw();
            return;
        }
        card.update(res.proposal);
        if (res.plan && typeof res.plan === "object") { setPlan(name, res.plan); renderRail(); renderTabbar(); }
        else refreshLessons(name);
    }

    function draw() {
        const p = card.p;
        const editing = open();
        const decided = p.state === "approved" ? "approved" : p.state === "dismissed" ? "dismissed" : null;
        const what = isChange() ? (decided === "approved" ? "Change to the plan" : "Proposed change to the plan")
                                : (decided === "approved" ? "Study plan" : "Proposed study plan");
        const modules = editing ? card.draft : copyModules(decided === "approved" && p.approved ? p.approved.modules : p.proposed && p.proposed.modules);
        const why = p.proposed && typeof (isChange() ? p.proposed.reason : p.proposed.note) === "string" ? (isChange() ? p.proposed.reason : p.proposed.note) : "";
        const count = modules.reduce((n, m) => n + m.lessons.length, 0);
        let foot;
        if (editing) {
            foot = [h("span", { class: "grow" }, `${count} lesson${count === 1 ? "" : "s"} in ${modules.length} module${modules.length === 1 ? "" : "s"}`,
                        h("button", { type: "button", class: "link addmod", onclick: () => {
                            card.draft.push({ title: "", lessons: [{ id: null, title: "", objective: "", state: null }] });
                            redraw(`[data-mod="${card.draft.length - 1}"] .pm`);
                        } }, "+ Add a module")),
                    h("button", { type: "button", disabled: card.busy, onclick: () => decide("dismiss") }, "Dismiss"),
                    h("button", { type: "button", class: "primary", disabled: card.busy, onclick: () => decide("approve") }, card.busy ? "Sending…" : "Approve")];
        } else if (decided === "approved") {
            const changes = (Array.isArray(p.changes) ? p.changes : []).filter((c) => typeof c === "string" && c);
            foot = [h("div", { class: "grow" },
                h("div", { class: "okc" }, changes.length ? "Approved, with your changes:" : "Approved as proposed."),
                changes.length ? h("ul", { class: "plan-changes" }, changes.map((c) => h("li", {}, `You ${c}${/[.!?]$/.test(c) ? "" : "."}`))) : null),
                isChange() ? null : buildAllEl()];
        } else {
            foot = [h("span", { class: "grow" }, "Dismissed. Nothing was changed.")];
        }
        fill(el,
            h("div", { class: "blk-head" }, h("span", {}, what),
                h("span", { class: "grow" + (decided === "approved" ? " okc" : "") },
                    decided === "approved" ? "✓ approved" : decided === "dismissed" ? "dismissed" : "waiting for you")),
            why ? h("div", { class: "plan-note" }, why) : null,
            editing ? h("div", { class: "plan-hint" }, "Change anything below before you approve it: the tutor is told what you changed.") : null,
            modulesEl(modules, editing),
            droppedEl(modules, editing),
            card.said ? h("div", { class: "plan-said bad" }, card.said) : null,
            h("div", { class: "blk-foot" }, foot),
            decided ? h("div", { class: "plan-told" }, "The tutor is told with your next message in this conversation.") : null);
        if (focusNext) { const f = el.querySelector(focusNext); if (f) f.focus(); focusNext = null; }
        // While Build all runs and the card is shown, the plan is read again (which draws the card again).
        const L = S.lessons[name];
        if (decided === "approved" && !isChange() && L && L.buildAll && !card.poll) {
            card.poll = setTimeout(() => { card.poll = null; if (el.isConnected && el.getClientRects().length) refreshLessons(name); }, BUILD_ALL_EVERY_MS);
        }
    }

    // Build all, on the approved plan: the lessons not begun, built nor dropped, one at a time in plan order.
    function buildAllEl() {
        const L = S.lessons[name];
        if (!L || !L.goals) return null;
        const q = L.buildAll;
        if (q) {
            const n = Array.isArray(q.lesson_ids) ? q.lesson_ids.length : 0;
            const cur = typeof q.current === "string" ? lessonRow(name, q.current) : null;
            return h("div", { class: "build-all" },
                h("span", { class: "grow" }, `Building ${Math.min((Number.isInteger(q.done) ? q.done : 0) + 1, n)} of ${n}`, cur ? `: “${String(cur.title || "")}”` : ""),
                h("button", { type: "button", disabled: card.building, onclick: () => buildAll("build-all/stop") }, "Stop"));
        }
        if (![...L.modules.flatMap((m) => m.lessons), ...L.other].some(canBuild)) return null;
        return h("div", { class: "build-all" },
            h("span", { class: "grow" }, "Builds the lessons not started yet, one at a time, in plan order"),
            h("button", { type: "button", class: "primary", disabled: card.building, onclick: () => buildAll("build-all") }, card.building ? "Sending…" : "Build all"));
    }
    async function buildAll(tail) {
        const L = S.lessons[name];
        if (card.building || !L || !L.goals) return;
        card.building = true;
        card.said = "";
        draw();
        const r = await tutorApi("POST", `/lessons/${enc(L.goals.id)}/${tail}`);
        card.building = false;
        const res = r.body.ok && r.body.result;
        if (!res || typeof res !== "object" || !("build_all" in res)) card.said = errText(r.body);
        else if (S.lessons[name]) S.lessons[name].buildAll = res.build_all && typeof res.build_all === "object" ? res.build_all : null;
        draw();
        refreshLessons(name);
    }
    // The plan read again (the lessons, Build all): an approved plan's foot follows it.
    card.planChanged = () => { if (card.p.state === "approved" && !isChange()) draw(); };

    // A newer state of the proposal: an open card keeps the learner's edits.
    card.update = (np) => {
        if (!np || typeof np !== "object") return;
        const was = card.p;
        card.p = np;
        if (np.state === "open" && (!was || !card.draft)) card.draft = copyModules(np.proposed && np.proposed.modules);
        if (!was || was.state !== np.state) draw();
    };
    card.update(p);
    return card;
}

// ---- an exercise and a quick question (the mock's exercise and quiz blocks) ------------------
//
// The tutor sets them; the learner acts on them in place. A check runs the exercise's command in
// the sandbox (up to half a minute); an answer is one of the options. When no answer is being
// written the action goes to the tutor as a message of its own, written by the system; while one
// is, the card changes at once and the tutor is told with the learner's next message.

// "I ran the check of …" as the learner sees it: what happened, not words they typed.
const composedWords = (text) => String(text || "").replace(/^I /, "You ");
const LETTERS = "ABCDEFGH";
const letterOf = (i) => (Number.isInteger(i) && i >= 0 && i < LETTERS.length ? LETTERS[i] : String(i));

// What the learner did on a card, sent: the card as it is now, and the message that followed (or
// the line saying the tutor hears of it with the next message).
async function cardAction(c, what, body, host, said) {
    const asMessage = !host.busy();
    const r = await tutorApi("POST", `/cards/${enc(c.id)}/${what}`, { ...body, as_message: asMessage });
    const res = r.body.ok && r.body.result;
    if (!res || !res.card) { said.className = "card-said bad"; said.textContent = errText(r.body); said.hidden = false; return null; }
    if (res.sent) { said.hidden = true; host.begun(res.sent); }
    else { said.className = "card-said note"; said.textContent = "The tutor is told with your next message."; said.hidden = false; }
    return res.card;
}

function exerciseCard(name, c, host) {
    const w = { c: null, busy: false };
    const el = h("div", { class: "blk exercise" });
    const said = h("div", { class: "card-said", hidden: true });
    w.el = el;
    const draw = () => {
        const x = w.c;
        const last = x.last_check && typeof x.last_check === "object" ? x.last_check : null;
        const check = x.check && typeof x.check === "object" ? x.check : {};
        const state = x.state === "passed" ? h("span", { class: "bh-state okc" }, "✓ passed")
            : x.state === "failed" ? h("span", { class: "bh-state badc" }, last && last.timed_out ? "✗ did not finish in time" : "✗ failed")
            : h("span", { class: "bh-state" }, "not checked yet");
        const btn = h("button", { class: "primary", disabled: w.busy, onclick: async () => {
            w.busy = true;
            draw();
            const card = await cardAction(w.c, "check", {}, host, said);
            w.busy = false;
            if (card) w.c = card;
            draw();
        } }, w.busy ? "Checking… (up to half a minute)" : x.state === "unchecked" ? "Check" : "Check again");
        const result = last ? h("details", { class: "ex-result", open: x.state === "failed" },
            h("summary", {}, `Last check, by ${last.by === "learner" ? "you" : "the tutor"}, ${fmtWhen(last.at)}: `,
                last.timed_out ? "it did not finish in time" : `exit ${last.exit_code}`, Number.isFinite(last.seconds) ? ` · ${last.seconds} s` : null),
            h("pre", {}, String(last.output || "").trim() ? String(last.output) : "(no output)"),
            last.output_cut ? h("div", { class: "note" }, "Only the end of the output is kept.") : null) : null;
        fill(el,
            h("div", { class: "blk-head" }, h("span", { class: "bh-title" }, "◎ ", String(x.title || "Exercise")), state),
            h("ol", { class: "ex-steps" }, (Array.isArray(x.steps) ? x.steps : []).map((st) => h("li", { class: "md" }, mdRender(String(st), { project: name, blocks: null })))),
            h("div", { class: "ex-check" }, "Check: ", h("code", {}, String(check.command || "")),
                Number.isInteger(check.expect_exit) && check.expect_exit !== 0 ? ` (passes with exit ${check.expect_exit})` : null),
            result,
            h("div", { class: "blk-foot" }, h("span", { class: "grow" }, "The check runs in your sandbox; what it gives back is kept."), btn),
            said);
    };
    w.update = (c2) => { w.c = c2; if (!w.busy) draw(); };
    w.update(c);
    return w;
}

function questionCard(name, c, host) {
    const w = { c: null, picked: null, busy: false };
    const el = h("div", { class: "blk quiz" });
    const said = h("div", { class: "card-said", hidden: true });
    w.el = el;
    const draw = () => {
        const x = w.c;
        const answered = x.state === "answered" && x.answer && typeof x.answer === "object";
        const choice = answered ? x.answer.choice : w.picked;
        const rightAt = answered ? x.right_choice : null;
        const options = (Array.isArray(x.options) ? x.options : []).map((o, i) => {
            let cls = "qopt";
            if (choice === i) cls += " picked";
            if (answered) cls += i === rightAt ? " right" : i === choice ? " wrong" : "";
            return h("button", { class: cls, type: "button", disabled: answered || w.busy, onclick: () => { w.picked = i; draw(); } },
                h("span", { class: "qdot" }), h("span", { class: "qletter" }, letterOf(i)), h("span", { class: "qtext" }, String(o)));
        });
        const go = h("button", { class: "primary", disabled: w.picked === null || w.busy, onclick: async () => {
            w.busy = true;
            draw();
            const card = await cardAction(w.c, "answer", { choice: w.picked }, host, said);
            w.busy = false;
            if (card) w.c = card;
            draw();
        } }, w.busy ? "Sending…" : "Answer");
        const foot = answered
            ? h("div", { class: "blk-foot qfoot" }, h("div", { class: `grow ${x.answer.right ? "okc" : "badc"}` },
                x.answer.right ? "✓ Right." : `✗ Not quite: the right answer is ${letterOf(rightAt)}.`),
                typeof x.explanation === "string" && x.explanation.trim()
                    ? h("div", { class: "qexpl md" }, mdRender(x.explanation, { project: name, blocks: null })) : null)
            : h("div", { class: "blk-foot" }, h("span", { class: "grow" }, "Your answer is kept, right or wrong."), go);
        fill(el,
            h("div", { class: "blk-head" }, h("span", { class: "bh-title" }, "? Quick question"),
                h("span", { class: "bh-state" + (answered ? (x.answer.right ? " okc" : " badc") : "") },
                    answered ? (x.answer.right ? "✓ answered right" : "✗ answered wrong") : "waiting for your answer")),
            h("div", { class: "qq md" }, mdRender(String(x.question || ""), { project: name, blocks: null })),
            h("div", { class: "qopts" }, options), foot, said);
    };
    w.update = (c2) => { w.c = c2; if (!w.busy) draw(); };
    w.update(c);
    return w;
}

function makeLessonPane(name, id) {
    const titleEl = h("span", { class: "lt" }, lessonTitle(name, id));
    const objEl = h("span", { class: "lo" });
    const markEl = h("span", { class: "lstate" });
    const summaryBtn = h("button", { class: "mini wide", hidden: true, title: "What the tutor wrote when it closed the lesson" }, "Summary");
    // The lesson's folder in the workspace (its transcript, the tutor's notes), in the Editor.
    const filesBtn = h("button", { class: "mini wide", hidden: true }, "Files");
    const stateEl = h("span", { class: "lw" });
    // The agent that teaches the lesson (the mock's select): the default, or one the project has.
    const agentSel = h("select", { class: "lagent", title: "The agent that teaches this lesson. It can be changed at any time.",
                                   "aria-label": "The agent that teaches this lesson" });
    const inner = h("div", { class: "conv-inner" });
    const conv = h("div", { class: "conv" }, inner);
    const ctx = h("div", { class: "ctxline", hidden: true });
    const said = h("div", { class: "composer-said", hidden: true });
    const ta = h("textarea", { rows: 2, placeholder: "Write to the tutor…", "aria-label": "Your message to the tutor" });
    const btn = h("button", { class: "primary" }, "Send");
    // The skills list, above the box (see drawSkills).
    const skillsEl = h("div", { class: "skills", role: "listbox", "aria-label": "Skills", hidden: true });
    const el = h("div", { class: "pane lesson-pane" },
        h("div", { class: "lhead" }, h("div", { class: "lh-main" }, titleEl, objEl), stateEl, agentSel, markEl, filesBtn, summaryBtn),
        conv, ctx,
        h("div", { class: "composer" }, said, h("div", { class: "composer-inner" }, h("div", { class: "composer-box" }, skillsEl, ta), btn)));
    const pane = { kind: "lesson", lessonId: id, el, title: null, lesson: null, turn: null, stale: true, loading: false, sending: false };
    const tools = new Map();   // tool id -> its line, so a result fills the line its call made
    const cards = new Map();   // proposal id -> its card, kept across redraws (with the learner's edits)
    const cardOf = (p) => {
        if (!p || typeof p !== "object" || typeof p.id !== "string") return null;
        let c = cards.get(p.id);
        if (c) c.update(p); else { c = proposalCard(name, p); cards.set(p.id, c); }
        return c;
    };
    // An exercise or a quick question, by its id: one card each, updated in place.
    const tasks = new Map();
    const host = {
        busy: () => !!(pane.turn || pane.sending),
        // The action sent as a message of its own: shown, and its answer followed.
        begun: (sent) => { if (sent && sent.run && typeof sent.run.id === "string" && !pane.turn) begun(sent); },
    };
    const taskOf = (c) => {
        if (!c || typeof c !== "object" || typeof c.id !== "string" || (c.kind !== "exercise" && c.kind !== "question")) return null;
        let w = tasks.get(c.id);
        if (w) w.update(c); else { w = c.kind === "exercise" ? exerciseCard(name, c, host) : questionCard(name, c, host); tasks.set(c.id, w); }
        return w;
    };
    summaryBtn.addEventListener("click", () => {
        const l = pane.lesson;
        if (!l) return;
        openModal(`Summary: ${l.title}`, [
            l.one_line ? h("p", { class: "sum-line" }, String(l.one_line)) : null,
            h("div", { class: "sum-text" }, String(l.summary || "The tutor wrote no summary.")),
            l.closed_at ? h("p", { class: "note" }, `Closed by the tutor ${fmtWhen(l.closed_at)}. The line above is what later lessons' tutors are given.`) : null,
        ], [h("button", { onclick: closeModal }, "Close")]);
    });
    // The transcript in the lesson's folder, as a file link opens a workspace file in the Editor.
    const transcriptOf = (l) => {
        const folder = l && typeof l.folder === "string" && l.folder.startsWith("/workspace/") ? l.folder.slice("/workspace/".length) : null;
        return folder ? workspaceTarget(`${folder}/transcript.md`, false) : null;
    };
    filesBtn.addEventListener("click", () => { const file = transcriptOf(pane.lesson); if (file) openInEditor(name, file); });
    // The lesson as last read: its title, objective, state and summary in the header.
    pane.setLesson = (l) => {
        if (!isLesson(l)) return;
        pane.lesson = l;
        const file = transcriptOf(l);
        const p = projectRow(name);
        filesBtn.hidden = !(file && p && p.editor);
        filesBtn.title = file ? `Open /workspace/${file.path} in the Editor: the lesson's folder holds its transcript and the tutor's notes` : "";
        if (typeof l.title === "string" && l.title) pane.setTitle(l.title);
        const goals = l.kind === "goals";
        objEl.textContent = goals ? GOALS_LINE : String(l.objective || "");
        objEl.title = objEl.textContent;
        const st = lessonState(l);
        markEl.className = `lstate ${st}`;
        markEl.textContent = (st === "done" ? "✓ " : "") + lessonStateWords(l);
        summaryBtn.hidden = !(st === "done" && (l.summary || l.one_line));
        ta.placeholder = goals ? "Tell the tutor what you want to learn…" : "Write to the tutor…";
        if (goals) for (const c of cards.values()) c.planChanged();
        pane.drawAgent();
    };
    // The choices: "the default (…)" and the agents the project has (the tutor service does not
    // know which a project has); a choice the project no longer has stays shown, as it is in effect.
    let agentKey = "";
    pane.drawAgent = () => {
        const l = pane.lesson;
        const p = projectRow(name);
        const have = (p && Array.isArray(p.agents) ? p.agents : []).filter((a) => AGENT_NAMES[a]);
        const own = l && AGENT_NAMES[l.agent] ? l.agent : "";
        if (own && !have.includes(own)) have.push(own);
        // A lesson that has started without choosing stays with the agent that began it
        // (agent_source "begun"); only one that has not started follows the default.
        const begun = l && !own && l.agent_source === "begun" && AGENT_NAMES[l.agent_in_effect] ? l.agent_in_effect : null;
        const settingsDefault = S.tutorSettings && AGENT_NAMES[S.tutorSettings.default_agent] ? S.tutorSettings.default_agent : null;
        const dflt = begun ? settingsDefault : l && !own && AGENT_NAMES[l.agent_in_effect] ? l.agent_in_effect : settingsDefault;
        const key = [own, begun, dflt, ...have].join("|");
        if (key === agentKey) return;
        agentKey = key;
        agentSel.replaceChildren(
            begun ? h("option", { value: "begun" }, `${AGENT_NAMES[begun]}, which began this lesson`) : null,
            h("option", { value: "" }, dflt ? `the default (${AGENT_NAMES[dflt]})` : "the default agent"),
            ...have.map((a) => h("option", { value: a }, AGENT_NAMES[a])));
        agentSel.value = begun ? "begun" : own;
    };
    agentSel.addEventListener("change", async () => {
        if (agentSel.value === "begun") return;   // what is in effect already
        const want = agentSel.value || null;
        agentSel.disabled = true;
        const r = await tutorApi("PATCH", `/lessons/${enc(id)}`, { agent: want });
        agentSel.disabled = false;
        const res = r.body.ok && r.body.result;
        if (!isLesson(res)) { agentKey = ""; pane.drawAgent(); tell(errText(r.body)); return; }
        const row = lessonRow(name, id);
        if (row) Object.assign(row, res);
        pane.setLesson(res);
        const now = AGENT_NAMES[res.agent_in_effect] || "the default agent";
        if (inner.querySelector(".msg")) sysline(`From your next message ${now} teaches this lesson, taking it up from where it is.`);
    });
    let follow = true;         // the conversation follows the newest text unless scrolled up

    // Scrolled up is above where the page last put it: text that grew or reflowed under a
    // following conversation (a narrower column, a plot drawn late) does not end the following.
    let putAt = 0;
    conv.addEventListener("scroll", () => { follow = conv.scrollTop >= putAt - 2 || conv.scrollHeight - conv.scrollTop - conv.clientHeight < 40; });
    const toBottom = () => { if (follow) { conv.scrollTop = conv.scrollHeight; putAt = conv.scrollTop; } };
    // The text grows or reflows without a scroll (its column made narrower, a plot drawn late):
    // a conversation that followed the newest text goes on following it.
    if (window.ResizeObserver) new ResizeObserver(() => { if (!el.hidden) toBottom(); }).observe(inner);

    // A refusal (busy, a failed Stop) stays until the answer ends or the next message; a note
    // about the connection goes as soon as the connection is back.
    let trouble = false;
    const tell = (text) => { trouble = false; said.textContent = text || ""; said.hidden = !text; };
    const tellTrouble = (text) => { tell(text); trouble = true; };
    const clearTrouble = () => { if (trouble) tell(""); };
    pane.setTitle = (t) => { pane.title = t; titleEl.textContent = t; };
    // The figures come as each request of an answer ends (`usage`), the window with the turn's
    // last line only: the figures sent while it is written keep the one known before (this
    // lesson's, else the project's last known), so the line does not change its form on the way.
    const setUsage = (usage) => {
        if (usage && typeof usage === "object") {
            if (usageNum(usage.context_window)) S.ctxWindow[name] = usage.context_window;
            else if (S.ctxWindow[name]) usage = { ...usage, context_window: S.ctxWindow[name] };
        }
        const text = contextFooter(usage);
        if (text) { ctx.textContent = text; ctx.hidden = false; }
    };
    const setComposer = () => {
        const busy = !!pane.turn;
        btn.textContent = busy ? "Stop" : "Send";
        btn.className = busy ? "danger" : "primary";
        btn.title = busy ? "Stop the tutor's answer" : "Send (Enter; Shift+Enter for a new line)";
        stateEl.textContent = busy ? "the tutor is working…" : "";
    };
    const syslineEl = (text, cls) => h("div", { class: "sysline" + (cls ? ` ${cls}` : "") }, h("span", {}, text));
    const sysline = (text, cls) => { inner.append(syslineEl(text, cls)); toBottom(); };
    const emptyNote = () => inner.querySelector(".empty");

    // blocks: the drawn elements of the answer this message was being written as, kept.
    // ended: the message's answer is over, so a tool in it without a result never finished.
    function messageEl(m, blocks, ended) {
        const you = m.role === "user";
        const body = h("div", { class: you ? "body" : "body md" });
        const traces = h("div", { class: "traces" });
        for (const t of (Array.isArray(m.tools) ? m.tools : [])) {
            if (!t || typeof t !== "object") continue;
            const line = toolLine(t, !!ended);
            if (typeof t.id === "string") tools.set(t.id, line);
            traces.append(line.el);
        }
        traces.hidden = !traces.childElementCount;
        const cardsEl = h("div", { class: "cards" });
        for (const p of (Array.isArray(m.proposals) ? m.proposals : [])) {
            const c = cardOf(p);
            if (c) cardsEl.append(c.el);
        }
        for (const c of (Array.isArray(m.cards) ? m.cards : [])) {
            const w = taskOf(c);
            if (w) cardsEl.append(w.el);
        }
        cardsEl.hidden = !cardsEl.childElementCount;
        // A message the system wrote for the learner (a card's check or answer sent as a
        // message): what happened, in a quiet line in their place, not as typed words.
        const composed = you && m.composed === true;
        if (composed) body.textContent = composedWords(m.text);
        else if (you) body.textContent = String(m.text || "");
        else body.append(mdRender(String(m.text || ""), { project: name, blocks: blocks || null }));
        // A stretch of the answer with no text (only tools, or a card) has no empty text box.
        body.hidden = !you && !String(m.text || "").trim();
        // What the tutor was told before the learner's words (the lesson's opening, a
        // proposal's outcome): not theirs, so only behind a quiet line that opens.
        const told = you && typeof m.context === "string" && m.context.trim()
            ? h("details", { class: "told" }, h("summary", {}, "what the tutor was told"), h("pre", {}, m.context)) : null;
        if (composed) body.className = "body composed-line";
        const el = h("div", { class: `msg ${you ? "you" : "tutor"}${composed ? " composed" : ""}`, "data-run": m.run_id === undefined || m.run_id === null ? null : String(m.run_id),
                              "data-agent": !you && AGENT_NAMES[m.agent] ? m.agent : null },
            h("div", { class: "av" }, you ? "Y" : "T"),
            // The tools a stretch of the answer used come after its text, as they were run; its cards after them.
            h("div", { class: "mc" }, h("div", { class: "who" }, you ? "You" : "Tutor"), body, told, traces, cardsEl));
        return { el, body, traces, cards: cardsEl };
    }
    // "Tutor · pi", "Tutor · Claude Code" once more than one agent has answered in the lesson.
    function markWho() {
        const msgs = [...inner.querySelectorAll(".msg.tutor[data-agent]")];
        const several = new Set(msgs.map((e) => e.dataset.agent)).size > 1;
        for (const e of msgs) {
            const who = e.querySelector(".who");
            if (who) who.textContent = several ? `Tutor · ${AGENT_NAMES[e.dataset.agent]}` : "Tutor";
        }
    }

    // ---- an answer being written
    function draftOf(turn) {
        if (turn.draft) return turn.draft;
        const d = messageEl({ role: "assistant", text: "", agent: turn.agent });
        d.text = "";
        d.toolIds = [];
        d.blocks = new Map();
        // The mark that it is being written comes last, under the tools it runs.
        d.tail = h("div", {}, h("span", { class: "pulse typing", title: "The tutor is working" }));
        d.el.querySelector(".mc").append(d.tail);
        inner.append(d.el);
        markWho();
        turn.draft = d;
        return d;
    }
    function paintDraft(turn) {
        if (turn.paintQueued) return;
        turn.paintQueued = true;
        requestAnimationFrame(() => {
            turn.paintQueued = false;
            const d = turn.draft;
            if (!d) return;
            d.body.replaceChildren(mdRender(d.text, { project: name, blocks: d.blocks, draft: true }));
            d.body.hidden = !d.text.trim();
            toBottom();
        });
    }
    // ended: the answer came to its end (not only left): the lesson and the list are read again,
    // since the tutor may have closed the lesson (no event says so).
    function endTurn(turn, ended) {
        if (pane.turn !== turn) return;
        turn.close();
        pane.turn = null;
        const d = turn.draft;
        if (d) {
            if (!d.text && !d.toolIds.length && !d.cards.childElementCount) d.el.remove();
            else { d.body.replaceChildren(mdRender(d.text, { project: name, blocks: d.blocks })); d.body.hidden = !d.text.trim(); d.tail.remove(); }
        }
        turn.draft = null;
        // The answer is over: a tool line still waiting for its result will not get one.
        if (ended) for (const line of tools.values()) if (line.pending) line.set(null, "", true);
        tell("");
        S.activity.delete(id);
        const row = lessonRow(name, id);
        if (row) row.running = null;
        setComposer();
        renderRail();
        if (ended) {
            refreshLessons(name);
            // The tutor may have recorded an insight: an Insights tab shown beside is read again.
            const ip = S.panes[paneKey(name, "insights")];
            if (ip) { ip.stale = true; if (!ip.el.hidden) ip.update(); }
            if (S.page === "knowledge") { S.kb.changes = null; renderKnowledge(); }
        }
    }
    function onEvent(turn, ev) {
        if (pane.turn !== turn || !Number.isInteger(ev.seq) || ev.seq <= turn.lastSeq) return;   // seen already
        turn.lastSeq = ev.seq;
        clearTrouble();
        const d = ev.data && typeof ev.data === "object" ? idsAsText(ev.data) : {};
        switch (ev.kind) {
            case "proposal": {
                // A card arrives while the answer is written; the message that follows holds it.
                const c = cardOf(d.proposal);
                if (c) { const draft = draftOf(turn); draft.cards.append(c.el); draft.cards.hidden = false; toBottom(); }
                break;
            }
            case "exercise":
            case "question": {
                // Made, or changed (the tutor's check; the learner's action on it): a card shown
                // already is updated where it is, a new one goes with the answer being written.
                const w = taskOf(d.card);
                if (w && !w.el.isConnected) { const draft = draftOf(turn); draft.cards.append(w.el); draft.cards.hidden = false; toBottom(); }
                break;
            }
            case "text_delta":
                if (typeof d.text === "string") { draftOf(turn).text += d.text; paintDraft(turn); }
                break;
            case "tool_call": {
                const draft = draftOf(turn);
                const line = toolLine({ ...d, ok: null, output: "" });
                if (typeof d.id === "string") { tools.set(d.id, line); draft.toolIds.push(d.id); }
                draft.traces.append(line.el);
                draft.traces.hidden = false;
                toBottom();
                break;
            }
            case "tool_result": {
                const line = tools.get(d.id);
                if (line) line.set(d.ok === true ? true : d.ok === false ? false : null, d.output, d.unfinished === true);
                break;
            }
            case "message": {
                // The stored message replaces what was streamed; tool lines it does not hold yet
                // stay in a new draft.
                const m = d.message;
                if (!m || typeof m !== "object") break;
                const draft = turn.draft;
                // The cards the draft shows that this message does not hold stay in the next draft.
                const leftCards = draft ? [...draft.cards.children] : [];
                const stored = messageEl(m, draft && draft.blocks);
                const have = new Set((Array.isArray(m.tools) ? m.tools : []).map((t) => t && t.id));
                turn.draft = null;
                if (draft) draft.el.replaceWith(stored.el); else inner.append(stored.el);
                markWho();
                const left = draft ? draft.toolIds.filter((t) => !have.has(t)) : [];
                const cardsLeft = leftCards.filter((c) => !stored.cards.contains(c));
                if (left.length || cardsLeft.length) {
                    const next = draftOf(turn);
                    for (const t of left) { next.traces.append(tools.get(t).el); next.toolIds.push(t); }
                    next.traces.hidden = !left.length;
                    for (const c of cardsLeft) next.cards.append(c);
                    next.cards.hidden = !cardsLeft.length;
                }
                toBottom();
                break;
            }
            case "usage": setUsage(d.usage); break;
            case "completed": setUsage(d.usage); endTurn(turn, true); if (turn.build) pane.reload(); break;
            case "error":
                endTurn(turn, true);
                if (turn.build) pane.reload(); else sysline(typeof d.message === "string" && d.message ? d.message : "The tutor's answer failed.", "bad");
                break;
            case "cancelled": endTurn(turn, true); if (turn.build) pane.reload(); else sysline("stopped"); break;
            default: break;
        }
    }

    // Follow an answer from after `after`: replayed first, then as it is written.
    // build: the run builds the lesson; once it ends the lesson is read again, to fold it.
    function join(runId, after, agent, build) {
        const turn = { runId, agent, build: !!build, lastSeq: after, draft: null, paintQueued: false, close: () => {} };
        pane.turn = turn;
        setComposer();
        draftOf(turn);
        turn.close = subscribeStream(() => tutorPath(`/runs/${enc(runId)}/stream?after=${turn.lastSeq}`), {
            onEvent: (ev) => onEvent(turn, ev),
            onOpen: clearTrouble,
            onTrouble: (status, body) => tellTrouble(status ? `${errText(body)} Trying again…` : "The connection to the tutor dropped. Trying again…"),
            onGone: () => { if (pane.turn === turn) { endTurn(turn); pane.reload(); } },
            // Before asking again: is this answer still being written? If not, show the lesson as stored.
            beforeRetry: async () => {
                const r = await tutorApi("GET", `/lessons/${enc(id)}`);
                if (pane.turn !== turn) return false;
                if (r.status === 404) { endTurn(turn); lessonGone(name, id); return false; }
                if (!r.body.ok || !r.body.result) return true;
                const run = r.body.result.run;
                if (run && run.id === runId && run.status === "running") return true;
                endTurn(turn);
                pane.reload();
                return false;
            },
        });
    }

    async function load() {
        if (pane.loading) return;
        pane.loading = true;
        if (pane.turn) endTurn(pane.turn);
        const r = await tutorApi("GET", `/lessons/${enc(id)}`);
        pane.loading = false;
        if (S.panes[paneKey(name, lessonTab(id))] !== pane) return;   // closed meanwhile
        if (pane.el.hidden) { pane.stale = true; return; }            // left meanwhile
        pane.stale = false;
        if (r.status === 404) { toast("That lesson is not there any more.", true); lessonGone(name, id); return; }
        const res = r.body.ok && r.body.result;
        pane.failed = !res || !res.lesson;
        if (pane.failed) {
            ctx.hidden = true;
            inner.replaceChildren(h("div", { class: "empty" }, h("p", { class: "bad" }, errText(r.body)),
                h("button", { onclick: () => { pane.stale = true; pane.update(); } }, "Try again")));
            return;
        }
        if (isLesson(res.lesson)) {
            const row = lessonRow(name, id);
            if (row) Object.assign(row, res.lesson);
            pane.setLesson(res.lesson);
            renderTabbar();
        }
        const run = res.run && res.run.status === "running" ? res.run : null;
        const msgs = (Array.isArray(res.messages) ? res.messages : []).filter((m) => m && typeof m === "object");
        const ended = (Array.isArray(res.ended) ? res.ended : []).filter((e) => e && typeof e === "object");
        const l = pane.lesson;
        // A build that ended is one block: its messages, by run, folded there (a running one is
        // shown as any answer, and folded once it ends).
        const builds = new Map();
        for (const m of msgs) {
            if (m.build !== true || m.run_id === undefined || m.run_id === null || (run && m.run_id === run.id)) continue;
            const k = String(m.run_id);
            if (!builds.has(k)) builds.set(k, []);
            builds.get(k).push(m);
        }
        tools.clear();
        inner.replaceChildren();
        let others = false;   // a message outside the builds is shown
        // A running answer is rebuilt from its first event: what it stored so far is left out here.
        for (const m of msgs) {
            if (run && m.role === "assistant" && m.run_id === run.id) continue;
            const k = m.run_id === undefined || m.run_id === null ? null : String(m.run_id);
            if (k !== null && builds.has(k)) {
                if (builds.get(k)[0] === m) {
                    const e = ended.find((x) => String(x.id) === k);
                    const status = l && l.build && l.build.run_id === k ? l.build.status : e ? e.status : "ok";
                    inner.append(buildEl(builds.get(k), status, e && e.status === "failed" ? e.error : null));
                }
                continue;
            }
            others = true;
            inner.append(messageEl(m, null, true).el);
        }
        // An answer that did not end well says so after its last part, as it did while it was written.
        for (const e of ended) {
            if (builds.has(String(e.id))) continue;   // said in its build's block
            const parts = inner.querySelectorAll(`.msg[data-run="${CSS.escape(String(e.id))}"]`);
            if (!parts.length) continue;
            parts[parts.length - 1].after(e.status === "cancelled" ? syslineEl("stopped")
                : syslineEl(typeof e.error === "string" && e.error ? e.error : "The tutor's answer failed.", "bad"));
        }
        if (!inner.childElementCount && !run) {
            inner.append(h("div", { class: "empty" }, l && l.kind === "goals"
                ? [h("p", {}, "Tell the tutor what you want to learn."),
                   h("p", { class: "note" }, "It asks what you want to be able to do, what you know already and how much time you have, then proposes a study plan.")]
                : [l && l.objective ? h("p", {}, String(l.objective)) : null, h("p", { class: "note" }, "Write to the tutor to begin."), buildOffer()]));
        } else if (builds.size && !others && !run && l && l.state === "not_started") {
            // Built, not begun: the learner's first message continues the build's session.
            inner.append(h("div", { class: "empty after-build" }, h("p", { class: "note" }, "Write to the tutor to begin."), buildOffer()));
        }
        ctx.hidden = true;
        setUsage(res.usage);
        follow = true;
        markWho();
        if (run) {
            S.activity.add(id);
            join(run.id, 0, run.agent, msgs.some((m) => m.run_id === run.id && m.build === true) || !!(l && l.build && l.build.run_id === run.id));
        } else setComposer();
        renderRail();
        // At once as well: the scroll the emptying above made would otherwise end the following.
        toBottom();
        requestAnimationFrame(toBottom);
    }

    async function send() {
        const text = ta.value;
        if (!text.trim() || pane.turn || pane.sending || pane.loading) return;
        pane.sending = true;
        btn.disabled = true;
        tell("");
        const r = await tutorApi("POST", `/lessons/${enc(id)}/messages`, { text });
        pane.sending = false;
        btn.disabled = false;
        const res = r.body.ok && r.body.result;
        if (!res || !res.run || typeof res.run.id !== "string") {
            tell(errText(r.body));
            // Busy: an answer is being written already (from another browser tab): follow it.
            if (r.body.error && r.body.error.kind === "busy" && !pane.turn) load();
            return;
        }
        ta.value = "";
        closeSkills();
        tell("");
        begun(res);
    }

    // A message stored and its answer started: shown, and the answer followed.
    function begun(res) {
        const empty = emptyNote();
        if (empty) empty.remove();
        if (res.message) inner.append(messageEl(res.message).el);
        follow = true;
        S.activity.add(id);
        join(res.run.id, 0, res.run.agent, !!(res.message && res.message.build === true));
        renderRail();
        toBottom();
    }

    // Build: the tutor sets the lesson up before the learner begins (no teaching); it stays not started.
    function buildOffer() {
        if (!canBuild(pane.lesson)) return null;
        const b = h("button", { class: "primary", onclick: () => build(b) }, "Build");
        return h("div", { class: "build-offer" }, b, h("p", { class: "note" }, "The tutor sets up what this lesson needs before you start."));
    }
    async function build(b) {
        if (pane.turn || pane.sending || pane.loading) return;
        pane.sending = true;
        b.disabled = btn.disabled = true;
        tell("");
        const r = await tutorApi("POST", `/lessons/${enc(id)}/build`);
        pane.sending = false;
        b.disabled = btn.disabled = false;
        const res = r.body.ok && r.body.result;
        if (!res || !res.run || typeof res.run.id !== "string") {
            tell(errText(r.body));
            if (r.body.error && r.body.error.kind === "busy" && !pane.turn) load();
            return;
        }
        begun(res);
    }

    // A build that ended, folded: its outcome, its last text in full, and its steps behind a line that opens.
    function buildEl(msgs, status, error) {
        const last = [...msgs].reverse().find((m) => m.role === "assistant" && String(m.text || "").trim());
        const has = (m, k) => Array.isArray(m[k]) && m[k].length;
        const steps = h("details", { class: "build-steps" }, h("summary", {}, "the build's steps"));
        for (const m of msgs) {
            // The last text is shown above; its tools and cards stay here, in their place.
            if (m === last && !has(m, "tools") && !has(m, "proposals") && !has(m, "cards")) continue;
            steps.append(messageEl(m === last ? { ...m, text: "" } : m, null, true).el);
        }
        const ok = status === "ok";
        const label = ok ? "✓ Built" : status === "failed" ? "Build failed" : "Build stopped";
        return h("div", { class: "blk build-fold" },
            h("div", { class: "blk-head" }, h("span", { class: ok ? "okc" : "badc" }, label)),
            h("div", { class: "build-text body md" }, last ? mdRender(String(last.text), { project: name, blocks: null }) : h("span", { class: "note" }, "The tutor wrote nothing.")),
            typeof error === "string" && error ? h("div", { class: "build-err badc" }, error) : null,
            steps);
    }

    async function stop() {
        const turn = pane.turn;
        if (!turn) return;
        btn.disabled = true;
        const r = await tutorApi("POST", `/runs/${enc(turn.runId)}/cancel`);
        btn.disabled = false;
        if (!r.body.ok) tell(errText(r.body));
    }

    // The skills list: open while the text is `/` and the start of a skill's name (no space yet),
    // in a lesson only, never in Goals. A choice fills in `/<name> ` for the learner to go on;
    // the message is then sent as typed.
    let skillRows = [];   // the skills shown, in order
    let skillAt = 0;      // the highlighted one
    let skillAsk = 0;     // the latest drawing; an older one's answer is dropped
    function closeSkills() { skillAsk++; skillRows = []; skillsEl.hidden = true; }
    function drawSkills() {
        const text = ta.value;
        const l = pane.lesson;
        if (!text.startsWith("/") || /\s/.test(text) || !l || l.kind === "goals") { closeSkills(); return; }
        const ask = ++skillAsk;
        loadChatSkills().then((list) => {
            if (ask !== skillAsk) return;
            const word = text.slice(1).toLowerCase();
            skillRows = list.filter((s) => s.name.toLowerCase().startsWith(word));
            if (!skillRows.length) { closeSkills(); return; }
            skillAt = 0;
            fill(skillsEl, skillRows.map((s) => h("div", {
                class: "skill", role: "option",
                onmousedown: (ev) => ev.preventDefault(),   // the box keeps the focus
                onclick: () => pickSkill(s),
            }, h("span", { class: "sk-name" }, `/${s.name}`),
               s.hint ? h("span", { class: "sk-hint" }, String(s.hint)) : null,
               h("span", { class: "sk-desc", title: String(s.description || "") }, String(s.description || "")))));
            markSkill();
            skillsEl.hidden = false;
        });
    }
    function markSkill() {
        [...skillsEl.children].forEach((r, i) => { r.classList.toggle("on", i === skillAt); r.setAttribute("aria-selected", i === skillAt ? "true" : "false"); });
        const on = skillsEl.children[skillAt];
        if (on) on.scrollIntoView({ block: "nearest" });
    }
    function pickSkill(s) {
        ta.value = `/${s.name} `;
        closeSkills();
        ta.focus();
        ta.setSelectionRange(ta.value.length, ta.value.length);
    }
    ta.addEventListener("input", (ev) => { if (!ev.isComposing) drawSkills(); });
    ta.addEventListener("compositionend", drawSkills);

    btn.addEventListener("click", () => (pane.turn ? stop() : send()));
    ta.addEventListener("keydown", (ev) => {
        if (ev.isComposing) return;
        if (!skillsEl.hidden && skillRows.length) {
            if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
                ev.preventDefault();
                skillAt = (skillAt + (ev.key === "ArrowDown" ? 1 : skillRows.length - 1)) % skillRows.length;
                markSkill();
                return;
            }
            if ((ev.key === "Tab" || ev.key === "Enter") && !ev.shiftKey) { ev.preventDefault(); pickSkill(skillRows[skillAt]); return; }
            if (ev.key === "Escape") { ev.preventDefault(); closeSkills(); return; }
        }
        if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); if (!pane.turn) send(); }
    });

    pane.reload = () => { pane.stale = true; if (!pane.el.hidden) load(); };
    pane.update = () => { if (pane.stale && !pane.loading) load(); };
    // Leaving the lesson closes its stream; the answer goes on, and is joined again on return.
    pane.hide = () => {
        if (pane.turn) endTurn(pane.turn);
        pane.stale = true;
    };
    pane.close = pane.hide;
    setComposer();
    pane.setLesson(lessonRow(name, id));
    return pane;
}

// ---- the knowledge base: the Knowledge page and a project's Insights tab (the mock's) --------
//
// What every tutor knows about the learner (the brief: their background, goals, preferences,
// the global insights they approved, the lessons done lately), the global insights tutors
// propose, each project's insights, the lessons done, and every change with its undo. All of it
// is the tutor service's records; the page only shows them and sends the learner's edits.

const INSIGHT_KINDS = { misconception: "misconception", strength: "strength", worked: "what worked", did_not_work: "what did not work" };
const INSIGHT_CLASS = { misconception: "k-bad", strength: "k-good", worked: "k-good", did_not_work: "k-warn" };
const RECORD_WORDS = { goal: "a goal", preference: "a preference", background: "your background", insight: "an insight", global: "a global insight" };
const CHANGE_WORDS = { add: "added", edit: "edited", delete: "deleted", replace: "replaced", retire: "retired", archive: "archived", restore: "restored", approve: "approved", undo: "undid a change to" };
const KB_VIEWS = new Set(["brief", "global", "lessons", "history"]);

const kbKey = (project, created) => `p:${project}|${created}`;
const removedMark = (x) => (x && x.project_removed ? " (removed)" : "");
const whereWords = (x) => (x && x.project ? `${x.project}${removedMark(x)}` : "");
const shortWhen = (s) => { const d = new Date(s); return s && !isNaN(d) ? d.toLocaleString([], { dateStyle: "short", timeStyle: "short" }) : String(s || ""); };
const dayOf = (s) => (typeof s === "string" ? s.slice(0, 10) : "");
const byWords = (by) => (by && by.who === "tutor" ? `the tutor in ${by.project || "a project"}` : "you");
const kbList = (r, key) => (r.body.ok && r.body.result && Array.isArray(r.body.result[key]) ? r.body.result[key] : null);

// What a record or a proposal rests on, in full: the evidence and the insights it cites.
function evidenceEl(e) {
    const d = e && e.data && typeof e.data === "object" ? e.data : {};
    const where = [whereWords(e), dayOf(e.at)].filter(Boolean).join(" · ");
    let what;
    if (e.kind === "check") {
        const words = d.timed_out ? "did not finish in time" : d.passed ? "passed" : "failed";
        what = [h("span", {}, `A check of “${d.title || "an exercise"}”, run by ${d.by === "learner" ? "you" : "the tutor"}: `,
                  h("span", { class: d.passed ? "okc" : "badc" }, words), d.exit_code === null || d.exit_code === undefined ? "" : ` (exit ${d.exit_code})`),
                h("div", {}, h("code", {}, String(d.command || ""))),
                String(d.output || "").trim() ? h("pre", { class: "ev-out" }, String(d.output)) : null];
    } else if (e.kind === "quote") {
        what = [h("span", { class: "quote" }, `Your words: “${d.sentence || ""}”`)];
    } else if (e.kind === "answer" && d.how === "typed") {
        what = [h("span", {}, `Your answer to “${d.question || ""}”: `), h("span", { class: "quote" }, `“${d.quote || ""}”`), " ",
                h("span", { class: d.right ? "okc" : "badc" }, d.right ? "right" : "not right"), d.note ? h("div", { class: "note" }, String(d.note)) : null];
    } else if (e.kind === "answer") {
        what = [h("span", {}, `Your answer to “${d.question || ""}”: ${letterOf(d.choice)} “${d.choice_text || ""}”, `),
                h("span", { class: d.right ? "okc" : "badc" }, d.right ? "right" : `wrong (the right one is ${letterOf(d.right_choice)})`)];
    } else if (e.kind === "completion") {
        what = [h("span", {}, `The lesson “${d.title || ""}” done: ${d.one_line || ""}`)];
    } else {
        what = [h("span", {}, String(e.kind || "evidence"))];
    }
    return h("div", { class: "evrow" }, h("div", {}, what), where ? h("div", { class: "when" }, where) : null);
}

function restsEl(detail) {
    const ev = detail && Array.isArray(detail.evidence) ? detail.evidence : [];
    const ins = detail && Array.isArray(detail.insights) ? detail.insights : [];
    if (!ev.length && !ins.length) return h("div", { class: "note" }, "Nothing that is still kept.");
    return h("div", {}, ev.map(evidenceEl), ins.map((r) => h("div", { class: "evrow" },
        h("div", {}, r.insight_kind ? h("span", { class: `kindp ${INSIGHT_CLASS[r.insight_kind] || ""}` }, INSIGHT_KINDS[r.insight_kind] || r.insight_kind) : null, " ",
            `The insight “${r.text}”`), h("div", { class: "when" }, whereWords(r)))));
}

// "What it rests on", read when it is opened (or at once, `open`).
function restsOn(rec, open) {
    const ro = rec.rests_on && typeof rec.rests_on === "object" ? rec.rests_on : {};
    const n = (Array.isArray(ro.evidence) ? ro.evidence.length : 0) + (Array.isArray(ro.insights) ? ro.insights.length : 0);
    if (!n) return null;
    const box = h("div", { class: "rests-body" }, h("span", { class: "note" }, "Asking…"));
    const d = h("details", { class: "rests" }, h("summary", {}, "what it rests on"), box);
    let asked = false;
    const ask = async () => {
        if (!d.open || asked) return;
        asked = true;
        const r = await tutorApi("GET", `/knowledge/records/${enc(rec.id)}`);
        const got = r.body.ok && r.body.result && r.body.result.record;
        fill(box, got ? restsEl(got.rests_on_detail) : h("span", { class: "bad" }, errText(r.body)));
    };
    d.addEventListener("toggle", ask);
    if (open) { d.open = true; ask(); }
    return d;
}

// One record, to read, edit in place (its text; an insight's kind too) and delete; a goal also
// archived (out of the brief, kept here) and restored. Every change is in the History, where it
// can be undone.
function recordRow(rec, opts) {
    const o = opts || {};
    const el = h("div", { class: "ins" + (rec.state === "retired" ? " retired" : rec.state === "archived" ? " archived" : "") });
    const said = h("div", { class: "bad small", hidden: true });
    const bad = (text) => { said.textContent = text; said.hidden = false; };
    const kindEl = () => (rec.kind === "insight" || rec.insight_kind) && rec.insight_kind
        ? h("span", { class: `kindp ${INSIGHT_CLASS[rec.insight_kind] || ""}` }, INSIGHT_KINDS[rec.insight_kind] || rec.insight_kind) : null;
    const approved = rec.kind === "global" && !(rec.by && rec.by.who === "tutor") && rec.project;
    const meta = () => [rec.by && rec.by.who === "tutor" ? `recorded by ${byWords(rec.by)}` : approved ? `approved by you, proposed in ${whereWords(rec)}` : "written by you",
        o.withProject && rec.project && !approved && !(rec.by && rec.by.who === "tutor" && rec.by.project === rec.project && !rec.project_removed) ? `from ${whereWords(rec)}`
            : rec.project_removed ? "from a removed project" : null,
        rec.state === "archived" ? `archived ${dayOf(rec.updated_at)}` : dayOf(rec.updated_at || rec.created_at),
        rec.state === "retired" ? (rec.replaced_by ? "replaced" : `retired${rec.reason ? `: ${rec.reason}` : ""}`) : null].filter(Boolean).join(" · ");
    // A button that sends one change and says so (Delete; a goal's Archive and Restore).
    const change = (label, method, body, said) => h("button", { onclick: async (ev) => {
        ev.target.disabled = true;
        const r = await tutorApi(method, `/knowledge/records/${enc(rec.id)}`, body);
        if (!r.body.ok) { ev.target.disabled = false; bad(errText(r.body)); return; }
        toast(said);
        if (o.onChanged) o.onChanged();
    } }, label);
    const del = () => change("Delete", "DELETE", undefined, "Deleted. The History can undo it.");
    const goal = rec.kind === "goal";
    const show = () => {
        const act = rec.state === "active" ? h("div", { class: "ins-act" },
            h("button", { onclick: edit }, "Edit"),
            goal ? change("Archive", "PATCH", { archived: true }, "Archived. The History can undo it.") : null,
            del())
            : goal && rec.state === "archived" ? h("div", { class: "ins-act" }, change("Restore", "PATCH", { archived: false }, "Restored: back in the brief."), del()) : null;
        fill(el, kindEl(), h("div", { class: "ins-main" }, h("div", { class: "ins-text" }, String(rec.text)), h("div", { class: "ins-ev" }, meta()),
            restsOn(rec, o.openRests), said), act);
    };
    const edit = () => {
        const text = h("textarea", { class: "kb-edit", rows: 2, "aria-label": "The text" });
        text.value = String(rec.text);
        const kind = rec.kind === "insight" ? h("select", { "aria-label": "Its kind" },
            Object.entries(INSIGHT_KINDS).map(([k, v]) => h("option", { value: k, selected: rec.insight_kind === k }, v))) : null;
        const save = h("button", { class: "primary", onclick: async () => {
            const body = {};
            if (text.value.trim() !== String(rec.text)) body.text = text.value;
            if (kind && kind.value !== rec.insight_kind) body.insight_kind = kind.value;
            if (!Object.keys(body).length) { show(); return; }
            save.disabled = true;
            const r = await tutorApi("PATCH", `/knowledge/records/${enc(rec.id)}`, body);
            save.disabled = false;
            if (!r.body.ok) { bad(errText(r.body)); return; }
            if (o.onChanged) o.onChanged(); else { Object.assign(rec, r.body.result.record); show(); }
        } }, "Save");
        fill(el, h("div", { class: "ins-main" }, text, kind ? h("div", { class: "kb-kind" }, kind) : null, said,
            h("div", { class: "row-btns kb-btns" }, h("button", { onclick: show }, "Cancel"), save)));
        text.focus();
    };
    show();
    return el;
}

// A line the learner adds in their own words: a goal, a preference (into the brief set `setId`),
// a global insight.
function addRow(kind, placeholder, onChanged, setId) {
    const input = h("input", { type: "text", placeholder, autocomplete: "off" });
    const said = h("div", { class: "bad small", hidden: true });
    const go = h("button", { type: "submit" }, "Add");
    return h("form", { class: "addrow", method: "post", action: "#", onsubmit: async (ev) => {
        ev.preventDefault();
        if (!input.value.trim()) return;
        go.disabled = true;
        const r = await tutorApi("POST", "/knowledge/records", setId === undefined ? { kind, text: input.value } : { kind, text: input.value, set: Number(setId) });
        go.disabled = false;
        if (!r.body.ok) { said.textContent = errText(r.body); said.hidden = false; return; }
        onChanged();
    } }, input, go, said);
}

function summaryDialog(sm) {
    openModal(`Summary: ${sm.title}`, [
        h("p", { class: "note" }, [whereWords(sm), sm.closed_at ? `done ${fmtWhen(sm.closed_at)}` : null].filter(Boolean).join(" · ")),
        sm.one_line ? h("p", { class: "sum-line" }, String(sm.one_line)) : null,
        h("div", { class: "sum-text" }, String(sm.summary || "The tutor wrote no summary.")),
        h("p", { class: "note" }, "The line in bold is what later lessons' tutors are given, in every project."),
    ], [h("button", { onclick: closeModal }, "Close")]);
}

function summaryRow(sm, withProject) {
    return h("div", { class: "rowline sumrow", title: "Read the summary", onclick: () => summaryDialog(sm) },
        h("div", { class: "grow" }, h("b", {}, String(sm.title || "Lesson")), withProject ? h("span", { class: "muted" }, ` · ${whereWords(sm)}`) : null,
            sm.one_line ? h("div", { class: "ins-ev" }, String(sm.one_line)) : null),
        h("span", { class: "when" }, dayOf(sm.closed_at)));
}

// A project's insights: the ones in force, and the retired ones behind a switch.
function insightsCard(records, o) {
    const active = records.filter((r) => r.state === "active");
    const retired = records.filter((r) => r.state !== "active");
    const sw = h("label", { class: "switch" }, h("input", { type: "checkbox", checked: !!o.showRetired, onchange: (ev) => o.onRetired(ev.target.checked) }),
        ` show retired (${retired.length})`);
    const shown = o.showRetired ? [...active, ...retired] : active;
    return h("div", { class: "card" }, h("div", { class: "card-head" }, "Insights", h("span", { class: "grow head-note" }, `${active.length} in use`), retired.length ? sw : null),
        shown.length ? shown.map((r) => recordRow(r, { onChanged: o.onChanged })) : h("div", { class: "none" }, o.empty || "None yet. A tutor records them as lessons go on."));
}

// ---- the dot: a global insight waits for the learner

async function refreshKbOpen() {
    const r = await tutorApi("GET", "/knowledge/proposals?state=open");
    if (!S.loggedIn) return;
    if (r.body.ok && r.body.result && Number.isInteger(r.body.result.open)) {
        const was = S.kbOpen;
        S.kbOpen = r.body.result.open;
        if (was !== S.kbOpen) { renderKbFoot(); if (S.page === "knowledge" && S.kb.navData) drawKbNav(); }
    }
}

function renderKbFoot() {
    if (!S.shell) return;
    const btn = S.shell.rail.parentNode.querySelector(".knowledge-btn");
    if (!btn) return;
    btn.classList.toggle("sel", S.page === "knowledge");
    btn.querySelector(".kdot").hidden = !(S.kbOpen > 0);
    btn.title = S.kbOpen > 0 ? `${S.kbOpen} global insight(s) proposed by a tutor wait for you` : "What every tutor knows about you";
}

// ---- the Knowledge page

function showKnowledge(view) {
    collapseRailAfterUse();
    S.page = "knowledge";
    if (view) S.kb.view = view;
    S.kb.changes = null;   // the History is read again when it is shown
    saveLayout();
    render();
    renderKnowledge();
}

function makeKnowledgePane() {
    const nav = h("div", { class: "kb-nav" });
    const body = h("div", { class: "page-inner" });
    const page = h("div", { class: "page kb-page" }, body);
    const el = h("div", { class: "pane kb", hidden: true }, nav, page);
    return { el, nav, body, page };
}

// The projects the page knows of: those there now, and the removed ones whose insights or
// lessons are kept.
function kbProjects(insights, summaries) {
    const out = new Map();
    for (const p of S.projects) if (typeof p.created === "string") out.set(kbKey(p.name, p.created), { project: p.name, created: p.created, removed: false, live: true });
    for (const x of [...insights, ...summaries]) {
        if (!x || !x.project) continue;
        const key = kbKey(x.project, x.created);
        if (!out.has(key)) out.set(key, { project: x.project, created: x.created, removed: !!x.project_removed, live: false });
    }
    return [...out.entries()].map(([key, v]) => ({ key, ...v }));
}

function drawKbNav() {
    const { nav } = S.shell.knowledge;
    const d = S.kb.navData;
    const v = S.kb.view;
    const item = (id, label, count, dot) => h("div", { class: "kn" + (v === id ? " on" : ""), onclick: () => showKnowledge(id) },
        h("span", {}, label), dot ? h("span", { class: "kdot", title: "A proposal waits for you" }) : null,
        count === null || count === undefined ? null : h("span", { class: "kc" }, String(count)));
    const projects = d.projects.map((p) => item(p.key, `${p.project}${p.live ? "" : " (removed)"}`,
        d.insights.filter((r) => r.state === "active" && kbKey(r.project, r.created) === p.key).length));
    fill(nav, h("div", { class: "kh" }, "ABOUT YOU, EVERYWHERE"),
        item("brief", "Your briefs"), item("global", "Global insights", d.globals, S.kbOpen > 0),
        h("div", { class: "kh" }, "PER PROJECT"), projects.length ? projects : h("div", { class: "kn none-kn" }, "No project yet."),
        h("div", { class: "kh" }, "EVERYTHING"), item("lessons", "Lessons done", d.summaries.length), item("history", "History"));
}

async function renderKnowledge() {
    if (!S.shell || S.page !== "knowledge") return;
    const seq = ++S.kb.seq;
    const v = S.kb.view;
    const [ins, sums, globals, props] = await Promise.all([tutorApi("GET", "/knowledge/records?kind=insight&all=1"),
        tutorApi("GET", "/knowledge/lessons"), tutorApi("GET", "/knowledge/records?kind=global"), tutorApi("GET", "/knowledge/proposals?state=open")]);
    if (seq !== S.kb.seq || S.page !== "knowledge") return;
    const { body } = S.shell.knowledge;
    const insights = kbList(ins, "records");
    const summaries = kbList(sums, "lessons");
    if (!insights || !summaries) {
        fill(body, h("h1", {}, "Knowledge"), h("p", { class: "bad" }, errText((insights ? sums : ins).body)),
            h("div", { class: "btns" }, h("button", { onclick: renderKnowledge }, "Try again")));
        return;
    }
    const gl = kbList(globals, "records") || [];
    const proposals = kbList(props, "proposals") || [];
    if (props.body.ok && Number.isInteger(props.body.result.open)) S.kbOpen = props.body.result.open;
    S.kb.navData = { insights, summaries, globals: gl.length, projects: kbProjects(insights, summaries) };
    if (!KB_VIEWS.has(v) && !S.kb.navData.projects.some((p) => p.key === v)) S.kb.view = "brief";
    drawKbNav();
    renderKbFoot();
    const again = () => renderKnowledge();
    let view;
    switch (S.kb.view) {
        case "brief": view = await briefView(again); break;
        case "global": view = await globalView(gl, proposals, again); break;
        case "lessons": view = await lessonsView(); break;
        case "history": view = await historyView(); break;
        default: {
            const p = S.kb.navData.projects.find((x) => x.key === S.kb.view);
            view = projectView(p, insights.filter((r) => kbKey(r.project, r.created) === p.key),
                summaries.filter((sm) => kbKey(sm.project, sm.created) === p.key), again);
        }
    }
    if (seq !== S.kb.seq || S.page !== "knowledge") return;
    fill(body, view);
}

// ---- brief sets: each holds goals, preferences and a background; a project uses one (Default
// when it was given none); the global insights are every set's.

async function briefSets() {
    const r = await tutorApi("GET", "/knowledge/sets");
    return { sets: kbList(r, "sets"), r };
}

const setUse = (st) => (st.default ? `used by every project not given another${st.projects ? `; given to ${st.projects}` : ""}`
    : `${st.projects} project${st.projects === 1 ? "" : "s"}`);

// A "Brief" select of the sets, `current` (an id) chosen, Default when none.
function briefSetSelect(sets, current) {
    const pick = String(current || ((sets.find((st) => st.default) || sets[0] || {}).id || ""));
    return h("select", { "aria-label": "Brief set" },
        sets.map((st) => h("option", { value: String(st.id), selected: String(st.id) === pick }, String(st.name))));
}

function briefSetDialog(title, init, withCopy, onDone) {
    const name = h("input", { type: "text", autocomplete: "off", maxlength: 80 });
    name.value = init || "";
    const said = h("p", { class: "bad", hidden: true });
    const empty = h("input", { type: "radio", name: "bs-from", checked: true });
    const copy = h("input", { type: "radio", name: "bs-from" });
    const go = h("button", { class: "primary", type: "submit" }, "Save");
    const form = h("form", { method: "post", action: "#", onsubmit: async (ev) => {
        ev.preventDefault();
        if (!name.value.trim()) return;
        go.disabled = true;
        const err = await onDone(name.value.trim(), copy.checked);
        go.disabled = false;
        if (err) { said.textContent = err; said.hidden = false; return; }
        closeModal();
    } }, formRow("Name", name),
    withCopy ? h("div", { class: "radios" }, h("label", {}, empty, " empty"), h("label", {}, copy, ` a copy of “${withCopy}” (its goals, preferences and background)`)) : null,
    said, h("div", { class: "btns" }, h("button", { type: "button", onclick: closeModal }, "Cancel"), go));
    openModal(title, form, []);
    name.focus();
}

async function briefView(again) {
    const { sets, r: rs } = await briefSets();
    if (!sets || !sets.length) return [h("h1", {}, "Your briefs"), h("p", { class: "bad" }, errText(rs.body))];
    const shown = sets.find((st) => st.id === S.kb.set) || sets.find((st) => st.default) || sets[0];
    S.kb.set = shown.id;
    const r = await tutorApi("GET", `/knowledge/brief?set=${enc(shown.id)}`);
    const res = r.body.ok && r.body.result;
    if (!res || !res.parts) return [h("h1", {}, "Your briefs"), h("p", { class: "bad" }, errText(r.body))];
    const parts = res.parts;
    const setId = shown.id;
    // The set picker: which brief is shown and edited here, and New, Rename, Delete.
    const picker = briefSetSelect(sets, setId);
    picker.addEventListener("change", () => { S.kb.set = picker.value; again(); });
    const pickSaid = h("span", { class: "note grow" }, setUse(shown));
    const newSet = h("button", { onclick: () => briefSetDialog("New brief set", "", shown.name, async (name, copy) => {
        const r2 = await tutorApi("POST", "/knowledge/sets", copy ? { name, copy_from: Number(setId) } : { name });
        if (!r2.body.ok) return errText(r2.body);
        S.kb.set = r2.body.result.set.id;
        toast(`The brief set “${name}” is made. Choose it for a project in its Config box or in the New project box.`);
        again();
        return null;
    }) }, "New");
    const renameSet = h("button", { onclick: () => briefSetDialog(`Rename “${shown.name}”`, shown.name, null, async (name) => {
        const r2 = await tutorApi("PATCH", `/knowledge/sets/${enc(setId)}`, { name });
        if (!r2.body.ok) return errText(r2.body);
        again();
        return null;
    }) }, "Rename");
    const deleteSet = h("button", { disabled: !!shown.default, title: shown.default ? "Default cannot be deleted: every project not given another set uses it." : null,
        onclick: () => {
            const said = h("p", { class: "bad", hidden: true });
            const go = h("button", { class: "danger", onclick: async () => {
                go.disabled = true;
                const r2 = await tutorApi("DELETE", `/knowledge/sets/${enc(setId)}`);
                go.disabled = false;
                if (!r2.body.ok) { said.textContent = errText(r2.body); said.hidden = false; return; }
                closeModal();
                S.kb.set = null;
                toast(`The brief set “${shown.name}” is deleted.`);
                again();
            } }, "Delete");
            openModal(`Delete “${shown.name}”`, [h("p", {}, "Its goals, preferences and background go with it, for good: the History cannot undo this."),
                h("p", { class: "note" }, "A set that a project uses cannot be deleted: give the project another set in its Config box first."), said],
            [h("button", { onclick: closeModal }, "Cancel"), go]);
        } }, "Delete");
    const setBar = h("div", { class: "card" }, h("div", { class: "card-head" }, "Brief set", h("span", { class: "grow head-note" }, "each project uses one; chosen in the New project box and the project's Config box")),
        h("div", { class: "form" }, h("div", { class: "row-btns" }, picker, pickSaid, newSet, renameSet, deleteSet)));
    const toggle = h("div", { class: "seg-toggle" },
        h("button", { class: S.kb.raw ? "" : "on", onclick: () => { S.kb.raw = false; again(); } }, "Overview"),
        h("button", { class: S.kb.raw ? "on" : "", onclick: () => { S.kb.raw = true; again(); } }, "As a tutor reads it"));
    const title = `Your brief: ${shown.name}`;
    if (S.kb.raw) {
        return [h("div", { class: "kb-top" }, h("div", {}, h("h1", {}, title),
                h("div", { class: "sub" }, "The exact text every tutor of a project that uses this brief set is given at the start of each lesson. It is put together from the records by the program, never written by a model, and never cut.")), toggle),
            setBar,
            h("div", { class: "card" }, h("div", { class: "card-head" }, "As a tutor reads it", h("span", { class: "grow head-note" }, `${String(res.text).length} characters`)),
                h("pre", { class: "rawbrief" }, String(res.text))),
            h("p", { class: "note" }, "A project's tutor is also told that project's own insights, after this text.")];
    }
    // The background: a box the learner writes, of any length.
    const bg = h("textarea", { class: "kb-bg", rows: 5, placeholder: "Comfortable with Python and SQL, new to streaming…", "aria-label": "Your background" });
    bg.value = parts.background ? String(parts.background.text) : "";
    const bgSaid = h("span", { class: "note grow" }, parts.background ? `Saved ${fmtWhen(parts.background.updated_at)}.` : "Not written yet.");
    const bgSave = h("button", { class: "primary", onclick: async () => {
        bgSave.disabled = true;
        const r2 = await tutorApi("PUT", "/knowledge/background", { text: bg.value, set: Number(setId) });
        bgSave.disabled = false;
        if (!r2.body.ok) { bgSaid.className = "bad grow"; bgSaid.textContent = errText(r2.body); return; }
        toast(bg.value.trim() ? "Your background is saved. The tutors of this set's projects read it from their next lesson." : "Your background is removed.");
        again();
    } }, "Save");
    bg.addEventListener("input", () => { bgSaid.className = "note grow"; bgSaid.textContent = "Not saved yet."; });
    const lines = (rows, kind, empty, placeholder) => [
        rows.length ? rows.map((x) => recordRow(x, { onChanged: again, openRests: true, withProject: true })) : h("div", { class: "none" }, empty),
        addRow(kind, placeholder, again, setId)];
    // Archived goals (out of the brief, kept here) behind a switch, as a project's retired insights.
    const archived = Array.isArray(parts.archived_goals) ? parts.archived_goals : [];
    const archivedSw = archived.length ? h("label", { class: "switch" },
        h("input", { type: "checkbox", checked: !!S.kb.showArchived, onchange: (ev) => { S.kb.showArchived = ev.target.checked; again(); } }),
        ` show archived (${archived.length})`) : null;
    const recently = Array.isArray(parts.recently) ? parts.recently : [];
    const globalsIn = Array.isArray(parts.global_insights) ? parts.global_insights : [];
    return [
        h("div", { class: "kb-top" }, h("div", {}, h("h1", {}, title),
            h("div", { class: "sub" }, "What every tutor of a project that uses this brief set knows about you before a lesson starts. A tutor records a goal or a preference only from a sentence you wrote, into its project's set; you can write, change or delete any of them.")), toggle),
        setBar,
        h("div", { class: "card" }, h("div", { class: "card-head" }, "Your background", h("span", { class: "grow head-note" }, "in your own words; only you write it")),
            h("div", { class: "form" }, h("p", { class: "note" }, "What you know already and what you work with. Every tutor of this set's projects reads it as you wrote it."), bg,
                h("div", { class: "row-btns kb-btns" }, bgSaid, bgSave))),
        h("div", { class: "two" },
            h("div", { class: "card" }, h("div", { class: "card-head" }, "Your goals", archivedSw ? h("span", { class: "grow" }) : null, archivedSw),
                lines(S.kb.showArchived ? [...(parts.goals || []), ...archived] : parts.goals || [], "goal", "None yet.", "Add a goal in your own words…")),
            h("div", { class: "card" }, h("div", { class: "card-head" }, "How you want to be taught"),
                lines(parts.preferences || [], "preference", "None yet.", "Add a preference in your own words…"))),
        h("div", { class: "two" },
            h("div", { class: "card" }, h("div", { class: "card-head" }, "How you learn",
                h("span", { class: "grow head-note" }, h("button", { class: "link", onclick: () => showKnowledge("global") }, `global insights (${globalsIn.length}), in every set`))),
                globalsIn.length ? globalsIn.map((g) => h("div", { class: "rowline" }, h("span", { class: "grow" }, String(g.text))))
                    : h("div", { class: "none" }, "None yet. A tutor proposes them; you approve them.")),
            h("div", { class: "card" }, h("div", { class: "card-head" }, "Recently", h("span", { class: "grow head-note" }, "lessons done, in this set's projects")),
                recently.length ? recently.map((sm) => summaryRow(sm, true)) : h("div", { class: "none" }, "No lesson done yet."))),
        h("p", { class: "note" }, "A project's tutor is also told that project's own insights. The lessons' summaries and your background stand in for a list of what you know."),
    ];
}

// A global insight a tutor proposes: a new one, a replacement, a retirement; with where it came
// from, what it rests on, the like insights of the other projects, the text to edit, and the
// learner's decision.
function globalProposalRow(p, again) {
    const said = h("div", { class: "bad small", hidden: true });
    const bad = (text) => { said.textContent = text; said.hidden = false; };
    const action = p.action === "replace" ? "Replaces a global insight" : p.action === "retire" ? "Retires a global insight" : "A new global insight";
    const lessonTitle = (() => {
        const l = lessonRow(p.project, String(p.lesson_id));
        if (l && !p.project_removed) return `“${l.title}”`;
        const sm = S.kb.navData && S.kb.navData.summaries.find((x) => String(x.lesson_id) === String(p.lesson_id));
        return sm ? `“${sm.title}”` : "a lesson";
    })();
    const text = p.action === "retire" ? null : h("textarea", { class: "kb-edit", rows: 2, "aria-label": "The insight, as every tutor would read it" });
    if (text) text.value = String(p.text || "");
    const similar = Array.isArray(p.similar) ? p.similar : [];
    const approve = h("button", { class: "primary", onclick: async () => {
        approve.disabled = true;
        const body = text && text.value.trim() !== String(p.text || "") ? { text: text.value } : {};
        const r = await tutorApi("POST", `/knowledge/proposals/${enc(p.id)}/approve`, body);
        approve.disabled = false;
        if (!r.body.ok) { bad(errText(r.body)); return; }
        toast("Approved: every tutor is told it from its next message.");
        again();
    } }, "Approve");
    const dismiss = h("button", { onclick: async () => {
        dismiss.disabled = true;
        const r = await tutorApi("POST", `/knowledge/proposals/${enc(p.id)}/dismiss`, {});
        dismiss.disabled = false;
        if (!r.body.ok) { bad(errText(r.body)); return; }
        toast("Dismissed: nothing is changed. The tutor hears of it with your next message there.");
        again();
    } }, "Dismiss");
    const rests = h("details", { class: "rests" }, h("summary", {}, "what it rests on"), h("div", { class: "rests-body" }, restsEl(p.rests_on_detail)));
    return h("div", { class: "ins proposed" },
        h("div", { class: "ins-main" },
            h("div", { class: "prop-what" }, action, h("span", { class: "ins-ev" }, ` · proposed by the tutor in ${whereWords(p)}, lesson ${lessonTitle} · ${dayOf(p.created_at)}`)),
            p.target ? h("div", { class: "prop-target" }, p.action === "retire" ? "To stop telling every tutor: " : "In force now: ", h("span", { class: "quote" }, `“${p.target.text}”`)) : null,
            text ? h("div", { class: "kb-kind" }, text, h("div", { class: "note" }, "Edit it as you want every tutor to read it.")) : null,
            p.reason ? h("div", { class: "ins-ev" }, `Why: ${p.reason}`) : null,
            rests,
            similar.length ? h("div", { class: "similar" }, h("div", { class: "note" }, "Like it, in your other projects (the tutor that proposes it does not see these):"),
                similar.map((x) => h("div", { class: "simrow" }, x.insight_kind ? h("span", { class: `kindp ${INSIGHT_CLASS[x.insight_kind] || ""}` }, INSIGHT_KINDS[x.insight_kind] || x.insight_kind) : null,
                    h("span", { class: "grow" }, String(x.text), h("span", { class: "ins-ev" }, ` · ${whereWords(x)}`))))) : null,
            said,
            h("div", { class: "row-btns kb-btns" }, dismiss, approve)));
}

async function globalView(globals, proposals, again) {
    for (const p of proposals) {
        if (!p.project_removed && projectRow(p.project) && !S.lessons[p.project]) await refreshLessons(p.project);
    }
    return [
        h("h1", {}, "Global insights"),
        h("div", { class: "sub" }, "The few things every tutor, in every project, is told about how you learn. A tutor can only propose one; it is told to the others once you approve it. There is no limit on how many."),
        h("div", { class: "capbar" }, `${globals.length} in use`, proposals.length ? ` · ${proposals.length} waiting for you` : ""),
        proposals.length ? h("div", { class: "card" }, h("div", { class: "card-head" }, "Waiting for you", h("span", { class: "grow head-note" }, "a tutor proposed these")),
            proposals.map((p) => globalProposalRow(p, again))) : null,
        h("div", { class: "card" }, h("div", { class: "card-head" }, "In use"),
            globals.length ? globals.map((g) => recordRow(g, { onChanged: again, withProject: true })) : h("div", { class: "none" }, "None yet."),
            addRow("global", "Add one yourself, in your own words…", again)),
    ];
}

function projectView(p, records, summaries, again) {
    return [
        h("h1", {}, `${p.project}: insights`, p.live ? null : h("span", { class: "kindp head-pill" }, "removed")),
        h("div", { class: "sub" }, p.live ? "What this project's tutor has learned about how you learn here. Only this project's tutors are told these; the others are not."
            : "This project was removed. Its insights and lessons are kept here, and no tutor is told its insights."),
        insightsCard(records, { showRetired: S.kb.showRetired, onRetired: (on) => { S.kb.showRetired = on; again(); }, onChanged: again }),
        h("div", { class: "card" }, h("div", { class: "card-head" }, "Lessons done here"),
            summaries.length ? summaries.map((sm) => summaryRow(sm, false)) : h("div", { class: "none" }, "No lesson done yet.")),
    ];
}

async function lessonsView() {
    const q = S.kb.q || "";
    const r = await tutorApi("GET", `/knowledge/lessons${q.trim() ? `?q=${enc(q.trim())}` : ""}`);
    const rows = kbList(r, "lessons");
    const search = h("input", { type: "text", placeholder: "Search the titles and summaries…", autocomplete: "off", "aria-label": "Search the lessons" });
    search.value = q;
    const list = h("div", {}, rows ? (rows.length ? rows.map((sm) => summaryRow(sm, true)) : h("div", { class: "none" }, q.trim() ? "No lesson holds every word of that." : "No lesson done yet."))
        : h("div", { class: "none bad" }, errText(r.body)));
    const form = h("form", { class: "addrow", method: "get", action: "#", onsubmit: (ev) => { ev.preventDefault(); S.kb.q = search.value; renderKnowledge(); } },
        search, h("button", { type: "submit" }, "Search"));
    return [h("h1", {}, "Lessons done"), h("div", { class: "sub" }, "Every lesson a tutor closed, in every project, newest first. Open one to read its summary."),
        h("div", { class: "card" }, form, list)];
}

// One change: who, where, what, before and after; Undo.
function changeRow(c) {
    const items = Array.isArray(c.items) ? c.items : [];
    const first = items.find((it) => it.after || it.before) || {};
    const rec = first.after || first.before || {};
    const who = c.by && c.by.who === "tutor" ? "the tutor" : "you";
    const where = c.by && c.by.who === "tutor" ? c.by.project : rec.project ? `${rec.project}${rec.project_removed ? " (removed)" : ""}` : "everywhere";
    const kinds = (Array.isArray(c.kinds) ? c.kinds : []).map((k) => RECORD_WORDS[k] || k).join(", ");
    const said = h("div", { class: "small", hidden: true });
    const undo = c.undone ? h("span", { class: "note" }, "undone") : h("button", { onclick: async () => {
        undo.disabled = true;
        const r = await tutorApi("POST", `/knowledge/changes/${enc(c.id)}/undo`, {});
        undo.disabled = false;
        if (!r.body.ok) {
            said.className = "bad small";
            said.textContent = r.body.error && r.body.error.kind === "conflict" ? `Not undone: ${r.body.error.message || "it changed since."}` : errText(r.body);
            said.hidden = false;
            return;
        }
        toast("Undone.");
        S.kb.changes = null;
        renderKnowledge();
    } }, "Undo");
    const ba = items.filter((it) => it.before && it.after && (it.before.text !== it.after.text || it.before.insight_kind !== it.after.insight_kind
        || it.before.state !== it.after.state));
    const textOf = (x) => `${x.insight_kind ? `(${INSIGHT_KINDS[x.insight_kind] || x.insight_kind}) ` : ""}${x.text}${x.state && x.state !== "active" ? ` [${x.state}]` : ""}`;
    const added = c.action === "replace" ? items.filter((it) => !it.before && it.after) : [];
    return h("div", { class: "hist" + (c.undone ? " undone" : "") },
        h("span", { class: "when" }, shortWhen(c.at)),
        h("span", { class: "who" }, `${who} · ${where}`),
        h("div", { class: "what" }, h("div", {}, `${CHANGE_WORDS[c.action] || c.action} ${kinds}: `, h("span", { class: "quote" }, `“${rec.text || ""}”`)),
            c.note ? h("div", { class: "note" }, String(c.note)) : null,
            ba.length || added.length ? h("details", { class: "rests" }, h("summary", {}, "before and after"),
                ba.map((it) => h("div", { class: "ba" }, h("div", {}, h("span", { class: "note" }, "before: "), textOf(it.before)),
                    h("div", {}, h("span", { class: "note" }, "after: "), textOf(it.after)))),
                added.map((it) => h("div", { class: "ba" }, h("span", { class: "note" }, "new: "), textOf(it.after)))) : null,
            said),
        undo);
}

async function historyView() {
    if (!S.kb.changes) {
        const r = await tutorApi("GET", "/knowledge/changes?limit=50");
        const rows = kbList(r, "changes");
        if (!rows) return [h("h1", {}, "History"), h("p", { class: "bad" }, errText(r.body))];
        S.kb.changes = rows;
        S.kb.next = r.body.result.next;
    }
    const more = S.kb.next !== null && S.kb.next !== undefined ? h("div", { class: "addrow" }, h("button", { onclick: async (ev) => {
        ev.target.disabled = true;
        const r = await tutorApi("GET", `/knowledge/changes?limit=50&before=${enc(S.kb.next)}`);
        const rows = kbList(r, "changes");
        if (!rows) { ev.target.disabled = false; toast(errText(r.body), true); return; }
        S.kb.changes = [...S.kb.changes, ...rows];
        S.kb.next = r.body.result.next;
        renderKnowledge();
    } }, "Show more")) : null;
    return [h("h1", {}, "History"),
        h("div", { class: "sub" }, "Every change to what tutors know about you, newest first: by you or by a tutor. Any change can be undone, unless what it changed was changed again since."),
        h("div", { class: "card" }, S.kb.changes.length ? S.kb.changes.map(changeRow) : h("div", { class: "none" }, "Nothing changed yet."), more)];
}

// ---- a project's Insights tab

function makeInsightsPane(name) {
    const body = h("div", { class: "page-inner" }, h("div", { class: "none" }, "Asking…"));
    const el = h("div", { class: "pane page insights-pane" }, body);
    const pane = { kind: "insights", el, showRetired: false, stale: true };
    const load = async () => {
        pane.stale = false;
        const r = await tutorApi("GET", `/project/${enc(name)}/insights?all=1`);
        const rows = kbList(r, "records");
        fill(body, h("h1", {}, `${name}: insights`),
            h("div", { class: "sub" }, "What this project's tutor has learned about how you learn here. Only this project's tutors are told these. ",
                h("button", { class: "link", onclick: () => showKnowledge("brief") }, "Open the Knowledge page"), " for your brief and every project."),
            rows ? insightsCard(rows, { showRetired: pane.showRetired, onRetired: (on) => { pane.showRetired = on; load(); }, onChanged: load })
                : h("p", { class: "bad" }, errText(r.body)));
    };
    // Read again each time it is shown: a lesson may have added one meanwhile.
    pane.update = () => { if (pane.stale) load(); };
    pane.hide = () => { pane.stale = true; };
    return pane;
}

// ---- Settings -> Tutor ------------------------------------------------------------------------

async function refreshTutorSettings() {
    if (!S.loggedIn) return;
    refreshInstructions();
    const r = await tutorApi("GET", "/settings");
    if (!S.loggedIn) return;
    if (r.body.ok && r.body.result && typeof r.body.result.claude_token_set === "boolean") { S.tutorSettings = r.body.result; S.tutorSettingsError = null; }
    else S.tutorSettingsError = errText(r.body);
    renderTutorSettings();
    renderDefaultAgent();
}

// The default agent: what a lesson that has not chosen one is taught by.
function renderDefaultAgent(note) {
    if (!S.shell) return;
    const box = S.shell.settings.agent;
    const st = S.tutorSettings;
    for (const pane of Object.values(S.panes)) if (pane.kind === "lesson" && pane.drawAgent) pane.drawAgent();
    if (!st) { box.replaceChildren(); return; }
    const sel = h("select", { "aria-label": "The default agent" },
        Object.entries(AGENT_NAMES).map(([a, words]) => h("option", { value: a }, words)));
    sel.value = AGENT_NAMES[st.default_agent] ? st.default_agent : "claude";
    const said = h("p", { class: note && note.bad ? "bad" : "muted" }, note ? note.text : "");
    said.hidden = !note;
    sel.addEventListener("change", async () => {
        sel.disabled = true;
        const r = await tutorApi("PUT", "/settings", { default_agent: sel.value });
        sel.disabled = false;
        if (!r.body.ok || !r.body.result || typeof r.body.result.claude_token_set !== "boolean") { renderDefaultAgent({ text: errText(r.body), bad: true }); return; }
        S.tutorSettings = r.body.result;
        renderDefaultAgent({ text: `The default agent is now ${AGENT_NAMES[S.tutorSettings.default_agent] || S.tutorSettings.default_agent}.` });
        // The lessons that follow the default teach with the new one: read again.
        for (const n of Object.keys(S.lessons)) refreshLessons(n);
        for (const pane of Object.values(S.panes)) if (pane.kind === "lesson" && !pane.turn) pane.reload();
    });
    box.replaceChildren(h("div", { class: "form" },
        formRow("Default agent", sel, "Lessons that have not started and have not chosen an agent follow this one. A lesson under way " +
            "stays with the agent that began it, until you choose another in its header."),
        h("p", { class: "note" }, "A project's tutor can only use an agent the project has: pi is added in the project's gear, under Agents, " +
            "and logged in by running pi in its Shell."),
        said));
}

function renderTutorSettings(note) {
    if (!S.shell) return;
    const box = S.shell.settings.tutor;
    const st = S.tutorSettings;
    const said = h("p", { class: note && note.bad ? "bad" : "muted" }, note ? note.text : "");
    said.hidden = !note;
    const about = h("p", { class: "note" }, "The tutor in every project signs in to Claude with this token, so a new project's first lesson works without a login in its Shell. " +
        "Without one, the tutor uses the project's own Claude Code login, made with claude in its Shell.");
    if (!st) {
        box.replaceChildren(h("div", { class: S.tutorSettingsError ? "none bad" : "none" }, S.tutorSettingsError || "Asking…"), h("div", { class: "form" }, about));
        return;
    }
    const token = h("input", { type: "password", autocomplete: "off", spellcheck: "false", placeholder: st.claude_token_set ? "a new token to replace it" : "the token" });
    const put = async (value, btn) => {
        btn.disabled = true;
        const r = await tutorApi("PUT", "/settings", { claude_token: value });
        btn.disabled = false;
        token.value = "";
        if (!r.body.ok || !r.body.result || typeof r.body.result.claude_token_set !== "boolean") { renderTutorSettings({ text: errText(r.body), bad: true }); return; }
        S.tutorSettings = r.body.result;
        renderTutorSettings({ text: value ? "The token is set." : "The token is removed." });
    };
    const setBtn = h("button", { class: "primary", type: "submit" }, st.claude_token_set ? "Replace" : "Set");
    const remove = st.claude_token_set ? h("button", { type: "button", onclick: () => put("", remove) }, "Remove") : null;
    const form = h("form", { class: "form", method: "post", action: "#", onsubmit: (ev) => {
        ev.preventDefault();
        if (!token.value.trim()) { renderTutorSettings({ text: "Paste a token first.", bad: true }); return; }
        put(token.value.trim(), setBtn);
    } },
    h("p", { class: "tutor-state" }, h("span", { class: `dot inline ${st.claude_token_set ? "up" : ""}` }), " ",
        st.claude_token_set ? "A Claude token is set. It is not shown again." : "No Claude token is set."),
    about,
    formRow("Claude token", token, "made with claude setup-token on a machine where you are logged in to Claude"),
    said,
    h("div", { class: "btns" }, remove, setBtn));
    box.replaceChildren(form);
}

// The tutor's instructions: the text every tutor is given with each message, editable, with
// clanker-learn's own text to go back to. The box is drawn once per reading; what the learner
// typed and has not saved is kept when Settings is read again.
async function refreshInstructions() {
    if (!S.loggedIn) return;
    const r = await tutorApi("GET", "/instructions");
    if (!S.loggedIn || !S.shell) return;
    const box = S.shell.settings.instr;
    const res = r.body.ok && r.body.result;
    if (!res || typeof res.text !== "string") {
        if (!box.querySelector("textarea")) box.replaceChildren(h("div", { class: "none bad" }, "The tutor's instructions could not be read: " + errText(r.body)));
        return;
    }
    if (box.instr && box.instr.dirty()) return;   // unsaved edits stay
    renderInstructions(res);
}

function renderInstructions(st, note) {
    const box = S.shell.settings.instr;
    const text = h("textarea", { class: "instr-text", rows: 18, spellcheck: "false", "aria-label": "The tutor's instructions" });
    text.value = st.text;
    const said = h("p", { class: note && note.bad ? "bad" : "muted" }, note ? note.text : "");
    said.hidden = !note;
    const where = h("p", { class: "tutor-state" }, h("span", { class: `dot inline ${st.edited ? "warn" : "up"}` }), " ",
        st.edited ? "Changed: this is not the text clanker-learn ships." : "The text clanker-learn ships.");
    const save = h("button", { class: "primary", type: "button" }, "Save");
    const back = h("button", { type: "button", disabled: !st.edited }, "Back to the shipped text");
    const sure = h("div", { class: "btns", hidden: true });
    const btns = h("div", { class: "btns" }, back, save);
    const dirty = () => text.value !== st.text;
    const unsaved = h("span", { class: "note" }, "");
    text.addEventListener("input", () => { unsaved.textContent = dirty() ? "Not saved yet." : ""; });
    save.addEventListener("click", async () => {
        if (!text.value.trim()) { renderInstructions(st, { text: "The instructions cannot be empty.", bad: true }); return; }
        save.disabled = true;
        const r = await tutorApi("PUT", "/instructions", { text: text.value });
        save.disabled = false;
        const res = r.body.ok && r.body.result;
        if (!res || typeof res.text !== "string") { said.className = "bad"; said.textContent = errText(r.body); said.hidden = false; return; }
        renderInstructions(res, { text: "Saved. Each lesson follows it from its next message." });
    });
    back.addEventListener("click", () => {
        btns.hidden = true;
        sure.hidden = false;
        fill(sure, h("span", { class: "grow" }, "Put back the text clanker-learn ships? Your version is lost."),
            h("button", { type: "button", onclick: () => { sure.hidden = true; btns.hidden = false; } }, "Keep mine"),
            h("button", { type: "button", class: "danger", onclick: async (ev) => {
                ev.target.disabled = true;
                const r = await tutorApi("POST", "/instructions/reset");
                const res = r.body.ok && r.body.result;
                if (!res || typeof res.text !== "string") { sure.hidden = true; btns.hidden = false; said.className = "bad"; said.textContent = errText(r.body); said.hidden = false; return; }
                renderInstructions(res, { text: "The shipped text is back. Each lesson follows it from its next message." });
            } }, "Put it back"));
    });
    box.replaceChildren(h("div", { class: "form" },
        h("h3", {}, "The tutor's instructions"),
        h("p", { class: "note" }, "What every tutor is given with each of your messages, in every project. " +
            "A lesson under way follows a new text from its next message, starting again from its files."),
        where, text, h("div", { class: "instr-foot" }, unsaved), said, btns, sure));
    box.instr = { dirty };
}

// ---- Settings -> Software (RS's Sandboxes -> Software: renderSoftwareScreen and its lane) -----

function showSettings(part) {
    collapseRailAfterUse();
    S.page = "settings";
    saveLayout();
    refreshSoftware();
    refreshTutorSettings();
    refreshBackups();
    render();
    renderMode();
    // "mode": the line under the project list leads to that part of Settings.
    if (part === "mode" && S.shell) S.shell.settings.mode.scrollIntoView({ block: "start" });
}

function makeSettingsPane() {
    const table = h("div", { class: "sw-table" }, h("div", { class: "none" }, "Asking…"));
    const said = h("p", { class: "muted sw-said" });
    const tutor = h("div", { class: "card-body tutor-set" }, h("div", { class: "none" }, "Asking…"));
    const agent = h("div", { class: "card-body tutor-agent" });
    const instr = h("div", { class: "card-body tutor-instr" }, h("div", { class: "none" }, "Asking…"));
    const backup = h("div", { class: "card-body backup-set" }, h("div", { class: "none" }, "Asking…"));
    const mode = h("div", { class: "card mode-set" }, h("div", { class: "card-head" }, "How it runs"), h("div", { class: "none" }, "Asking…"));
    const el = h("div", { class: "pane page", hidden: true }, h("div", { class: "page-inner" },
        h("h1", {}, "Settings"),
        h("div", { class: "card" }, h("div", { class: "card-head" }, "Interface"), makeInterfaceSettings(),
            h("p", { class: "note if-hint" }, "Kept by this browser only; they apply at once.")),
        h("p", { class: "sub" }, "The software clanker-learn deploys into the projects' sandboxes. Each is fetched in a throwaway container and copied into a project from there."),
        h("div", { class: "card" }, h("div", { class: "card-head" }, "Software"), table,
            h("p", { class: "note sw-hint" }, "Running projects keep the agent versions they have until Update is pressed in their Config box (Agents); new projects get these. " +
                "A newer editor reaches a project at its next start.")),
        said,
        h("div", { class: "card" }, h("div", { class: "card-head" }, "Tutor"), tutor, agent, instr),
        h("div", { class: "card" }, h("div", { class: "card-head" }, "Backup"), backup),
        mode));
    return { el, table, said, tutor, agent, instr, backup, mode };
}

// The build lane's target for a row (the broker's build_target).
const swTarget = (row) => row.kind === "agent" ? `agent:${row.id}` : "editor";
const swVerb = (row, what) => (row.kind === "agent" ? "agent_" : "editor_") + what;

async function refreshSoftware() {
    if (!S.loggedIn || refreshSoftware.busy) return;
    refreshSoftware.busy = true;
    try {
        const r = await api("GET", "/broker/software");
        if (!S.loggedIn) return;
        if (r.body.ok && Array.isArray(r.body.result)) { S.software = r.body.result; S.softwareError = null; }
        else S.softwareError = errText(r.body);
        renderSoftware();
    } finally {
        refreshSoftware.busy = false;
    }
}

function swTell(text, bad) {
    if (!S.shell) return;
    S.shell.settings.said.className = (bad ? "bad" : "muted") + " sw-said";
    S.shell.settings.said.textContent = text;
}

function fmtWhen(s) {
    const d = new Date(s);
    return s && !isNaN(d) ? d.toLocaleString() : String(s || "");
}

function renderSoftware() {
    if (!S.shell) return;
    const table = S.shell.settings.table;
    const rows = S.software;
    if (!rows) { table.replaceChildren(h("div", { class: S.softwareError ? "none bad" : "none" }, S.softwareError || "Asking…")); return; }
    // One build at a time: while one runs, nothing that would start another is offered.
    const anyBuild = Object.keys(S.builds).length > 0 || rows.some((r) => r && r.building);
    const out = [h("div", { class: "sw-row sw-head" },
        h("span", {}, "Software"), h("span", {}, "Pulled"), h("span", {}, "In effect"), h("span", {}, "Status"), h("span", {}))];
    for (const row of rows) {
        if (!row || typeof row !== "object") continue;
        const target = swTarget(row);
        const build = S.builds[target];
        const busy = !!build || !!row.building;
        const why = busy ? "This is being built right now" : anyBuild ? "Another build is running; try again when it is done" : null;
        const status = busy
            ? h("span", { class: "sw-busy" }, build && /_refresh$/.test(build.verb) ? "refreshing…" : "pulling…",
                build ? " " : null,
                build ? h("button", { class: "link", onclick: () => followOp(build.opId, swTitle(build.verb, row), { onEnd: (ok) => buildEnded(target, build.opId, ok) }) }, "log") : null)
            : h("span", { class: row.status === "up to date" ? "good" : row.status === "not pulled" ? "muted" : "warnc" }, String(row.status || "—"));
        const pulled = row.pulled
            ? h("span", {}, h("code", {}, String(row.pulled.version)), h("span", { class: "note sw-when" }, fmtWhen(row.pulled.pulled_at)))
            : h("span", { class: "muted" }, "—");
        const pull = h("button", { disabled: !!why, title: why, onclick: () => pullDialog(row) }, "Pull");
        const refresh = h("button", { disabled: !!why || S.checking.has(target), title: why || (S.checking.has(target) ? "Still checking upstream" : null),
                                      onclick: () => refreshDialog(row) }, "Refresh");
        const reset = row.overridden ? h("button", { disabled: busy, title: `Put back ${row.repo_version}, the version clanker-learn ships (removes the local override)`,
                                                     onclick: () => pinReset(row, reset) }, "Use repo version") : null;
        out.push(h("div", { class: "sw-row" },
            h("span", { class: "sw-name" }, String(row.row || row.id)),
            pulled,
            h("span", {}, row.version ? h("code", {}, String(row.version)) : "—"),
            status,
            h("span", { class: "sw-act" }, pull, refresh, reset)));
        // The agent's editor extension, as RS's continuation sub-row.
        if (row.kind === "agent" && "ext_version" in row) {
            const have = row.pulled && row.pulled.ext_version;
            out.push(h("div", { class: "sw-row sw-sub" },
                h("span", { class: "sw-subname" }, "└ editor extension"),
                h("span", {}, have ? h("code", {}, String(have)) : "—"),
                h("span", {}, row.ext_version ? h("code", {}, String(row.ext_version)) : "—"),
                h("span", {}), h("span", {})));
        }
        // The pins a distribution bundles beside its own (pi: its private node, RS's rules
        // extension, pi-subagents, the tree's layout), RS's sub-rows: one moved reads stale.
        for (const x of (Array.isArray(row.extra_pins) ? row.extra_pins : [])) {
            if (!x || typeof x !== "object") continue;
            out.push(h("div", { class: "sw-row sw-sub" },
                h("span", { class: "sw-subname", title: String(x.key || "") }, "└ " + (EXTRA_PIN_WORDS[x.id] || String(x.label || x.id))),
                h("span", {}, x.pulled ? h("code", {}, String(x.pulled)) : "—"),
                h("span", {}, x.version ? h("code", {}, String(x.version)) : "—"),
                h("span", {}), h("span", {})));
        }
        if (row.problem) out.push(h("div", { class: "sw-row sw-sub" }, h("span", { class: "bad sw-problem" }, String(row.problem))));
    }
    if (S.softwareError) out.push(h("div", { class: "none bad" }, S.softwareError));
    table.replaceChildren(...out);
}

const EXTRA_PIN_WORDS = { node_ver: "node", rules_ver: "rules extension", subagents_ver: "pi-subagents", layout_ver: "layout" };

function swTitle(verb, row) {
    return (/_refresh$/.test(verb) ? "Refreshing " : "Pulling ") + (row ? row.row : verb);
}

// A build the page follows: registered, followed (in a dialog, or not), dropped at its end.
function followBuild(target, opId, verb, row, background) {
    S.builds[target] = { opId, verb };
    renderSoftware();
    return followOp(opId, swTitle(verb, row), { background, onEnd: (ok) => buildEnded(target, opId, ok) });
}

function buildEnded(target, opId, ok) {
    const b = S.builds[target];
    if (b && b.opId === opId) delete S.builds[target];
    renderSoftware();
    refreshSoftware();
}

// After a reload (or a login): every build in flight is found again and followed quietly;
// its end is told in a notice.
async function seedBuilds() {
    if (!S.software) await refreshSoftware();
    const r = await api("GET", "/broker/software/active");
    const ops = r.body.ok && r.body.result && Array.isArray(r.body.result.ops) ? r.body.result.ops : [];
    for (const op of ops) {
        if (!op || typeof op.target !== "string" || typeof op.op_id !== "string" || typeof op.verb !== "string") continue;
        if (S.builds[op.target]) continue;
        const row = (S.software || []).find((x) => swTarget(x) === op.target) || { row: op.target };
        followBuild(op.target, op.op_id, op.verb, row, true);
    }
}

// Start a build (Pull, or Refresh's "Bump + re-pull") from a dialog; its refusal stays in it.
async function startBuild(row, what, said, btn) {
    btn.disabled = true;
    const verb = swVerb(row, what);
    const body = row.kind === "agent" ? { verb, agent: row.id } : { verb };
    const r = await api("POST", "/broker/software/build", body);
    if (!r.body.ok || !r.body.op_id) {
        btn.disabled = false;
        said.textContent = "Failed: " + errText(r.body);
        return;
    }
    followBuild(swTarget(row), r.body.op_id, verb, row, false);
}

// RS's mgmtBuildDialog: confirm, then the log.
function pullDialog(row) {
    const said = h("p", { class: "bad" });
    const go = h("button", { class: "primary", onclick: () => startBuild(row, "pull", said, go) }, "Pull");
    openModal(`Pull ${row.row}`, [
        h("p", {}, `Fetch ${row.label} again at the version in effect, `, h("code", {}, String(row.version || "?")),
            row.kind === "agent" && row.ext_version ? [" (its editor extension at ", h("code", {}, String(row.ext_version)), ")"] : null,
            Array.isArray(row.extra_pins) && row.extra_pins.length
                ? [" with ", row.extra_pins.map((x, i) => [i ? ", " : "", `${EXTRA_PIN_WORDS[x.id] || x.label || x.id} `, h("code", {}, String(x.version || "?"))])]
                : null,
            ", in a throwaway container (a few minutes). The log is shown while it runs."),
        said,
    ], [h("button", { onclick: closeModal }, "Cancel"), go]);
}

// RS's mgmtRefreshDialog: ask upstream first; with something newer, offer "Bump + re-pull",
// else say there is nothing to do. The check can take a while; the dialog can be closed, and
// the row's Refresh stays off until the answer is in.
async function refreshDialog(row) {
    const target = swTarget(row);
    const bodyEl = h("div", {}, h("p", { class: "muted" }, "Checking upstream…"));
    const scrim = openModal(`Refresh ${row.row}`, bodyEl, [h("button", { onclick: closeModal }, "Close")]);
    const btns = scrim.querySelector(".btns");
    S.checking.add(target);
    renderSoftware();
    const body = row.kind === "agent" ? { verb: "agent_refresh_check", agent: row.id } : { verb: "editor_refresh_check" };
    const r = await api("POST", "/broker/software/refresh-check", body);
    S.checking.delete(target);
    renderSoftware();
    if (!scrim.isConnected) return;
    if (!r.body.ok || !r.body.result) {
        bodyEl.replaceChildren(h("p", { class: "bad" }, "Could not resolve upstream: " + errText(r.body)));
        return;
    }
    const res = r.body.result;
    const mono = (v) => h("code", {}, String(v || "(unset)"));
    const current = res.current, latest = res.latest;
    const extLatest = res.ext_latest || "";
    const cliDown = !!res.downgrade, extDown = !!res.ext_downgrade;
    const cliMoved = current !== latest && !cliDown;
    const extMoved = !!extLatest && res.ext_current !== extLatest && !extDown;
    if (!cliMoved && !extMoved) {
        const kids = [];
        if (cliDown) kids.push(h("p", {}, `${row.row}: upstream latest `, mono(latest), " is older than the pin ", mono(current), " — nothing to do."));
        if (extDown) kids.push(h("p", {}, "editor extension: upstream latest ", mono(extLatest), " is older than the pin ", mono(res.ext_current), " — nothing to do."));
        if (!kids.length) {
            kids.push(h("p", {}, `${row.row} is already at the upstream version `, mono(latest),
                extLatest ? [" (extension ", mono(extLatest), ")"] : null, ". Nothing to do; use Pull to fetch it again if the pulled copy is stale."));
        }
        bodyEl.replaceChildren(...kids);
        return;
    }
    const pinLine = (label, cur, up, moved, down) => h("p", {}, down
        ? [`${label}: pin `, mono(cur), " — kept (upstream ", mono(up), " is older)."]
        : [`${label}: pin `, mono(cur), moved ? " → upstream " : " — already at upstream ", mono(up), "."]);
    const said = h("p", { class: "bad" });
    const go = h("button", { class: "primary", onclick: () => startBuild(row, "refresh", said, go) }, "Bump + re-pull");
    fill(bodyEl,
        pinLine(row.row, current, latest, cliMoved, cliDown),
        extLatest ? pinLine("editor extension", res.ext_current, extLatest, extMoved, extDown) : null,
        h("p", {}, (extLatest && cliMoved && extMoved ? "Bump both pins to the versions above (at last check)" : "Bump to the version above (at last check)") +
            " in the local override (versions.local.env on the data volume) and re-pull? A few minutes; the rest of the page stays responsive."),
        said);
    btns.replaceChildren(h("button", { onclick: closeModal }, "Cancel"), go);
}

// "Use repo version": the row's pins out of the local override, then the table read again.
async function pinReset(row, btn) {
    btn.disabled = true;
    const r = await api("POST", "/broker/software/pin-reset", { software: row.id });
    if (!r.body.ok) { btn.disabled = false; swTell(errText(r.body), true); return; }
    const v = r.body.result && r.body.result.version;
    swTell(`${row.row}: back to ${v || row.repo_version}, the version clanker-learn ships. Use repo version changes the pin only: Pull to fetch it.`);
    refreshSoftware();
}

// ---- Settings -> How it runs: the mode (sysbox or privileged) ---------------------------------

async function refreshMode() {
    const [r, c] = await Promise.all([api("GET", "/broker/mode"), api("GET", "/broker/cert")]);
    if (!S.loggedIn) return;
    const m = r.body.ok && r.body.result ? r.body.result.mode : null;
    S.mode = m === "sysbox" || m === "privileged" ? m : null;
    S.cert = c.body.ok && c.body.result ? c.body.result : null;
    render();
    renderMode();
}

// One line under How it runs: who made the certificate and when it ends (RS's cert-tailscale).
function certLine() {
    const c = S.cert;
    if (!c) return h("p", { class: "muted" }, "The certificate could not be read.");
    const until = c.not_after ? new Date(c.not_after).toLocaleDateString() : "an unreadable date";
    if (c.provider === "self-signed") {
        return h("p", { class: "muted" }, `Certificate: clanker-learn's own, self-signed, until ${until}. ` +
            "The editor's previews (Markdown, notebooks) need a certificate the browser trusts; https://joaopn.github.io/clanker-learn/guide/configuration.html#a-trusted-certificate says how to get one from Tailscale.");
    }
    const due = c.state === "expired" || c.state === "renew-due";
    const when = c.state === "expired" ? `expired on ${until}` : `until ${until}`;
    const renew = c.provider.startsWith("tailscale:") ? "run ./setup cert-tailscale on the machine" : "replace it in the web server's folder";
    return h("p", { class: due ? "tutor-state" : "muted" }, due ? h("span", { class: "dot inline warn" }) : null,
        `${due ? " " : ""}Certificate: ${c.provider}, ${when}.` + (due ? ` Renew it: ${renew}.` : ""));
}

function renderMode() {
    if (!S.shell) return;
    const box = S.shell.settings.mode;
    const head = h("div", { class: "card-head" }, "How it runs");
    const said = S.mode === "privileged"
        ? [h("p", { class: "tutor-state" }, h("span", { class: "dot inline warn" }), " Privileged mode: the clanker-learn container was started with --privileged."),
            h("p", {}, "That container is root-equivalent on this machine, so what stands between a sandbox and the machine is an ordinary container boundary " +
                "and clanker-learn's guard rules. Sysbox adds a user namespace beneath them; the install guide says how to run it that way.")]
        : S.mode === "sysbox"
            ? [h("p", { class: "tutor-state" }, h("span", { class: "dot inline up" }), " Sysbox: the clanker-learn container runs under sysbox's runtime."),
                h("p", {}, "Root inside it is not root on this machine: on top of the sandboxes' own container boundary and clanker-learn's guard rules, " +
                    "the whole of clanker-learn lives in a user namespace of its own.")]
            : [h("p", { class: "muted" }, "The mode could not be read.")];
    box.replaceChildren(head, h("div", { class: "form" }, said, certLine()));
}

// ---- Settings -> Backup: make one (an operation of its own lane), list, download, remove ------

function fmtSize(n) {
    if (!Number.isFinite(n)) return "?";
    const units = ["bytes", "KB", "MB", "GB"];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return i === 0 ? `${n} bytes` : `${n.toFixed(1)} ${units[i]}`;
}

async function refreshBackups() {
    if (!S.loggedIn || refreshBackups.busy) return;
    refreshBackups.busy = true;
    try {
        const r = await api("GET", "/broker/backups");
        if (!S.loggedIn) return;
        if (r.body.ok && Array.isArray(r.body.result)) { S.backups = r.body.result; S.backupsError = null; }
        else S.backupsError = errText(r.body);
        renderBackups();
    } finally {
        refreshBackups.busy = false;
    }
}

function backupTell(text, bad) {
    if (!S.shell) return;
    const said = S.shell.settings.backup.querySelector(".backup-said");
    if (!said) return;
    said.className = (bad ? "bad" : "muted") + " backup-said";
    said.textContent = text;
    said.hidden = !text;
}

function renderBackups() {
    if (!S.shell) return;
    const box = S.shell.settings.backup;
    const keep = box.querySelector(".backup-said");
    const said = h("p", { class: keep ? keep.className : "muted backup-said" }, keep ? keep.textContent : "");
    said.hidden = !said.textContent;
    const about = h("div", { class: "form" },
        h("p", { class: "note" }, "A backup is one file with your projects' workspaces and home folders, the tutor's conversations and what it knows about you, and the settings; " +
            "not the fetched software, which is fetched again, nor what was installed into a sandbox outside its workspace and home folder, which the project's setup recipe brings back."),
        h("p", { class: "note" }, "It holds the password's hash, the Claude token and every project's keys: keep it as safely as those. " +
            "Restoring one is a command, ", h("code", {}, "cl restore"), " (the install guide says how), because it replaces everything."));
    const busy = !!S.backupOp;
    const make = h("button", { class: "primary", disabled: busy, title: busy ? "A backup is being made" : null, onclick: () => makeBackup(make) }, "Make a backup");
    const doing = busy ? h("span", { class: "sw-busy grow" }, "Making a backup… ",
        h("button", { class: "link", onclick: () => followOp(S.backupOp, "Making a backup") }, "log")) : h("span", { class: "grow" });
    const rows = [];
    if (!S.backups) rows.push(h("div", { class: S.backupsError ? "none bad" : "none" }, S.backupsError || "Asking…"));
    else if (!S.backups.length) rows.push(h("div", { class: "none" }, "No backup yet."));
    for (const b of S.backups || []) {
        if (!b || typeof b.backup !== "string") continue;
        const projects = Array.isArray(b.projects) ? b.projects.map(String) : [];
        const running = Array.isArray(b.running) ? b.running.map(String) : [];
        const what = b.problem ? h("span", { class: "bad" }, String(b.problem))
            : h("span", { class: "muted" }, projects.length ? `${projects.length} project${projects.length === 1 ? "" : "s"}: ${projects.join(", ")}` : "no project",
                b.kind && b.kind !== "backup" ? ` (${String(b.kind)})` : null);
        const download = h("button", { onclick: () => downloadBackupDialog(b.backup) }, "Download");
        const remove = h("button", { onclick: () => removeBackupDialog(b.backup) }, "Remove");
        rows.push(h("div", { class: "bk-row" },
            h("div", { class: "bk-main" },
                h("div", { class: "bk-name" }, h("code", {}, b.backup)),
                h("div", { class: "bk-meta" }, h("span", {}, fmtSize(b.size)), b.made_at ? h("span", {}, fmtWhen(b.made_at)) : null, what),
                running.length ? h("div", { class: "warnc bk-ran" }, `Made while ${running.join(", ")} ${running.length === 1 ? "was" : "were"} running: files being written at that moment may be torn.`) : null),
            h("div", { class: "sw-act" }, download, remove)));
    }
    if (S.backups && S.backupsError) rows.push(h("div", { class: "none bad" }, S.backupsError));
    box.replaceChildren(about, h("div", { class: "bk-bar" }, doing, make), said, h("div", { class: "bk-list" }, rows));
}

// The backup the page follows: registered, followed (in a dialog, or not), dropped at its end.
function followBackup(opId, background) {
    S.backupOp = opId;
    renderBackups();
    return followOp(opId, "Making a backup", { background, onEnd: (ok) => {
        if (S.backupOp === opId) S.backupOp = null;
        backupTell(ok ? "" : "The backup failed: its log says why.", !ok);
        refreshBackups();
        renderBackups();
    } });
}

async function makeBackup(btn) {
    btn.disabled = true;
    backupTell("");
    const r = await api("POST", "/broker/backups");
    if (!r.body.ok || !r.body.op_id) { btn.disabled = false; backupTell(errText(r.body), true); return; }
    followBackup(r.body.op_id, false);
}

// After a reload (or a login): a backup in flight is found again and followed quietly.
async function seedBackup() {
    const r = await api("GET", "/broker/backups/active");
    const ops = r.body.ok && r.body.result && Array.isArray(r.body.result.ops) ? r.body.result.ops : [];
    const op = ops.find((o) => o && typeof o.op_id === "string");
    if (op && S.backupOp !== op.op_id) followBackup(op.op_id, true);
}

// Downloading asks for the password again too: an archive holds the password's hash, the Claude
// token and every project's keys. The web server answers a right password with a one-time
// address of this login's, opened as a plain download: the file goes to the browser's
// downloads as it comes, never through the page (the policy's form-action is 'none' anyway).
function downloadBackupDialog(name) {
    const pw = h("input", { type: "password", autocomplete: "current-password" });
    const said = h("p", { class: "bad" });
    const go = h("button", { class: "primary", type: "submit" }, "Download");
    const form = h("form", { method: "post", action: "#", onsubmit: async (ev) => {
        ev.preventDefault();
        if (!pw.value) { said.textContent = "Downloading a backup needs your password again."; return; }
        go.disabled = true;
        said.textContent = "";
        const proof = await deriveLoginProof(pw.value);
        pw.value = "";
        const r = await api("POST", `/broker/backups/${enc(name)}/download`, { proof });
        go.disabled = false;
        if (!r.body.ok || typeof r.body.url !== "string" || !r.body.url.startsWith("/broker/backups/file/")) { said.textContent = errText(r.body); pw.focus(); return; }
        closeModal();
        const a = h("a", { href: r.body.url, download: name, hidden: true });
        document.body.append(a);
        a.click();
        a.remove();
        backupTell(`${name} is being downloaded: your browser's downloads show how far it is.`);
    } }, h("p", {}, h("code", {}, name), " holds the password's hash, the Claude token and every project's keys. Type your password to download it."),
        formRow("Password", pw), said, h("div", { class: "btns" }, h("button", { type: "button", onclick: closeModal }, "Cancel"), go));
    openModal("Download a backup", form, []);
    pw.focus();
}

// Removing asks for the password again, as destroying a project does.
function removeBackupDialog(name) {
    const pw = h("input", { type: "password", autocomplete: "current-password" });
    const said = h("p", { class: "bad" });
    const go = h("button", { class: "danger", type: "submit" }, "Remove the backup");
    const form = h("form", { method: "post", action: "#", onsubmit: async (ev) => {
        ev.preventDefault();
        if (!pw.value) { said.textContent = "Removing a backup needs your password again."; return; }
        go.disabled = true;
        const proof = await deriveLoginProof(pw.value);
        pw.value = "";
        const r = await api("POST", "/broker/backups/remove", { backup: name, proof });
        go.disabled = false;
        if (!r.body.ok) { said.textContent = errText(r.body); return; }
        closeModal();
        backupTell(`${name} is removed.`);
        refreshBackups();
    } }, h("p", {}, h("code", {}, name), " goes for good: it may be the only copy of a project destroyed since. Type your password to confirm."),
        formRow("Password", pw), said, h("div", { class: "btns" }, h("button", { type: "button", onclick: closeModal }, "Cancel"), go));
    openModal("Remove a backup", form, []);
    pw.focus();
}

// ---- dialogs: a new project, a project's actions, an operation's progress -------------------

let modal = null;
let allProjectsRedraw = null;   // while ▤ All projects is open: redraws its rows

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
    allProjectsRedraw = null;
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
    // Filled from a project file's settings: a value it has, or "none" for no limit.
    const set = (lim) => {
        for (const [k, input] of Object.entries(inputs)) {
            if (!lim || !(k in lim)) continue;
            input.value = lim[k] === null ? "none" : k === "memory" ? fmtMemory(lim[k]) : String(lim[k]);
        }
    };
    return { rows, values, set };
}

// ---- agents and the editor in a project (RS's create dialog and Config box, Agents) ------

const AGENT_LABELS = { claude: "Claude", pi: "pi" };
let modelListSeq = 0;

// The catalog of Claude's tiers, read once a login (null when the broker cannot serve it:
// the model field then takes free text only).
async function modelCatalog() {
    if (S.models) return S.models;
    const r = await api("GET", "/broker/models");
    if (r.body.ok && r.body.result && Array.isArray(r.body.result.models)) S.models = r.body.result;
    return S.models;
}

// Claude's model and effort on one line (RS's Agents box, Claude's tab): the model is free
// text with the catalog's tiers offered, the effort a select that follows the chosen tier
// (RS's modelPicker): a tier that takes no effort gets none, so the broker's refusal of one
// is not reached from here.
function claudeModelControls(cat, init, onChange) {
    const tiers = (cat && Array.isArray(cat.models)) ? cat.models : [];
    const listId = "claude-models-" + (++modelListSeq);
    const list = h("datalist", { id: listId }, tiers.map((m) => h("option", { value: m.alias }, m.label || m.alias)));
    const model = h("input", { type: "text", autocomplete: "off", spellcheck: "false", list: listId, placeholder: "Claude's own default" });
    model.value = (init && init.model) || "";
    const effort = h("select", {});
    const blurb = h("span", { class: "note" });
    let wanted = (init && init.effort) || "";
    const sync = () => {
        const tier = tiers.find((m) => m.alias === model.value.trim());
        const levels = tier ? (tier.efforts || []) : ((cat && cat.efforts) || []);
        blurb.textContent = tier && tier.blurb ? String(tier.blurb) : "";
        if (tier && !levels.length) {
            effort.replaceChildren(h("option", { value: "" }, "no effort levels"));
            effort.disabled = true;
            effort.value = "";
            return;
        }
        effort.replaceChildren(h("option", { value: "" }, "Claude's default"), ...levels.map((lv) => h("option", { value: lv }, lv)));
        effort.disabled = false;
        effort.value = levels.includes(wanted) ? wanted : "";
    };
    model.addEventListener("input", () => { sync(); if (onChange) onChange(); });
    effort.addEventListener("change", () => { wanted = effort.value; if (onChange) onChange(); });
    sync();
    const node = h("div", { class: "field" }, h("span", {}, "Claude's model and effort"),
        list,
        h("div", { class: "model-row" },
            h("div", { class: "model-cell model-name" }, h("span", { class: "model-cap" }, "model"), model),
            h("div", { class: "model-cell" }, h("span", { class: "model-cap" }, "effort"), effort)),
        blurb);
    return { node, model, effort, value: () => ({ model: model.value.trim(), effort: effort.disabled ? "" : effort.value }) };
}

// pi's model and thinking level (RS's Agents box, pi's tab): no catalog (pi lists its models
// only once a provider is logged in inside the project), so the model is free provider/model
// text; the broker checks it and its refusal is shown as it comes.
function piModelControls(cat, init) {
    const pi = (cat && cat.pi) || {};
    const levels = Array.isArray(pi.thinking_levels) ? pi.thinking_levels : ["off", "minimal", "low", "medium", "high", "xhigh", "max"];
    const model = h("input", { type: "text", autocomplete: "off", spellcheck: "false",
                               placeholder: pi.model_example || "provider/model (empty: pi's own pick)" });
    model.value = (init && init.model) || "";
    const thinking = h("select", {}, h("option", { value: "" }, "pi's default"), ...levels.map((lv) => h("option", { value: lv }, lv)));
    thinking.value = init && levels.includes(init.thinking) ? init.thinking : "";
    const node = h("div", { class: "field" }, h("span", {}, "pi's model and thinking level"),
        h("div", { class: "model-row" },
            h("div", { class: "model-cell model-name" }, h("span", { class: "model-cap" }, "model"), model),
            h("div", { class: "model-cell" }, h("span", { class: "model-cap" }, "thinking"), thinking)),
        h("span", { class: "note" }, "provider/model, as pi names it; empty: pi's own pick. Log in to the provider by running pi in the project's Shell."));
    return { node, model, thinking, value: () => ({ pi_model: model.value.trim(), pi_thinking: thinking.value }) };
}

// A toggle card (RS's box-opt-card): on, off, or not offered (with why).
function optCard(label, on, why) {
    const card = h("button", { type: "button", class: "opt-card" + (on ? " on" : ""), "aria-pressed": on ? "true" : "false",
                               disabled: !!why, title: why || null }, label);
    const set = (v) => {
        card.classList.toggle("on", v);
        card.setAttribute("aria-pressed", v ? "true" : "false");
    };
    card.addEventListener("click", () => set(!card.classList.contains("on")));
    return { card, on: () => card.classList.contains("on") && !card.disabled, set };
}

const softwareRow = (rows, id) => (rows || []).find((r) => r && r.id === id) || null;

function newProjectDialog() {
    const name = h("input", { type: "text", autocomplete: "off", spellcheck: "false", maxlength: 31, placeholder: "rust-ownership" });
    const limits = limitsInputs(null);
    const limitsBox = h("details", { class: "more" }, h("summary", {}, "Limits (optional)"), limits.rows);
    const said = h("p", { class: "muted" }, NAME_RULE);
    const go = h("button", { class: "primary", type: "submit", disabled: true }, "Create");
    // Empty (a new project) or From a file (a project file a project's Export made): the file is
    // uploaded when picked, and what its manifest says fills the name, the limits and the editor.
    let fromFile = false, upload = null, manifest = null, editorCard = null;
    const picker = h("input", { type: "file", accept: ".clproject" });
    const fileSaid = h("p", { class: "note" }, "A .clproject file, made by a project's Export.");
    const fileBox = h("div", { class: "field", hidden: true }, h("span", {}, "Project file"), picker, fileSaid);
    const modeEmpty = h("button", { type: "button", class: "on" }, "Empty");
    const modeFile = h("button", { type: "button" }, "From a file");
    const setMode = (file) => {
        fromFile = file;
        modeEmpty.className = file ? "" : "on";
        modeFile.className = file ? "on" : "";
        fileBox.hidden = !file;
    };
    modeEmpty.addEventListener("click", () => setMode(false));
    modeFile.addEventListener("click", () => setMode(true));
    const fromManifest = () => {
        if (!manifest) return;
        const st = manifest.settings && typeof manifest.settings === "object" ? manifest.settings : {};
        if (editorCard && typeof st.editor === "boolean") editorCard.set(st.editor);
    };
    picker.addEventListener("change", async () => {
        const file = picker.files && picker.files[0];
        upload = manifest = null;
        if (!file) { fileSaid.className = "note"; fileSaid.textContent = "A .clproject file, made by a project's Export."; return; }
        fileSaid.className = "note";
        fileSaid.textContent = "Uploading…";
        picker.disabled = true;
        const r = await api("POST", "/broker/project-file", file);
        picker.disabled = false;
        if (!r.body.ok || typeof r.body.upload !== "string" || !r.body.manifest || typeof r.body.manifest !== "object") {
            fileSaid.className = "bad";
            fileSaid.textContent = errText(r.body);
            return;
        }
        upload = r.body.upload;
        manifest = r.body.manifest;
        const from = typeof manifest.project === "string" ? manifest.project : "a project";
        fileSaid.className = "note";
        fileSaid.textContent = `From ${from}'s file of ${dayOf(manifest.made_at) || "an unknown day"}, ${manifest.conversations ? "with" : "without"} its conversations.`;
        if (typeof manifest.project === "string") name.value = manifest.project;
        const st = manifest.settings && typeof manifest.settings === "object" ? manifest.settings : {};
        if (st.limits && typeof st.limits === "object") { limits.set(st.limits); limitsBox.open = true; }
        fromManifest();
    });
    // The Agents box (RS's: open by default) and the editor's card, filled once the broker
    // said what is pulled.
    const agentsBody = h("div", {}, h("p", { class: "note" }, "Asking which software is pulled…"));
    const editorBody = h("div", {});
    let chosen = () => ({ agents: [], editor: false, claude_model: "", claude_effort: "" });
    // The brief set its tutors are told (Default preselected); given once the project is made.
    const briefBody = h("div", {}, h("p", { class: "note" }, "Asking which brief sets there are…"));
    let briefPick = null;
    (async () => {
        const { sets, r: rs } = await briefSets();
        if (!sets || !sets.length) {
            fill(briefBody, h("p", { class: "note" }, `The brief sets could not be read (${errText(rs.body)}): the project uses Default.`));
            return;
        }
        briefPick = briefSetSelect(sets, null);
        fill(briefBody, briefPick, h("p", { class: "note" }, "What its tutors are told about you: that set's goals, preferences and background, and the global insights."));
    })();
    (async () => {
        const [sw, cat] = await Promise.all([api("GET", "/broker/software"), modelCatalog()]);
        const rows = sw.body.ok && Array.isArray(sw.body.result) ? sw.body.result : [];
        const claude = softwareRow(rows, "claude");
        const pi = softwareRow(rows, "pi");
        const editor = softwareRow(rows, "code-server");
        const notPulled = "Not pulled yet: pull it under Settings → Software first.";
        const claudeCard = optCard(AGENT_LABELS.claude, !!(claude && claude.pulled), claude && claude.pulled ? null : notPulled);
        // pi is never ticked by itself: a second agent is a choice.
        const piCard = optCard(AGENT_LABELS.pi, false, pi && pi.pulled ? null : notPulled);
        const controls = claudeModelControls(cat, null);
        const piControls = piModelControls(cat, null);
        const hint = h("p", { class: "note" });
        const sync = () => {
            controls.node.hidden = !claudeCard.on();
            piControls.node.hidden = !piCard.on();
            hint.textContent = !(claude && claude.pulled) && !(pi && pi.pulled)
                ? "No agent is pulled yet — pull Claude Code (or pi) under Settings → Software. Without one the project starts with none."
                : claudeCard.on() || piCard.on() ? "" : "No agent: the project starts without one.";
            hint.hidden = !hint.textContent;
        };
        claudeCard.card.addEventListener("click", sync);
        piCard.card.addEventListener("click", sync);
        sync();
        fill(agentsBody,
            sw.body.ok ? null : h("p", { class: "bad" }, "What is pulled could not be read: " + errText(sw.body)),
            h("div", { class: "opt-cards" }, claudeCard.card, piCard.card),
            pi && !pi.pulled ? h("p", { class: "note" }, "pi is not pulled yet: pull it under Settings → Software to offer it here.") : null,
            controls.node, piControls.node, hint);
        editorCard = optCard("Editor (code-server)", !!(editor && editor.pulled), editor && editor.pulled ? null : notPulled);
        fromManifest();
        fill(editorBody, h("div", { class: "opt-cards" }, editorCard.card),
            editor && editor.pulled ? null : h("p", { class: "note" }, "The editor is not pulled yet: pull it under Settings → Software."));
        chosen = () => {
            const on = claudeCard.on(), piOn = piCard.on();
            const mv = on ? controls.value() : { model: "", effort: "" };
            const out = { agents: [...(on ? ["claude"] : []), ...(piOn ? ["pi"] : [])], editor: editorCard.on(), claude_model: mv.model, claude_effort: mv.effort };
            if (piOn) Object.assign(out, piControls.value());
            return out;
        };
        go.disabled = false;
    })();
    const form = h("form", { method: "post", action: "#", onsubmit: async (ev) => {
        ev.preventDefault();
        if (fromFile && !upload) { said.className = "bad"; said.textContent = "Pick a project file first, or choose Empty."; return; }
        go.disabled = true;
        const fields = { name: name.value.trim(), ...limits.values(), ...chosen() };
        const r = fromFile ? await api("POST", "/broker/project-import", { ...fields, upload })
            : await api("POST", "/broker/project", fields);
        go.disabled = false;
        if (!r.body.ok) {
            said.className = "bad";
            const e = r.body.error || {};
            // A name the broker refuses is said with the form a name takes; any other
            // refusal (an agent not pulled, a tier that takes no effort) as it comes.
            said.textContent = e.kind === "validation" && !NAME_RE.test(name.value.trim())
                ? `“${name.value.trim()}” is not a project name. ${NAME_RULE}` : errText(r.body);
            return;
        }
        const project = name.value.trim();
        const briefSet = briefPick ? briefPick.value : null;
        S.current = S.creating = project;
        S.page = null;
        saveLayout();
        render();
        refreshProjects();
        let made = false;
        await followOp(r.body.op_id, `${fromFile ? "Importing" : "Creating"} ${project}`, { onEnd: (ok) => { made = ok; } });
        if (S.creating === project) S.creating = null;
        // Its brief set, through the tutor service; without it the project uses Default.
        if (made && S.loggedIn) {
            const rb = briefSet ? await tutorApi("PUT", `/project/${enc(project)}/brief-set`, { set: Number(briefSet) }) : null;
            if (!rb || !rb.body.ok) toast(`${project} uses the brief set Default: ${rb ? errText(rb.body) : "the brief sets could not be read"}. Change it in its Config box.`, true);
        }
        // A new project opens on Goals: the conversation that agrees a study plan.
        if (made && S.loggedIn && projectRow(project) && S.current === project) openGoals(project);
    } }, h("div", { class: "seg-toggle np-mode" }, modeEmpty, modeFile), fileBox,
    formRow("Name", name),
    h("details", { class: "more", open: true }, h("summary", {}, "Agents"), agentsBody),
    h("div", { class: "field" }, h("span", {}, "Editor"), editorBody),
    h("div", { class: "field" }, h("span", {}, "Brief"), briefBody),
    limitsBox, said,
    h("div", { class: "btns" }, h("button", { type: "button", onclick: closeModal }, "Cancel"), go));
    openModal("New project", form, []);
    name.focus();
}

// The Config box's Agents section (RS's appendAgentsBox / agentsBoxContent, cut to what a
// clanker-learn project has): what the project has, Add for an agent it has not, Claude's
// model and effort, and Update. Each acts on the running sandbox, live.
function agentsSection(name, p, box) {
    const op = opOf(name);
    const running = p.state === "running" && !op;
    // While an operation on the project runs, it is busy with that, whatever its state was.
    const notNow = (start) => (op ? `${name} is ${OP_WORDS[op.verb] || "busy with something"} now: wait until that is done.` : start);
    const agents = Array.isArray(p.agents) ? p.agents : [];
    const has = [...agents.map((a) => AGENT_NAMES[a] || a), p.editor ? "the editor (code-server)" : null].filter(Boolean);
    const said = h("p", { class: "muted" });
    const tell = (text, bad) => { said.className = bad ? "bad" : "muted"; said.textContent = text; };
    const body = h("div", {}, h("p", { class: "note" }, "Asking…"));
    const listed = has.length > 1 ? `${has.slice(0, -1).join(", ")} and ${has[has.length - 1]}` : has[0];
    box.append(h("p", {}, has.length ? `This project has ${listed}.` : "This project has no agent and no editor."), body, said);
    (async () => {
        const [sw, cat] = await Promise.all([api("GET", "/broker/software"), modelCatalog()]);
        const rows = sw.body.ok && Array.isArray(sw.body.result) ? sw.body.result : [];
        const parts = [];
        // Add, for an agent the project does not have.
        for (const agent of Object.keys(AGENT_LABELS)) {
            if (agents.includes(agent)) continue;
            const row = softwareRow(rows, agent);
            const why = !running ? notNow("Start the project to add an agent.") : !(row && row.pulled) ? "Pull it under Settings → Software first." : null;
            const add = h("button", { disabled: !!why, title: why, onclick: async () => {
                add.disabled = true;
                const r = await api("POST", projectPath(name, "/agents"), { agent });
                if (!r.body.ok) { add.disabled = false; tell(errText(r.body), true); return; }
                refreshProjects();
                followOp(r.body.op_id, `${name}: adding ${row ? row.label : agent}`);
            } }, `Add ${AGENT_LABELS[agent]}`);
            parts.push(h("div", { class: "kv" }, h("span", { class: "k" }, AGENT_LABELS[agent]),
                h("span", { class: "row-btns" }, add, h("span", { class: "note" }, why || "live, nothing restarted"))));
        }
        // Claude's model and effort, while the project runs.
        if (agents.includes("claude")) {
            const controls = claudeModelControls(cat, { model: p.claude_model || "", effort: p.claude_effort || "" });
            controls.model.disabled = !running;
            if (!running) controls.effort.disabled = true;
            const apply = h("button", { disabled: !running, onclick: async () => {
                const v = controls.value();
                apply.disabled = true;
                const r = await api("POST", projectPath(name, "/model"), v);
                apply.disabled = false;
                if (!r.body.ok) { tell(errText(r.body), true); return; }
                const res = r.body.result || {};
                const pair = [res.claude_model, res.claude_effort].filter(Boolean).join(", ");
                tell((pair ? `Claude's model and effort are now ${pair}.` : "Claude's model and effort are back to Claude's own defaults.") +
                     (res.applied === false ? " Claude Code's settings are not installed yet: they get this when they are." : " Written into Claude Code's settings; a session started from now on uses it."));
                refreshProjects();
            } }, "Apply");
            parts.push(controls.node, h("div", { class: "row-btns" }, apply,
                running ? null : h("span", { class: "note" }, notNow("Start the project to change Claude's model and effort."))));
        }
        // pi's model and thinking level, while the project runs.
        if (agents.includes("pi")) {
            const pc = piModelControls(cat, { model: p.pi_model || "", thinking: p.pi_thinking || "" });
            pc.model.disabled = pc.thinking.disabled = !running;
            const apply = h("button", { disabled: !running, onclick: async () => {
                apply.disabled = true;
                const r = await api("POST", projectPath(name, "/model"), pc.value());
                apply.disabled = false;
                if (!r.body.ok) { tell(errText(r.body), true); return; }
                const res = r.body.result || {};
                const pair = [res.pi_model, res.pi_thinking && `thinking ${res.pi_thinking}`].filter(Boolean).join(", ");
                tell((pair ? `pi's model and thinking level are now ${pair}.` : "pi's model and thinking level are back to pi's own pick.") +
                     (res.pi_applied === false ? " pi's settings are not installed yet: they get this when they are." : " Written into pi's settings; a session started from now on uses it."));
                refreshProjects();
            } }, "Apply");
            parts.push(pc.node, h("div", { class: "row-btns" }, apply,
                running ? null : h("span", { class: "note" }, notNow("Start the project to change pi's model and thinking level."))));
        }
        // Update: the pulled versions into the running project (RS's mgmtAgentsUpdateDialog).
        if (agents.length) {
            const upd = h("button", { disabled: !running, title: running ? "Deploy the pulled agent versions into this project" : notNow("Start the project to update its agents."),
                                      onclick: () => agentsUpdateDialog(name, agents, rows) }, "Update");
            parts.push(h("div", { class: "row-btns update-row" }, upd,
                h("span", { class: "note" }, running ? "deploys the pulled versions, live" : notNow("Start the project to update its agents."))));
        }
        body.replaceChildren(...parts);
    })();
}

function agentsUpdateDialog(name, agents, rows) {
    const versions = agents.map((a) => {
        const row = softwareRow(rows, a);
        if (!row || !row.pulled) return h("li", {}, `${a}: not pulled`);
        return h("li", {}, `${row.label} ${row.pulled.version}` + (row.pulled.ext_version ? ` (editor extension ${row.pulled.ext_version})` : ""),
            row.status === "up to date" ? "" : " — the pulled copy is stale; press Pull in Settings → Software first to get the version in effect");
    });
    const said = h("p", { class: "bad" });
    const go = h("button", { class: "primary", onclick: async () => {
        go.disabled = true;
        const r = await api("POST", projectPath(name, "/agents_update"));
        if (!r.body.ok) { go.disabled = false; said.textContent = errText(r.body); return; }
        refreshProjects();
        followOp(r.body.op_id, `Updating the agents of ${name}`, { endNote: AGENTS_UPDATE_HINT });
    } }, "Update");
    openModal(`Update the agents of ${name}`, [
        h("p", {}, "Deploys the versions pulled on this machine:"),
        h("ul", { class: "plain" }, versions),
        h("p", { class: "note" }, AGENTS_UPDATE_HINT),
        said,
    ], [h("button", { onclick: closeModal }, "Cancel"), go]);
}

// ▤ All projects: every project with its state, archived ones with Unarchive. The box stays
// open while one comes back (its operation followed without a dialog), and redraws its rows
// whenever the projects are read again.
function allProjectsDialog() {
    const list = h("div", { class: "allp" });
    const errors = new Map();   // project -> what its Unarchive was told
    const pending = new Set();  // asked, the operation not yet seen
    const unarchive = async (name) => {
        pending.add(name);
        errors.delete(name);
        draw();
        const r = await api("POST", projectPath(name, "/unarchive"));
        if (!r.body.ok || !r.body.op_id) {
            pending.delete(name);
            errors.set(name, errText(r.body));
            draw();
            return;
        }
        refreshProjects();
        // Followed without a dialog (it would replace this box); its end reads the projects again.
        followOp(r.body.op_id, `${name}: ${OP_WORDS.unarchive}`, { background: true, onEnd: () => { pending.delete(name); } });
    };
    const draw = () => {
        const rows = S.projects.map((p) => {
            const op = opOf(p.name);
            const st = pending.has(p.name) && !op ? { text: OP_WORDS.unarchive, cls: "busy" } : stateWords(p);
            const err = errors.get(p.name);
            return h("div", { class: "allp-row" },
                h("span", { class: `dot inline ${st.cls}` }),
                h("span", { class: "grow" }, h("span", { class: "allp-name" }, p.name),
                    h("span", { class: "note" }, " · ", st.text, p.archived ? " · archived" : "")),
                p.archived ? h("button", { disabled: !!op || pending.has(p.name), onclick: () => unarchive(p.name) }, "Unarchive") : null,
                err ? h("p", { class: "bad allp-err" }, err) : null);
        });
        if (!rows.length) rows.push(h("p", { class: "muted" }, "No project yet."));
        list.replaceChildren(...rows);
    };
    draw();
    openModal("All projects", [
        h("p", { class: "note" }, "Unarchive brings a project back to the sidebar, stopped: start it from its ⚙."),
        list,
    ], [h("button", { onclick: closeModal }, "Close")]);
    allProjectsRedraw = draw;
}

// RS's words (agentsUpdateHint, for a project whose own terminal has the agents).
const AGENTS_UPDATE_HINT = "Applied live — nothing is recreated, and logins, sessions and settings are kept. " +
    "The project's Shell gets these versions, including over an agent you updated yourself. " +
    "Restart running agent sessions afterwards to use the new versions.";

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
    const forget = h("input", { type: "checkbox" });
    const destroyGo = h("button", { class: "danger", type: "submit", disabled: !!op }, "Destroy the project");
    const destroyForm = h("form", { method: "post", action: "#", onsubmit: async (ev) => {
        ev.preventDefault();
        if (!pw.value) { said.className = "bad"; said.textContent = "Destroying needs your password again."; return; }
        destroyGo.disabled = true;
        const proof = await deriveLoginProof(pw.value);
        pw.value = "";
        const r = await api("POST", projectPath(name, "/destroy"), { proof, forget: forget.checked });
        destroyGo.disabled = false;
        if (!r.body.ok) { said.className = "bad"; said.textContent = errText(r.body); return; }
        followOp(r.body.op_id, `Destroying ${name}`);
    } }, h("p", { class: "note" }, "The sandbox, its workspace and its ports go, for good. Type your password to confirm."),
        h("label", { class: "checkrow" }, forget, " also forget what I learned here"),
        h("p", { class: "note" }, "Otherwise the lessons' summaries and the goals and preferences you stated there stay for your other projects' tutors, and this project's insights stay on the Knowledge page for you, told to no tutor; all marked as from a removed project. Its conversations go either way."),
        formRow("Password", pw), h("div", { class: "btns" }, destroyGo));
    const running = p.state === "running";
    const agentsBox = h("details", { class: "more agents-box", open: true }, h("summary", {}, "Agents"));
    agentsSection(name, p, agentsBox);
    const briefBox = h("details", { class: "more", open: true }, h("summary", {}, "Brief"));
    briefSection(name, briefBox);
    const exportBox = h("details", { class: "more" }, h("summary", {}, "Export"));
    exportSection(name, exportBox, !!op);
    openModal(name, [
        h("div", { class: "kv" }, h("span", { class: "k" }, "Sandbox"),
            h("span", {}, h("span", { class: `dot inline ${st.cls}` }), " ", st.text, " ",
                op ? h("button", { class: "link", onclick: () => followOp(op.op_id, `${name}: ${OP_WORDS[op.verb] || op.verb}`) }, "show the log") : null)),
        h("div", { class: "kv" }, h("span", { class: "k" }, ""),
            h("span", { class: "row-btns" }, running ? action("stop", "Stop") : action("start", "Start", "primary"),
                action("recreate", "Recreate"), action("archive", "Archive"))),
        h("p", { class: "note" }, "Recreate makes a new sandbox from the current image, with the same workspace and home folder; its setup recipe runs again. " +
            "Archive stops the project and hides it from the sidebar; ▤ All projects brings it back."),
        p.problem ? h("p", { class: "bad" }, String(p.problem)) : null,
        agentsBox,
        briefBox,
        h("details", { class: "more" }, h("summary", {}, "Limits"), limitsForm),
        exportBox,
        h("details", { class: "more danger-zone" }, h("summary", {}, "Destroy"), destroyForm),
        said,
    ], [h("button", { onclick: closeModal }, "Close")]);
}

// The Config box's Brief: the brief set the project's tutors are told, changed at once (a lesson
// takes it at its next start).
function briefSection(name, box) {
    const body = h("div", {}, h("p", { class: "note" }, "Asking…"));
    box.append(body);
    (async () => {
        const [{ sets, r: rs }, cur] = await Promise.all([briefSets(), tutorApi("GET", `/project/${enc(name)}/brief-set`)]);
        const now = cur.body.ok && cur.body.result && cur.body.result.set;
        if (!sets || !sets.length || !now) { fill(body, h("p", { class: "bad" }, errText((sets && sets.length ? cur : rs).body))); return; }
        const pick = briefSetSelect(sets, now.id);
        const said = h("p", { class: "note" }, "A lesson takes a new brief set at its next start; Knowledge → Your briefs shows and edits each set.");
        pick.addEventListener("change", async () => {
            pick.disabled = true;
            const r = await tutorApi("PUT", `/project/${enc(name)}/brief-set`, { set: Number(pick.value) });
            pick.disabled = false;
            if (!r.body.ok) { said.className = "bad"; said.textContent = errText(r.body); pick.value = String(now.id); return; }
            now.id = r.body.result.set.id;
            said.className = "note";
            said.textContent = `${name} now uses the brief set “${r.body.result.set.name}”: a lesson takes it at its next start.`;
        });
        fill(body, h("div", { class: "field" }, h("span", {}, "Brief set"), pick), said);
    })();
}

// The Config box's Export: the project as one file, made by an operation and then downloaded
// as a backup is (a one-time address of this login's, opened as a plain download).
function exportSection(name, box, busy) {
    const conversations = h("input", { type: "checkbox" });
    const said = h("p", { class: "muted" });
    const go = h("button", { class: "primary", disabled: busy, onclick: async () => {
        go.disabled = true;
        said.className = "muted";
        said.textContent = "";
        const r = await api("POST", projectPath(name, "/export"), { conversations: conversations.checked });
        go.disabled = false;
        if (!r.body.ok || !r.body.op_id) { said.className = "bad"; said.textContent = errText(r.body); return; }
        refreshProjects();
        let made = null;
        await followOp(r.body.op_id, `Exporting ${name}`, { onEnd: (ok, result) => { made = ok ? result : null; } });
        if (!made || !S.loggedIn) return;
        const d = await api("POST", projectPath(name, "/export-download"), { op_id: r.body.op_id });
        if (!d.body.ok || typeof d.body.url !== "string" || !d.body.url.startsWith("/broker/project-file/file/")) {
            toast(`The project file of ${name} could not be downloaded: ${errText(d.body)}`, true);
            return;
        }
        const file = made.result && typeof made.result.name === "string" ? made.result.name : `${name}.clproject`;
        const a = h("a", { href: d.body.url, download: file, hidden: true });
        document.body.append(a);
        a.click();
        a.remove();
        const words = made.result && typeof made.result.said === "string" ? made.result.said : file;
        toast(`${words} Your browser's downloads show how far it is.`);
    } }, "Download project file");
    box.append(
        h("p", { class: "note" }, "Always in: the workspace folder, the study plan and its lessons, the project's limits and editor. " +
            "With your conversations: what was said in each lesson, the lessons' states, each lesson's transcript and notes; without them every lesson arrives not started. " +
            "Never in: the home folder (the Claude login and the agents' history), the project's keys and Shell password, your knowledge base."),
        h("p", { class: "note warnc" }, "Everything in the workspace folder goes in as it is, a .env with a key included."),
        h("label", { class: "checkrow" }, conversations, " Include my conversations"),
        h("div", { class: "btns" }, go), said);
}

// opts.quiet (the row's Start/Stop): followed without the dialog, a notice only on failure.
async function runAction(name, verb, opts) {
    opts = opts || {};
    const r = await api("POST", projectPath(name, `/${verb}`));
    if (!r.body.ok) { toast(`${name}: ${errText(r.body)}`, true); refreshProjects(); return; }
    refreshProjects();
    // A stop pressed here, once it ended well, closes the project (RS's teardownProjectState):
    // its panes go with their sockets and frames, so nothing keeps connecting to the stopped
    // sandbox, and its lessons fold. Its tab arrangement and lesson list are kept for its next run.
    const onEnd = (ok) => {
        if (!ok || verb !== "stop") return;
        for (const key of Object.keys(S.panes)) if (key.startsWith(`${name}|`)) dropPane(key);
        // Its tabs list goes too (RS: delete state.projectServices[name]): reopened, a stale list
        // would make an Editor frame on the stopped sandbox before the fresh list comes.
        delete S.services[name];
        S.expanded.delete(name);
        if (S.current === name) S.current = null;
        saveLayout();
        render();
    };
    await followOp(r.body.op_id, `${name}: ${OP_WORDS[verb] || verb}`, { onEnd, background: !!opts.quiet, quiet: !!opts.quiet });
    // A Shell the stop or recreate closed connects again once the sandbox runs (see its update).
    const shell = S.panes[paneKey(name, "shell")];
    if (shell && (shell.state === "closed" || shell.state === "failed")) { shell.wasDown = true; shell.update(); }
}

// Follow an operation: its log as it grows, then its result. The dialog can be closed; the
// operation goes on in the broker, and the rail says so. opts.background follows it without
// a dialog (a build found again after a reload); opts.quiet leaves out its end notice unless it
// failed; opts.onEnd(ok) is told when it is over.
async function followOp(opId, title, opts) {
    opts = opts || {};
    const state = h("p", { class: "opstate" }, "Running…");
    const log = h("pre", { class: "oplog" });
    const scrim = opts.background ? null
        : openModal(title, [state, log], [h("button", { onclick: closeModal }, "Close")]);
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
    if (opts.endNote && ok) state.after(h("p", { class: "note" }, opts.endNote));
    // After a destroy: whether the tutor service was told the project is gone.
    const kn = ok && result.knowledge && typeof result.knowledge === "object" ? result.knowledge : null;
    const knWords = !kn ? "" : kn.told ? (kn.forgotten ? "What you learned in this project is forgotten." : "What you learned in this project is kept, marked as from a removed project.")
        : String(kn.message || "The tutor service was not told the project is gone.");
    if (kn) state.after(h("p", { class: kn.told ? "note" : "bad" }, knWords));
    if ((!scrim || !scrim.isConnected) && (!opts.quiet || !ok)) toast(`${title}: ${ok ? "done" : "failed"}${msg}${kn && !kn.told ? `. ${knWords}` : ""}`, !ok || (kn && !kn.told));
    if (kn && S.page === "knowledge") renderKnowledge();
    if (opts.onEnd) opts.onEnd(ok, result);
    await refreshProjects();
    if (S.current) refreshServices(S.current);
}

// ---- go ------------------------------------------------------------------------------------

window.addEventListener("DOMContentLoaded", () => { applyInterfaceSettings(); installRailOutsideClickHandlers(); start(); });
