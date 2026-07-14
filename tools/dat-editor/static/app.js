"use strict";

const state = {
    offset: 0,
    limit: 100,
    total: 0,
    query: "",
    selectedId: null,
    enums: null,
    dirty: false,
    loaded: false,
    filename: "",
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

async function apiUpload(file) {
    const form = new FormData();
    form.append("file", file);
    const r = await fetch("/api/open", { method: "POST", body: form });
    if (!r.ok) throw new Error(`upload failed: ${r.status} ${await r.text()}`);
    return r.json();
}

async function loadEnums() {
    if (state.enums) return;
    state.enums = await apiGet("/api/enums");
}

async function loadStatus() {
    const s = await apiGet("/api/status");
    state.loaded = s.loaded;
    state.filename = s.filename || "";
    document.getElementById("filename").textContent = s.filename || "";
    document.getElementById("item-count").textContent = s.loaded ? `${s.item_count} items` : "";
    document.getElementById("download-btn").disabled = !s.loaded;
    document.getElementById("landing").classList.toggle("hidden", s.loaded);
    document.getElementById("layout").classList.toggle("hidden", !s.loaded);
    return s;
}

async function loadList() {
    if (!state.loaded) return;
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

const EQUIPMENT_TYPES = new Set(["ARMOR", "WEAPON"]);

function renderEditor(item) {
    const editor = document.getElementById("editor");
    editor.innerHTML = "";

    const h = document.createElement("h2");
    h.textContent = `#${item.id} · ${escapeHtml(item.name)}`;
    editor.appendChild(h);

    // Only the equipment-typed items store meaningful values in the
    // level/slots/races/jobs fields. For general items (BOOK, USABLE,
    // FURNISHING, etc.) those bytes are actually part of the strings
    // block, so surfacing them as editable fields would be misleading.
    const isEquipment = EQUIPMENT_TYPES.has(item.item_type_name);

    const meta = document.createElement("p");
    meta.className = "item-meta";
    let metaText = `type: ${item.item_type_name}`;
    if (!isEquipment) metaText += " · equipment-only fields hidden";
    editor.appendChild(meta);
    meta.textContent = metaText;

    editor.appendChild(sectionTitle("Basics"));
    const basicFields = [
        numField(item, "flags", "Flags (u16)"),
        numField(item, "stack_size", "Stack size"),
        numField(item, "valid_targets", "Valid targets"),
        numField(item, "resource_id", "Resource id"),
        numField(item, "id", "Item id (rename)"),
    ];
    if (isEquipment) {
        basicFields.unshift(
            numField(item, "level", "Level to equip"),
            numField(item, "superior_level", "Item level"),
        );
        basicFields.push(numField(item, "model", "Model id"));
    }
    editor.appendChild(fieldGrid(basicFields));

    if (isEquipment) {
        editor.appendChild(sectionTitle("Slots"));
        editor.appendChild(checkboxGrid(item, "slot_names", state.enums.slots));

        editor.appendChild(sectionTitle("Races"));
        editor.appendChild(checkboxGrid(item, "race_names", state.enums.races));

        editor.appendChild(sectionTitle("Jobs"));
        editor.appendChild(checkboxGrid(item, "job_names", state.enums.jobs));
    }

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

    if (isEquipment) {
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
    }

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
    for (const f of fields) wrap.appendChild(f);
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
        if (!input.dataset.field) continue;
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
        status.textContent = "applied — click Download when done";
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

async function openFile(file) {
    if (!file) return;
    const landingStatus = document.getElementById("landing-status");
    landingStatus.className = "status";
    landingStatus.textContent = `opening ${file.name}…`;
    try {
        await apiUpload(file);
        state.offset = 0;
        state.selectedId = null;
        await loadStatus();
        await loadEnums();
        await loadList();
    } catch (e) {
        landingStatus.textContent = "error: " + e.message;
        landingStatus.classList.add("err");
    }
}

async function openLocalPath(path) {
    const landingStatus = document.getElementById("landing-status");
    landingStatus.className = "status";
    landingStatus.textContent = `opening ${path}…`;
    try {
        await apiSend("POST", "/api/open-path", { path });
        state.offset = 0;
        state.selectedId = null;
        await loadStatus();
        await loadEnums();
        await loadList();
    } catch (e) {
        landingStatus.textContent = "error: " + e.message;
        landingStatus.classList.add("err");
    }
}

async function scanFolder() {
    const path = document.getElementById("scan-path").value.trim();
    const scanStatus = document.getElementById("scan-status");
    const results = document.getElementById("scan-results");
    scanStatus.className = "status";
    if (!path) {
        scanStatus.textContent = "Type your FFXI install folder above first, then click Scan.";
        scanStatus.classList.add("err");
        return;
    }
    scanStatus.textContent = `scanning ${path}… (this can take a minute for big install folders)`;
    results.innerHTML = "";
    try {
        const data = await apiSend("POST", "/api/scan", { path });
        if (!data.results.length) {
            scanStatus.textContent = `no item DATs found under ${data.root}. Double-check the folder path.`;
            return;
        }
        scanStatus.textContent = `Found ${data.results.length} candidate item DAT${data.results.length === 1 ? "" : "s"}. Click one to open.`;
        for (const r of data.results) {
            const li = document.createElement("li");
            li.innerHTML = `
                <div>
                    <div class="scan-path">${escapeHtml(r.path)}</div>
                    <div class="scan-meta">
                        example item: ${escapeHtml(r.sample_name)} · type ${escapeHtml(r.sample_type)}
                    </div>
                </div>
                <div class="scan-meta">${r.size_mb} MB · ${r.records.toLocaleString()} items</div>
            `;
            li.addEventListener("click", () => openLocalPath(r.path));
            results.appendChild(li);
        }
    } catch (e) {
        scanStatus.textContent = "error: " + e.message;
        scanStatus.classList.add("err");
    }
}

document.addEventListener("DOMContentLoaded", async () => {
    // Landing / upload wiring.
    const fileInput = document.getElementById("file-input");
    const drop = document.getElementById("file-drop");
    fileInput.addEventListener("change", (e) => openFile(e.target.files[0]));
    ["dragover", "dragenter"].forEach((ev) =>
        drop.addEventListener(ev, (e) => {
            e.preventDefault();
            drop.classList.add("drag-over");
        })
    );
    ["dragleave", "drop"].forEach((ev) =>
        drop.addEventListener(ev, () => drop.classList.remove("drag-over"))
    );
    drop.addEventListener("drop", (e) => {
        e.preventDefault();
        const f = e.dataTransfer?.files?.[0];
        if (f) openFile(f);
    });

    document.getElementById("open-btn").addEventListener("click", () => fileInput.click());
    document.getElementById("download-btn").addEventListener("click", () => {
        window.location.href = "/api/download";
    });

    // Scan-folder wiring on the landing page.
    const scanPath = document.getElementById("scan-path");
    // On Windows, pre-fill with the standard install path so the user
    // usually just has to click Scan. Empty on other OSes so they type.
    const isWindows = /win/i.test(navigator.platform) || /windows/i.test(navigator.userAgent);
    if (isWindows && !scanPath.value) {
        scanPath.value = "C:\\Program Files (x86)\\PlayOnline\\SquareEnix\\FINAL FANTASY XI";
    }
    document.getElementById("scan-btn").addEventListener("click", scanFolder);
    scanPath.addEventListener("keydown", (e) => {
        if (e.key === "Enter") scanFolder();
    });

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

    // If a DAT was preloaded via --path, jump straight to the editor.
    const s = await loadStatus();
    if (s.loaded) {
        await loadEnums();
        await loadList();
    }
});
