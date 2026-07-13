"use strict";

const state = {
    offset: 0,
    limit: 100,
    total: 0,
    query: "",
    selectedId: null,
    enums: null,
    dirty: false,
};

async function apiGet(path) {
    const r = await fetch(path);
    if (!r.ok) throw new Error(`${path}: ${r.status} ${await r.text()}`);
    return r.json();
}
async function apiSend(method, path, body) {
    const r = await fetch(path, {
        method,
        headers: { "Content-Type": "application/json" },
        body: body ? JSON.stringify(body) : undefined,
    });
    if (!r.ok) throw new Error(`${path}: ${r.status} ${await r.text()}`);
    return r.json();
}

async function loadEnums() {
    state.enums = await apiGet("/api/enums");
}

async function loadList() {
    const params = new URLSearchParams({
        offset: state.offset,
        limit: state.limit,
    });
    if (state.query) params.set("q", state.query);
    const data = await apiGet("/api/items?" + params.toString());
    state.total = data.total;
    const list = document.getElementById("item-list");
    list.innerHTML = "";
    for (const item of data.items) {
        const li = document.createElement("li");
        li.dataset.id = item.id;
        if (item.id === state.selectedId) li.classList.add("selected");
        li.innerHTML = `
            <div>
                <div class="item-name">${escapeHtml(item.name)}</div>
                <div class="item-meta">#${item.id} · ${item.item_type_name} · lvl ${item.level}</div>
            </div>
            <div class="item-meta">${(item.slot_names || []).join(", ")}</div>
        `;
        li.addEventListener("click", () => selectItem(item.id));
        list.appendChild(li);
    }
    const pageInfo = document.getElementById("page-info");
    const from = Math.min(state.offset + 1, state.total);
    const to = Math.min(state.offset + data.items.length, state.total);
    pageInfo.textContent = state.total ? `${from}–${to} of ${state.total}` : "no matches";
    document.getElementById("prev-page").disabled = state.offset <= 0;
    document.getElementById("next-page").disabled = state.offset + state.limit >= state.total;
}

async function selectItem(itemId) {
    if (state.dirty && !confirm("Discard unsaved changes to this item?")) return;
    state.selectedId = itemId;
    state.dirty = false;
    for (const li of document.querySelectorAll("#item-list li")) {
        li.classList.toggle("selected", Number(li.dataset.id) === itemId);
    }
    const item = await apiGet(`/api/items/${itemId}`);
    renderEditor(item);
}

function renderEditor(item) {
    const editor = document.getElementById("editor");
    editor.innerHTML = "";

    const h = document.createElement("h2");
    h.textContent = `#${item.id} · ${escapeHtml(item.name)}`;
    editor.appendChild(h);

    const meta = document.createElement("p");
    meta.className = "item-meta";
    meta.textContent = `type: ${item.item_type_name}${item.is_weapon ? " · weapon fields shown" : ""}`;
    editor.appendChild(meta);

    editor.appendChild(sectionTitle("Basics"));
    const basics = fieldGrid([
        numField(item, "level", "Level to equip"),
        numField(item, "superior_level", "Item level"),
        numField(item, "flags", "Flags (u16)"),
        numField(item, "stack_size", "Stack size"),
        numField(item, "valid_targets", "Valid targets"),
        numField(item, "model", "Model id"),
        numField(item, "resource_id", "Resource id"),
        numField(item, "id", "Item id (rename)"),
    ]);
    editor.appendChild(basics);

    editor.appendChild(sectionTitle("Slots"));
    editor.appendChild(checkboxGrid(item, "slot_names", state.enums.slots));

    editor.appendChild(sectionTitle("Races"));
    editor.appendChild(checkboxGrid(item, "race_names", state.enums.races));

    editor.appendChild(sectionTitle("Jobs"));
    editor.appendChild(checkboxGrid(item, "job_names", state.enums.jobs));

    if (item.is_weapon) {
        editor.appendChild(sectionTitle("Weapon"));
        editor.appendChild(
            fieldGrid([
                numField(item, "damage", "Damage"),
                numField(item, "delay", "Delay"),
                numField(item, "dps", "DPS"),
                numField(item, "skill", "Skill"),
                numField(item, "jug_size", "Jug size"),
            ])
        );
    }

    editor.appendChild(sectionTitle("Timings"));
    editor.appendChild(
        fieldGrid([
            numField(item, "casting_time", "Casting time"),
            numField(item, "use_delay", "Use delay"),
            numField(item, "reuse_delay", "Reuse delay"),
            numField(item, "max_charges", "Max charges"),
            numField(item, "shield_size", "Shield size / skill"),
        ])
    );

    if (item.strings_block && item.strings_block.parsed) {
        editor.appendChild(sectionTitle("Name & description"));
        editor.appendChild(stringsEditor(item.strings_block));
    } else if (item.strings && item.strings.length) {
        editor.appendChild(sectionTitle("Strings preview (unparseable — read-only)"));
        const pre = document.createElement("div");
        pre.className = "strings-preview";
        pre.textContent = item.strings.join("\n");
        editor.appendChild(pre);
    }

    const actions = document.createElement("div");
    actions.className = "editor-actions";
    const applyBtn = document.createElement("button");
    applyBtn.textContent = "Apply changes";
    applyBtn.addEventListener("click", () => applyEdits(item.id));
    const revertBtn = document.createElement("button");
    revertBtn.className = "secondary";
    revertBtn.textContent = "Revert";
    revertBtn.addEventListener("click", () => selectItem(item.id));
    const status = document.createElement("span");
    status.id = "edit-status";
    status.className = "status";
    actions.append(applyBtn, revertBtn, status);
    editor.appendChild(actions);
}

function sectionTitle(text) {
    const el = document.createElement("div");
    el.className = "section-title";
    el.textContent = text;
    return el;
}

function fieldGrid(fields) {
    const wrap = document.createElement("div");
    wrap.className = "field-grid";
    for (const field of fields) wrap.appendChild(field);
    return wrap;
}

function numField(item, key, label) {
    const l = document.createElement("label");
    l.textContent = label;
    const input = document.createElement("input");
    input.type = "number";
    input.value = item[key] ?? 0;
    input.dataset.field = key;
    input.addEventListener("input", () => (state.dirty = true));
    l.appendChild(input);
    return l;
}

function stringsEditor(block) {
    const wrap = document.createElement("div");
    wrap.className = "strings-editor";
    wrap.dataset.stringsField = "1";
    const guessLabel = (i, e) => {
        if (e.kind === 1) return `Value #${i}`;
        // Retail slots by convention: 0=name, 1=singular log, 2=plural
        // log, 3=article/pronoun, 4=description. Show the convention
        // but don't enforce it — private servers reorder these.
        return ["Name", "Log name (singular)", "Log name (plural)", "Article", "Description"][i] || `String #${i}`;
    };
    block.entries.forEach((entry, i) => {
        const l = document.createElement("label");
        l.className = "string-label";
        const cap = document.createElement("span");
        cap.textContent = guessLabel(i, entry);
        l.appendChild(cap);
        let input;
        if (entry.kind === 1) {
            input = document.createElement("input");
            input.type = "number";
            input.value = entry.value ?? 0;
            input.dataset.kind = "integer";
        } else if ((entry.text || "").length > 40) {
            input = document.createElement("textarea");
            input.rows = 3;
            input.value = entry.text || "";
            input.dataset.kind = "string";
        } else {
            input = document.createElement("input");
            input.type = "text";
            input.value = entry.text || "";
            input.dataset.kind = "string";
        }
        input.dataset.index = String(i);
        input.addEventListener("input", () => (state.dirty = true));
        l.appendChild(input);
        wrap.appendChild(l);
    });
    return wrap;
}

function checkboxGrid(item, namesKey, options) {
    const wrap = document.createElement("div");
    wrap.className = "checkbox-grid";
    wrap.dataset.namesField = namesKey;
    const active = new Set(item[namesKey] || []);
    for (const opt of options) {
        const l = document.createElement("label");
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.value = opt.name;
        cb.checked = active.has(opt.name);
        cb.addEventListener("change", () => (state.dirty = true));
        l.append(cb, document.createTextNode(" " + opt.name));
        wrap.appendChild(l);
    }
    return wrap;
}

function collectPatch() {
    const patch = {};
    for (const input of document.querySelectorAll("#editor input[type=number]")) {
        if (!input.dataset.field) continue; // skip strings-editor inputs
        patch[input.dataset.field] = Number(input.value);
    }
    for (const grid of document.querySelectorAll("#editor .checkbox-grid")) {
        const key = grid.dataset.namesField;
        patch[key] = Array.from(grid.querySelectorAll("input:checked")).map((c) => c.value);
    }
    const stringsWrap = document.querySelector("#editor .strings-editor");
    if (stringsWrap) {
        const entries = [];
        for (const el of stringsWrap.querySelectorAll("[data-index]")) {
            const i = Number(el.dataset.index);
            if (el.dataset.kind === "integer") {
                entries[i] = { value: Number(el.value) };
            } else {
                entries[i] = { text: el.value };
            }
        }
        patch.strings_block = { entries };
    }
    return patch;
}

async function applyEdits(itemId) {
    const patch = collectPatch();
    const status = document.getElementById("edit-status");
    status.className = "status";
    status.textContent = "saving…";
    try {
        await apiSend("PUT", `/api/items/${itemId}`, patch);
        state.dirty = false;
        status.textContent = "applied (in memory — remember to Save)";
        status.classList.add("ok");
        await loadList();
    } catch (e) {
        status.textContent = "error: " + e.message;
        status.classList.add("err");
    }
}

function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
}

// Save dialog wiring.
function openSaveDialog() {
    document.getElementById("save-dialog").classList.remove("hidden");
    document.getElementById("save-status").textContent = "";
    document.getElementById("save-status").className = "status";
}
function closeSaveDialog() {
    document.getElementById("save-dialog").classList.add("hidden");
}
async function confirmSave() {
    const path = document.getElementById("save-path").value.trim();
    const status = document.getElementById("save-status");
    status.className = "status";
    status.textContent = "saving…";
    try {
        const r = await apiSend("POST", "/api/save", { path });
        status.textContent = "saved to " + r.saved_to;
        status.classList.add("ok");
    } catch (e) {
        status.textContent = e.message;
        status.classList.add("err");
    }
}

// Wire top-level controls once the DOM is ready.
document.addEventListener("DOMContentLoaded", async () => {
    await loadEnums();
    await loadList();

    document.getElementById("search").addEventListener("input", (e) => {
        state.query = e.target.value;
        state.offset = 0;
        loadList();
    });
    document.getElementById("prev-page").addEventListener("click", () => {
        state.offset = Math.max(0, state.offset - state.limit);
        loadList();
    });
    document.getElementById("next-page").addEventListener("click", () => {
        state.offset += state.limit;
        loadList();
    });

    document.getElementById("save-btn").addEventListener("click", openSaveDialog);
    document.getElementById("save-cancel").addEventListener("click", closeSaveDialog);
    document.getElementById("save-confirm").addEventListener("click", confirmSave);
});
