(function () {
    "use strict";

    // ── Constants ────────────────────────────────────────────────────────────
    const STEP_NUMERIC_FIELDS = ["delta_time"];
    const ACTOR_NUMERIC_PARAM_FIELDS = ["target_temp_c", "pressure_mbar_a", "rpm"];
    const ACTOR_PARAM_FIELDS = ["status_on", ...ACTOR_NUMERIC_PARAM_FIELDS];
    const PRIORITY_MIN = 1;
    const PRIORITY_MAX = 10;
    const DEFAULT_PROFILE_BY_SYMBOL = {
        motor: "motor_rpm",
        hc_system: "hc_system_temperature",
        pump: "pump_rpm",
    };
    const TARGET_FIELD_LABELS = {
        target_temp_c: "Temp",
        pressure_mbar_a: "Pressure",
        rpm: "RPM",
    };
    const ACTOR_PARAM_FULL_LABELS = {
        target_temp_c: "Temperature",
        pressure_mbar_a: "Pressure",
        rpm: "Speed",
    };
    const ACTOR_PARAM_UNITS = {
        target_temp_c: "°C",
        pressure_mbar_a: "mbar(a)",
        rpm: "rpm",
    };
    const STATUS_OPTIONS = [
        { value: "", label: "No change" },
        { value: "on", label: "ON" },
        { value: "off", label: "OFF" },
    ];

    // ── Utilities ────────────────────────────────────────────────────────────
    function parseJsonScript(id, fallback) {
        const el = document.getElementById(id);
        if (!el) { return fallback; }
        try { return JSON.parse(el.textContent); } catch (_e) { return fallback; }
    }

    function asString(value, fallback = "") {
        if (value == null) { return fallback; }
        const s = String(value).trim();
        return s || fallback;
    }

    function parseId(value) {
        if (value == null || value === "") { return null; }
        const n = Number.parseInt(String(value), 10);
        return Number.isFinite(n) ? n : null;
    }

    function parseOptionalNumber(value) {
        if (value == null || value === "") { return null; }
        const n = Number.parseFloat(String(value));
        return Number.isFinite(n) ? n : null;
    }

    function priorityFrom(value, fallback = null) {
        if (value == null || value === "") { return fallback; }
        const s = String(value).trim();
        if (!/^(?:[1-9]|10)$/.test(s)) { return fallback; }
        const n = Number.parseInt(s, 10);
        return n >= PRIORITY_MIN && n <= PRIORITY_MAX ? n : fallback;
    }

    function priorityInputValid(value) {
        return /^(?:[1-9]|10)$/.test(String(value).trim());
    }

    function escapeHtml(value) {
        return String(value ?? "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
    }

    function escapeSelectorValue(value) {
        if (window.CSS && typeof window.CSS.escape === "function") {
            return window.CSS.escape(String(value));
        }
        return String(value).replace(/["\\]/g, "\\$&");
    }

    async function fetchJson(url, options = {}) {
        const response = await fetch(url, options);
        const text = await response.text();
        let payload = {};
        if (text) {
            try { payload = JSON.parse(text); } catch (_e) { payload = {}; }
        }
        if (!response.ok) {
            throw new Error(payload.error || `Request failed (HTTP ${response.status}).`);
        }
        return payload;
    }

    // ── Data model helpers ───────────────────────────────────────────────────
    function emptyStep() {
        return { actors: [], task: "", delta_time: null };
    }

    function emptyActorParams() {
        return { status_on: null, target_temp_c: null, pressure_mbar_a: null, rpm: null };
    }

    function actorIdForRef(ref) {
        return asString(ref?.actor_id || ref?.actor);
    }

    function actorTypeForProfile(profileId, symbolId, fallback = "") {
        const profile = asString(profileId).toLowerCase();
        const symbol = asString(symbolId).toLowerCase();
        if (profile === "hc_system_temperature" || symbol === "hc_system") { return "H/C System"; }
        if (profile === "motor_rpm" || symbol === "motor") { return "Motor"; }
        if (profile === "pump_rpm" || symbol === "pump") { return "Pump"; }
        if (profile.includes("pressure") || profile.includes("vacuum") || symbol.includes("pressure") || symbol.includes("vacuum")) { return "Pressure"; }
        return fallback || "Actor";
    }

    function parseOptionalStatus(value) {
        if (value === true || value === false) { return value; }
        return null;
    }

    function normalizeActorParams(rawParams) {
        const params = emptyActorParams();
        const src = rawParams && typeof rawParams === "object" ? rawParams : {};
        params.status_on = parseOptionalStatus(src.status_on);
        for (const f of ACTOR_NUMERIC_PARAM_FIELDS) {
            params[f] = parseOptionalNumber(src[f]);
        }
        return params;
    }

    function nextAvailablePriority(refs) {
        const used = new Set(refs.map((r) => priorityFrom(r.priority)).filter(Boolean));
        for (let p = PRIORITY_MIN; p <= PRIORITY_MAX; p++) {
            if (!used.has(p)) { return p; }
        }
        return PRIORITY_MAX;
    }

    function normalizeActorRefs(rawActors) {
        const rawRefs = Array.isArray(rawActors) ? rawActors : [];
        const refs = [];
        const seen = new Set();

        rawRefs.forEach((rawRef, index) => {
            const obj = rawRef && typeof rawRef === "object" ? rawRef : {};
            const actorId = asString(obj.actor_id || obj.actor);
            const key = actorId.toLowerCase();
            if (!actorId || seen.has(key)) { return; }
            seen.add(key);

            const option = actorOption(actorId);
            const fallbackPriority = Math.min(index + 1, PRIORITY_MAX);
            const priority = priorityFrom(obj.priority, fallbackPriority);
            refs.push({
                actor: actorId,
                actor_id: actorId,
                actor_type: asString(obj.actor_type, option?.actor_type || actorTypeForProfile(option?.profile_id, option?.symbol_id)),
                priority,
                params: normalizeActorParams(obj.params),
            });
        });

        return refs;
    }

    function normalizeLoadedStep(rawStep) {
        const payload = rawStep && typeof rawStep === "object" ? rawStep : {};
        const step = emptyStep();
        step.actors = normalizeActorRefs(payload.actors);
        step.task = asString(payload.task);
        const rawDelta = payload.delta_time ?? payload.delta_min;
        step.delta_time = parseOptionalNumber(rawDelta);
        return step;
    }

    function isActorNode(node) {
        if (!node || typeof node !== "object") { return false; }
        if (asString(node.category).toLowerCase() === "actuators") { return true; }
        const control = node.control;
        return Boolean(control && typeof control === "object" && asString(control.profile_id));
    }

    function actorOptionsForBuild(buildData) {
        const definition = buildData?.definition_json && typeof buildData.definition_json === "object"
            ? buildData.definition_json : {};
        const rawNodes = Array.isArray(definition.nodes) ? definition.nodes : [];
        const seenIds = new Set();
        const options = [];

        for (const rawNode of rawNodes) {
            if (!isActorNode(rawNode)) { continue; }
            const instanceId = asString(rawNode.instance_id);
            if (!instanceId) { continue; }
            const lookupKey = instanceId.toLowerCase();
            if (seenIds.has(lookupKey)) { continue; }
            seenIds.add(lookupKey);

            const symbolId = asString(rawNode.symbol_id);
            const labelText = asString(rawNode.label, symbolId || "Actor");
            const control = rawNode.control && typeof rawNode.control === "object" ? rawNode.control : {};
            const profileId = asString(control.profile_id, DEFAULT_PROFILE_BY_SYMBOL[symbolId] || "");
            const actorType = actorTypeForProfile(profileId, symbolId, labelText);
            options.push({
                value: instanceId,
                label: labelText && labelText !== instanceId ? `${instanceId} | ${labelText}` : instanceId,
                actor_type: actorType,
                profile_id: profileId,
                symbol_id: symbolId,
            });
        }

        options.sort((a, b) => a.value.localeCompare(b.value, undefined, { sensitivity: "base" }));
        return options;
    }

    // ── App state ────────────────────────────────────────────────────────────
    const metaData = parseJsonScript("recipe-meta", {});
    const currentRecipeData = parseJsonScript("recipe-current-data", null);

    const state = {
        recipeId: parseId(metaData.selectedRecipeId),
        reactorBuildId: parseId(currentRecipeData?.reactor_build_id),
        actorOptions: [],
        steps: [],
        activeStepIndex: null,
        dirty: false,
        loadingBuild: false,
    };

    const dom = {
        recipeSelect: document.getElementById("recipe-select"),
        buildSelect: document.getElementById("recipe-build-select"),
        newButton: document.getElementById("recipe-new-btn"),
        saveButton: document.getElementById("recipe-save-btn"),
        titleInput: document.getElementById("recipe-title"),
        operatorInput: document.getElementById("recipe-operator"),
        statusBadge: document.getElementById("recipe-status-badge"),
        saveStateBadge: document.getElementById("recipe-save-state"),
        stepCount: document.getElementById("recipe-step-count"),
        workflowContainer: document.getElementById("recipe-workflow"),
        statusMessage: document.getElementById("recipe-status-msg"),
        flowHint: document.getElementById("recipe-no-flowsheet-hint"),
    };

    // ── Actor lookups ────────────────────────────────────────────────────────
    function actorLookup() {
        return new Map(state.actorOptions.map((o) => [o.value.toLowerCase(), o]));
    }

    function isKnownActor(value) {
        const n = asString(value).toLowerCase();
        return Boolean(n) && actorLookup().has(n);
    }

    function actorRefsForStep(step) {
        const normalized = normalizeActorRefs(step?.actors);
        if (step && step.actors !== normalized) { step.actors = normalized; }
        return normalized;
    }

    function sortedActorRefsForStep(step) {
        return actorRefsForStep(step)
            .map((ref, index) => ({ ref, index }))
            .sort((a, b) => {
                const pa = priorityFrom(a.ref.priority, PRIORITY_MAX);
                const pb = priorityFrom(b.ref.priority, PRIORITY_MAX);
                return pa !== pb ? pa - pb : a.index - b.index;
            })
            .map((item) => item.ref);
    }

    function actorOption(value) {
        const n = asString(value).toLowerCase();
        return n ? actorLookup().get(n) || null : null;
    }

    function targetFieldsForActor(actorValue) {
        const option = actorOption(actorValue);
        const profileId = asString(option?.profile_id).toLowerCase();
        const symbolId = asString(option?.symbol_id).toLowerCase();
        if (profileId === "hc_system_temperature") { return ["target_temp_c"]; }
        if (profileId === "motor_rpm" || profileId === "pump_rpm") { return ["rpm"]; }
        if (profileId.includes("pressure") || profileId.includes("vacuum") || symbolId.includes("pressure") || symbolId.includes("vacuum")) { return ["pressure_mbar_a"]; }
        if (profileId.includes("pump") || symbolId.includes("pump")) { return ["rpm"]; }
        return ACTOR_NUMERIC_PARAM_FIELDS;
    }

    function statusSupportedForActor(actorValue) {
        const option = actorOption(actorValue);
        const profileId = asString(option?.profile_id).toLowerCase();
        const symbolId = asString(option?.symbol_id).toLowerCase();
        return profileId === "hc_system_temperature" || profileId === "motor_rpm" || symbolId === "hc_system" || symbolId === "motor";
    }

    function actorFieldHint(actorValue) {
        const fields = targetFieldsForActor(actorValue);
        return fields.map((f) => TARGET_FIELD_LABELS[f] || f).join(", ");
    }

    function stepActorsValid(step) {
        const refs = actorRefsForStep(step);
        return refs.length > 0 && refs.every((ref) => isKnownActor(actorIdForRef(ref)));
    }

    function duplicatePriorities(step) {
        const counts = new Map();
        for (const ref of actorRefsForStep(step)) {
            const p = priorityFrom(ref.priority);
            if (!p) { continue; }
            counts.set(p, (counts.get(p) || 0) + 1);
        }
        return [...counts.entries()].filter(([, v]) => v > 1).map(([k]) => k).sort((a, b) => a - b);
    }

    // ── Status / badge helpers ───────────────────────────────────────────────
    function updateStepCount() {
        if (dom.stepCount) { dom.stepCount.textContent = String(state.steps.length); }
    }

    function setStatus(message, tone = "muted") {
        if (!dom.statusMessage) { return; }
        dom.statusMessage.textContent = message || "";
        dom.statusMessage.className = tone === "error" ? "error-text" : "muted";
    }

    function setSaveState(label, badgeClass) {
        if (!dom.saveStateBadge) { return; }
        dom.saveStateBadge.textContent = label;
        dom.saveStateBadge.className = `badge ${badgeClass}`;
    }

    function markUnsaved() {
        state.dirty = true;
        setSaveState("Unsaved", "badge-warning");
    }

    function markSaved() {
        state.dirty = false;
        setSaveState("Saved", "badge-muted");
    }

    function updateStatusBadge(status) {
        if (!dom.statusBadge) { return; }
        const s = asString(status, "draft");
        let cls = "badge-muted";
        if (s === "approved") { cls = "badge-success"; }
        else if (s === "archived") { cls = "badge-warning"; }
        dom.statusBadge.textContent = s;
        dom.statusBadge.className = `badge ${cls}`;
    }

    function syncBuildSelect() {
        if (!dom.buildSelect) { return; }
        dom.buildSelect.value = state.reactorBuildId ? String(state.reactorBuildId) : "";
    }

    function currentFlowHint() {
        if (state.loadingBuild) { return "Loading device list from the selected flowsheet."; }
        if (!state.reactorBuildId) { return "Select a flowsheet above to populate the Device list."; }
        if (state.actorOptions.length === 0) { return "The selected flowsheet does not contain any actors."; }
        return "";
    }

    function syncFlowHint() {
        if (!dom.flowHint) { return; }
        const msg = currentFlowHint();
        dom.flowHint.textContent = msg;
        dom.flowHint.classList.toggle("is-hidden", !msg);
    }

    function invalidActorCount() {
        if (!state.reactorBuildId) { return 0; }
        return state.steps.filter((s) => !stepActorsValid(s)).length;
    }

    function canEditSteps() {
        return Boolean(state.reactorBuildId) && state.actorOptions.length > 0 && !state.loadingBuild;
    }

    // ── Step management ──────────────────────────────────────────────────────
    function addStepAfter(afterIndex) {
        if (!canEditSteps()) { return null; }
        const newStep = emptyStep();
        const prevStep = state.steps[afterIndex] ?? null;
        if (prevStep) { newStep.delta_time = prevStep.delta_time; }
        const insertAt = afterIndex + 1;
        state.steps.splice(insertAt, 0, newStep);
        if (state.activeStepIndex !== null && state.activeStepIndex >= insertAt) {
            state.activeStepIndex += 1;
        }
        state.activeStepIndex = insertAt;
        markUnsaved();
        return insertAt;
    }

    function addFirstStep() {
        if (!canEditSteps()) { return null; }
        state.steps.push(emptyStep());
        state.activeStepIndex = 0;
        markUnsaved();
        return 0;
    }

    // ── Input builders ───────────────────────────────────────────────────────
    function makeTextInput(value, fieldName, rowIndex, isEmpty, placeholder, disabled) {
        const v = asString(value);
        return `<input type="text" maxlength="255" data-field="${fieldName}" data-row="${rowIndex}" class="recipe-text-input${isEmpty ? " recipe-input-empty" : ""}" value="${escapeHtml(v)}"${placeholder ? ` placeholder="${escapeHtml(placeholder)}"` : ""}${disabled ? " disabled" : ""}>`;
    }

    function makeStepNumericInput(value, fieldName, rowIndex, isEmpty, disabled) {
        const v = value == null ? "" : String(value);
        return `<input type="number" step="0.01" min="0" data-field="${fieldName}" data-row="${rowIndex}" class="recipe-num-input${isEmpty ? " recipe-input-empty" : ""}" value="${escapeHtml(v)}"${isEmpty ? ' placeholder="—"' : ""}${disabled ? " disabled" : ""}>`;
    }

    function makePriorityInput(ref, rowIndex, disabled) {
        const actorId = actorIdForRef(ref);
        const step = state.steps[rowIndex];
        const value = priorityFrom(ref.priority, step ? nextAvailablePriority(actorRefsForStep(step)) : 1) || PRIORITY_MAX;
        return `<input type="number" inputmode="numeric" min="${PRIORITY_MIN}" max="${PRIORITY_MAX}" step="1" data-field="priority" data-row="${rowIndex}" data-actor="${escapeHtml(actorId)}" class="recipe-priority-input" value="${escapeHtml(value)}"${disabled ? " disabled" : ""}>`;
    }

    function statusSelectValue(ref) {
        const v = ref.params?.status_on;
        if (v === true) { return "on"; }
        if (v === false) { return "off"; }
        return "";
    }

    function makeActorStatusSelect(ref, rowIndex, disabled) {
        const actorId = actorIdForRef(ref);
        const supported = statusSupportedForActor(actorId);
        const value = supported ? statusSelectValue(ref) : "";
        const selectDisabled = disabled || !supported;
        const title = supported ? "Set device status for this step." : "This device has no ON/OFF command in recipes.";
        const options = STATUS_OPTIONS.map((o) => `<option value="${escapeHtml(o.value)}"${o.value === value ? " selected" : ""}>${escapeHtml(o.label)}</option>`).join("");
        return `<select data-param="status_on" data-row="${rowIndex}" data-actor="${escapeHtml(actorId)}" class="recipe-status-select" title="${escapeHtml(title)}"${selectDisabled ? " disabled" : ""}>${options}</select>`;
    }

    function makeActorParamInput(ref, paramField, rowIndex, disabled) {
        const actorId = actorIdForRef(ref);
        const activeFields = new Set(targetFieldsForActor(actorId));
        const fieldActive = activeFields.has(paramField);
        const value = ref.params?.[paramField];
        const v = value == null ? "" : String(value);
        const minAttr = paramField === "target_temp_c" ? ' min="-40"' : ' min="0"';
        const maxAttr = paramField === "rpm" ? ' max="2000"' : "";
        const statusOff = ref.params?.status_on === false;
        const targetDisabled = disabled || !fieldActive || statusOff;
        const isEmpty = v === "";
        let title = "";
        if (!fieldActive) { title = `${TARGET_FIELD_LABELS[paramField] || paramField} is not used by this device.`; }
        else if (statusOff) { title = "Status OFF sends no setpoint for this device."; }
        const fieldTitle = title ? ` title="${escapeHtml(title)}"` : "";
        return `<input type="number" step="0.01"${minAttr}${maxAttr} data-param="${paramField}" data-row="${rowIndex}" data-actor="${escapeHtml(actorId)}" class="recipe-num-input${fieldActive ? "" : " recipe-num-input-inactive"}${isEmpty && fieldActive && !statusOff ? " recipe-input-empty" : ""}" value="${escapeHtml(v)}"${isEmpty && fieldActive && !statusOff ? ' placeholder="—"' : ""}${targetDisabled ? " disabled" : ""}${fieldTitle}>`;
    }

    function makeActorPicker(step, rowIndex, isEmpty, disabled) {
        const refs = actorRefsForStep(step);
        const selectedKeys = new Set(refs.map((ref) => actorIdForRef(ref).toLowerCase()));
        let placeholder = "Add device…";
        if (!state.reactorBuildId) { placeholder = "Select flowsheet first"; }
        else if (state.actorOptions.length === 0) { placeholder = "No devices available"; }

        let html = `<div class="recipe-actor-picker">`;
        html += `<select data-field="actor" data-row="${rowIndex}" class="recipe-actor-select"${disabled ? " disabled" : ""}>`;
        html += `<option value="">${escapeHtml(placeholder)}</option>`;
        for (const option of state.actorOptions) {
            if (selectedKeys.has(option.value.toLowerCase())) { continue; }
            html += `<option value="${escapeHtml(option.value)}">${escapeHtml(option.label)} — ${escapeHtml(actorFieldHint(option.value))}</option>`;
        }
        html += "</select></div>";
        return html;
    }

    function duplicatePriorityWarningHtml(step) {
        const dupes = duplicatePriorities(step);
        if (!dupes.length) { return ""; }
        return `<div class="recipe-priority-warning">Duplicate priority ${escapeHtml(dupes.join(", "))}. Equal priorities run in table order.</div>`;
    }

    // ── Step card rendering ──────────────────────────────────────────────────
    function stepSummaryHtml(step) {
        const refs = sortedActorRefsForStep(step);
        if (!refs.length) { return `<span class="recipe-step-no-devices muted">No devices added</span>`; }
        return refs.map((ref) => {
            const actorId = actorIdForRef(ref);
            const option = actorOption(actorId);
            const label = (option?.label || actorId).split(" | ")[0];
            const activeFields = targetFieldsForActor(actorId);
            const primaryField = activeFields[0] || null;
            const value = primaryField ? ref.params?.[primaryField] : null;
            const unit = primaryField ? (ACTOR_PARAM_UNITS[primaryField] || "") : "";
            const statusVal = ref.params?.status_on;

            let valueStr = "—";
            if (statusVal === false) { valueStr = "OFF"; }
            else if (value != null) { valueStr = `${value} ${unit}`; }
            else if (statusVal === true) { valueStr = "ON"; }

            return `<span class="recipe-summary-chip"><span class="recipe-summary-device">${escapeHtml(label)}</span><span class="recipe-summary-sep">·</span><span class="recipe-summary-value">${escapeHtml(valueStr)}</span></span>`;
        }).join("");
    }

    function renderDeviceTableRow(ref, stepIndex, disabled) {
        const actorId = actorIdForRef(ref);
        const option = actorOption(actorId);
        const activeFields = targetFieldsForActor(actorId);
        const hasStatus = statusSupportedForActor(actorId);
        const fullLabel = option?.label || `${actorId} (not in flowsheet)`;
        const shortLabel = fullLabel.split(" | ")[0];
        const isUnknown = !option;

        const primaryField = activeFields[0] || null;
        const paramLabel = primaryField ? (ACTOR_PARAM_FULL_LABELS[primaryField] || primaryField) : "—";
        const unit = primaryField ? (ACTOR_PARAM_UNITS[primaryField] || "—") : "—";

        const statusHtml = hasStatus
            ? makeActorStatusSelect(ref, stepIndex, disabled)
            : `<span class="recipe-device-na">—</span>`;

        const setpointHtml = primaryField
            ? makeActorParamInput(ref, primaryField, stepIndex, disabled)
            : `<span class="recipe-device-na">—</span>`;

        return `
            <tr class="recipe-device-row${isUnknown ? " is-invalid" : ""}">
                <td class="recipe-device-col-name">
                    <span class="recipe-device-name${isUnknown ? " is-invalid" : ""}" title="${escapeHtml(fullLabel)}">${escapeHtml(shortLabel)}</span>
                    ${isUnknown ? `<span class="recipe-device-badge-error">not in flowsheet</span>` : ""}
                </td>
                <td class="recipe-device-col-param">${escapeHtml(paramLabel)}</td>
                <td class="recipe-device-col-action">${statusHtml}</td>
                <td class="recipe-device-col-setpoint">${setpointHtml}</td>
                <td class="recipe-device-col-unit"><span class="recipe-unit-badge">${escapeHtml(unit)}</span></td>
                <td class="recipe-device-col-order">${makePriorityInput(ref, stepIndex, disabled)}</td>
                <td class="recipe-device-col-remove">
                    <button type="button" class="recipe-actor-remove" data-row="${stepIndex}" data-actor="${escapeHtml(actorId)}" title="Remove device"${disabled ? " disabled" : ""}>&#xd7;</button>
                </td>
            </tr>`;
    }

    function renderStepCard(step, index, isActive, disabled) {
        const refs = actorRefsForStep(step);
        const hasErrors = Boolean(state.reactorBuildId) && refs.some((ref) => !isKnownActor(actorIdForRef(ref)));
        const isMissingActor = Boolean(state.reactorBuildId) && refs.length === 0;
        const isValid = refs.length > 0 && !hasErrors;

        let cardClass = "recipe-step-card";
        if (isActive) { cardClass += " is-active"; }
        if (hasErrors || isMissingActor) { cardClass += " is-error"; }
        else if (isValid) { cardClass += " is-valid"; }

        let html = `<div class="${cardClass}" data-row="${index}">`;

        // ── Header (always shown) ──
        html += `<div class="recipe-step-header" data-activate="${index}" role="button" tabindex="0" aria-expanded="${isActive}">`;
        html += `<span class="recipe-step-num-badge">${index + 1}</span>`;
        html += `<div class="recipe-step-header-content">`;
        html += `<span class="recipe-step-task-label">${escapeHtml(step.task || "Untitled step")}</span>`;
        if (!isActive) {
            html += `<div class="recipe-step-header-row2">`;
            if (step.delta_time != null) {
                html += `<span class="recipe-step-duration-badge">${step.delta_time} min</span>`;
            }
            html += `<div class="recipe-step-summary">${stepSummaryHtml(step)}</div>`;
            html += `</div>`;
        }
        html += `</div>`;
        html += `<div class="recipe-step-header-right">`;
        if (isActive && step.delta_time != null) {
            html += `<span class="recipe-step-duration-badge">${step.delta_time} min</span>`;
        }
        html += `<button class="btn recipe-del-btn" data-del="${index}" type="button" title="Delete step"${disabled ? " disabled" : ""}>&#xd7;</button>`;
        html += `</div>`;
        html += `</div>`; // end header

        // ── Body (only when active) ──
        if (isActive) {
            const sortedRefs = sortedActorRefsForStep(step);
            const actorRequired = Boolean(state.reactorBuildId) && !stepActorsValid(step);

            html += `<div class="recipe-step-body">`;

            // Step meta fields
            html += `<div class="recipe-step-fields-row">`;
            html += `<label class="recipe-step-field">`;
            html += `<span class="recipe-step-field-label">Task / Description</span>`;
            html += makeTextInput(step.task, "task", index, !step.task, "e.g. Ramp up temperature…", disabled);
            html += `</label>`;
            html += `<label class="recipe-step-field recipe-step-field-duration">`;
            html += `<span class="recipe-step-field-label">Duration</span>`;
            html += `<span class="recipe-duration-input-wrap">`;
            html += makeStepNumericInput(step.delta_time, "delta_time", index, step.delta_time == null, disabled);
            html += `<span class="recipe-unit-suffix">min</span>`;
            html += `</span>`;
            html += `</label>`;
            html += `</div>`;

            // Device table
            html += `<div class="recipe-device-section${actorRequired ? " recipe-device-section-required" : ""}">`;
            if (actorRequired) {
                html += `<div class="recipe-device-required-hint">At least one device from the selected flowsheet is required for this step.</div>`;
            }
            html += `<table class="recipe-device-table">`;
            html += `<thead><tr>`;
            html += `<th class="recipe-device-col-name">Device</th>`;
            html += `<th class="recipe-device-col-param">Parameter</th>`;
            html += `<th class="recipe-device-col-action">Action</th>`;
            html += `<th class="recipe-device-col-setpoint">Setpoint</th>`;
            html += `<th class="recipe-device-col-unit">Unit</th>`;
            html += `<th class="recipe-device-col-order">Order</th>`;
            html += `<th class="recipe-device-col-remove"></th>`;
            html += `</tr></thead>`;
            html += `<tbody>`;
            for (const ref of sortedRefs) {
                html += renderDeviceTableRow(ref, index, disabled);
            }
            if (!sortedRefs.length) {
                html += `<tr class="recipe-device-empty-row"><td colspan="7" class="recipe-device-empty-cell">No devices added yet — use the selector below.</td></tr>`;
            }
            html += `</tbody></table>`;
            html += makeActorPicker(step, index, !sortedRefs.length, disabled);
            html += duplicatePriorityWarningHtml(step);
            html += `</div>`; // end device-section

            html += `</div>`; // end step-body
        }

        html += `</div>`; // end step-card
        return html;
    }

    function renderWorkflow() {
        if (!dom.workflowContainer) { return; }

        const controlsDisabled = !canEditSteps();

        // Clamp activeStepIndex
        if (state.activeStepIndex !== null && state.activeStepIndex >= state.steps.length) {
            state.activeStepIndex = state.steps.length > 0 ? state.steps.length - 1 : null;
        }
        if (state.activeStepIndex === null && state.steps.length > 0) {
            state.activeStepIndex = 0;
        }

        let html = "";

        if (state.steps.length === 0) {
            html += `<div class="recipe-workflow-empty"><p class="muted">${controlsDisabled ? "Select a flowsheet first, then add steps." : "No steps yet."}</p></div>`;
            html += `<div class="recipe-step-add-row"><button type="button" class="btn btn-primary recipe-add-first-btn" data-after="-1"${controlsDisabled ? " disabled" : ""}>+ Add First Step</button></div>`;
        } else {
            for (let i = 0; i < state.steps.length; i++) {
                html += renderStepCard(state.steps[i], i, i === state.activeStepIndex, controlsDisabled);
                html += `<div class="recipe-step-gap"><button type="button" class="recipe-add-step-between" data-after="${i}" title="Add step after ${i + 1}"${controlsDisabled ? " disabled" : ""}>+ Add Step</button></div>`;
            }
        }

        dom.workflowContainer.innerHTML = html;
        updateStepCount();
        syncFlowHint();
        attachWorkflowListeners();
    }

    function attachWorkflowListeners() {
        if (!dom.workflowContainer) { return; }

        for (const el of dom.workflowContainer.querySelectorAll("[data-activate]")) {
            el.addEventListener("click", onStepHeaderClick);
            el.addEventListener("keydown", (e) => {
                if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onStepHeaderClick(e); }
            });
        }

        for (const btn of dom.workflowContainer.querySelectorAll("[data-del]")) {
            btn.addEventListener("click", onDeleteRow);
        }

        for (const btn of dom.workflowContainer.querySelectorAll("[data-after]")) {
            btn.addEventListener("click", onAddStepClick);
        }

        for (const input of dom.workflowContainer.querySelectorAll('input[data-field="task"]')) {
            input.addEventListener("input", onTaskInput);
        }

        for (const input of dom.workflowContainer.querySelectorAll('input[data-field="delta_time"]')) {
            input.addEventListener("input", onStepNumericInput);
        }

        for (const input of dom.workflowContainer.querySelectorAll("input[data-param]")) {
            input.addEventListener("input", onActorParamInput);
        }

        for (const select of dom.workflowContainer.querySelectorAll('select[data-param="status_on"]')) {
            select.addEventListener("change", onActorStatusChange);
        }

        for (const input of dom.workflowContainer.querySelectorAll('input[data-field="priority"]')) {
            input.addEventListener("input", onPriorityInput);
            input.addEventListener("change", onPriorityChange);
        }

        for (const select of dom.workflowContainer.querySelectorAll('select[data-field="actor"]')) {
            select.addEventListener("change", onActorChange);
        }

        for (const btn of dom.workflowContainer.querySelectorAll(".recipe-actor-remove")) {
            btn.addEventListener("click", onActorRemove);
        }
    }

    // ── Event handlers ────────────────────────────────────────────────────────
    function onStepHeaderClick(event) {
        if (event.target.closest("[data-del]")) { return; }
        const index = parseId(event.currentTarget.getAttribute("data-activate"));
        if (index == null) { return; }
        if (index !== state.activeStepIndex) {
            state.activeStepIndex = index;
            renderWorkflow();
            const card = dom.workflowContainer?.querySelector(`[data-row="${index}"]`);
            if (card) { card.scrollIntoView({ behavior: "smooth", block: "nearest" }); }
        }
    }

    function onAddStepClick(event) {
        const afterIndex = parseId(event.currentTarget.getAttribute("data-after"));
        if (afterIndex == null) { return; }
        if (!canEditSteps()) {
            setStatus(state.reactorBuildId ? "The selected flowsheet does not contain any actors." : "Select a flowsheet before adding steps.", "error");
            return;
        }
        const newIndex = afterIndex === -1 ? addFirstStep() : addStepAfter(afterIndex);
        if (newIndex == null) { return; }
        renderWorkflow();
        const card = dom.workflowContainer?.querySelector(`[data-row="${newIndex}"]`);
        if (card) {
            card.scrollIntoView({ behavior: "smooth", block: "nearest" });
            card.querySelector('input[data-field="task"]')?.focus();
        }
    }

    function onDeleteRow(event) {
        event.stopPropagation();
        const rowIndex = parseId(event.currentTarget.getAttribute("data-del"));
        if (rowIndex == null || rowIndex < 0 || rowIndex >= state.steps.length) { return; }
        state.steps.splice(rowIndex, 1);
        if (state.activeStepIndex !== null) {
            if (state.activeStepIndex === rowIndex) {
                state.activeStepIndex = rowIndex > 0 ? rowIndex - 1 : (state.steps.length > 0 ? 0 : null);
            } else if (state.activeStepIndex > rowIndex) {
                state.activeStepIndex -= 1;
            }
        }
        markUnsaved();
        renderWorkflow();
    }

    function onTaskInput(event) {
        const input = event.currentTarget;
        const rowIndex = parseId(input.getAttribute("data-row"));
        if (rowIndex == null || rowIndex >= state.steps.length) { return; }
        state.steps[rowIndex].task = input.value;
        markUnsaved();
        // Patch header label without full re-render
        const card = dom.workflowContainer?.querySelector(`.recipe-step-card[data-row="${rowIndex}"]`);
        const label = card?.querySelector(".recipe-step-task-label");
        if (label) { label.textContent = input.value || "Untitled step"; }
    }

    function onStepNumericInput(event) {
        const input = event.currentTarget;
        const rowIndex = parseId(input.getAttribute("data-row"));
        const fieldName = input.getAttribute("data-field");
        if (rowIndex == null || rowIndex >= state.steps.length || !fieldName) { return; }
        const raw = input.value.trim();
        state.steps[rowIndex][fieldName] = raw === "" ? null : Number.parseFloat(raw);
        markUnsaved();
        // Patch duration badge without full re-render
        const card = dom.workflowContainer?.querySelector(`.recipe-step-card[data-row="${rowIndex}"]`);
        const badge = card?.querySelector(".recipe-step-duration-badge");
        if (badge) { badge.textContent = state.steps[rowIndex][fieldName] != null ? `${state.steps[rowIndex][fieldName]} min` : ""; }
    }

    function actorRefForRow(rowIndex, actorId) {
        if (rowIndex == null || rowIndex >= state.steps.length || !actorId) { return null; }
        return actorRefsForStep(state.steps[rowIndex]).find(
            (ref) => actorIdForRef(ref).toLowerCase() === actorId.toLowerCase(),
        ) || null;
    }

    function onActorParamInput(event) {
        const input = event.currentTarget;
        const rowIndex = parseId(input.getAttribute("data-row"));
        const actorId = asString(input.getAttribute("data-actor"));
        const paramField = input.getAttribute("data-param");
        const ref = actorRefForRow(rowIndex, actorId);
        if (!ref || !ACTOR_PARAM_FIELDS.includes(paramField)) { return; }
        const raw = input.value.trim();
        ref.params = ref.params && typeof ref.params === "object" ? ref.params : emptyActorParams();
        ref.params[paramField] = raw === "" ? null : Number.parseFloat(raw);
        markUnsaved();
    }

    function onActorStatusChange(event) {
        const select = event.currentTarget;
        const rowIndex = parseId(select.getAttribute("data-row"));
        const actorId = asString(select.getAttribute("data-actor"));
        const ref = actorRefForRow(rowIndex, actorId);
        if (!ref) { return; }
        ref.params = ref.params && typeof ref.params === "object" ? ref.params : emptyActorParams();
        if (select.value === "on") { ref.params.status_on = true; }
        else if (select.value === "off") {
            ref.params.status_on = false;
            for (const f of ACTOR_NUMERIC_PARAM_FIELDS) { ref.params[f] = null; }
        } else { ref.params.status_on = null; }
        markUnsaved();
        renderWorkflow();
    }

    function syncPriorityInputValidity(input) {
        const valid = priorityInputValid(input.value);
        input.classList.toggle("is-invalid", !valid);
        input.setAttribute("aria-invalid", String(!valid));
        input.setCustomValidity(valid ? "" : "Priority must be an integer from 1 to 10.");
        return valid;
    }

    function onPriorityInput(event) { syncPriorityInputValidity(event.currentTarget); }

    function onPriorityChange(event) {
        const input = event.currentTarget;
        if (!syncPriorityInputValidity(input)) {
            setStatus("Priority must be an integer from 1 to 10.", "error");
            return;
        }
        const rowIndex = parseId(input.getAttribute("data-row"));
        const actorId = asString(input.getAttribute("data-actor"));
        const ref = actorRefForRow(rowIndex, actorId);
        if (!ref) { return; }
        ref.priority = priorityFrom(input.value, ref.priority);
        markUnsaved();
        renderWorkflow();
        if (duplicatePriorities(state.steps[rowIndex] || {}).length) {
            setStatus("Duplicate priorities are allowed; equal priorities run in table order.", "muted");
        }
    }

    function onActorChange(event) {
        const select = event.currentTarget;
        const rowIndex = parseId(select.getAttribute("data-row"));
        if (rowIndex == null || rowIndex >= state.steps.length) { return; }
        const actor = asString(select.value);
        if (!actor) { return; }
        const refs = actorRefsForStep(state.steps[rowIndex]);
        if (!refs.some((ref) => actorIdForRef(ref).toLowerCase() === actor.toLowerCase())) {
            const option = actorOption(actor);
            refs.push({
                actor,
                actor_id: actor,
                actor_type: option?.actor_type || actorTypeForProfile(option?.profile_id, option?.symbol_id),
                priority: nextAvailablePriority(refs),
                params: emptyActorParams(),
            });
        }
        state.steps[rowIndex].actors = refs;
        select.value = "";
        markUnsaved();
        renderWorkflow();
    }

    function onActorRemove(event) {
        event.stopPropagation();
        const btn = event.currentTarget;
        const rowIndex = parseId(btn.getAttribute("data-row"));
        const actor = asString(btn.getAttribute("data-actor"));
        if (rowIndex == null || rowIndex >= state.steps.length || !actor) { return; }
        state.steps[rowIndex].actors = actorRefsForStep(state.steps[rowIndex]).filter(
            (ref) => actorIdForRef(ref).toLowerCase() !== actor.toLowerCase(),
        );
        markUnsaved();
        renderWorkflow();
    }

    // ── Build / recipe loading ───────────────────────────────────────────────
    async function loadBuildActors(buildId, { quiet = false } = {}) {
        state.reactorBuildId = parseId(buildId);
        state.actorOptions = [];
        syncBuildSelect();
        if (!state.reactorBuildId) { return []; }
        const buildData = await fetchJson(`/api/reactor-builds/${state.reactorBuildId}`);
        state.actorOptions = actorOptionsForBuild(buildData);
        if (!quiet && state.actorOptions.length === 0) {
            setStatus("The selected flowsheet does not contain any actors.", "error");
        }
        return state.actorOptions;
    }

    async function refreshActorOptions(buildId, { quiet = false, renderPending = false } = {}) {
        state.loadingBuild = true;
        if (renderPending) { renderWorkflow(); }
        try {
            return await loadBuildActors(buildId, { quiet });
        } catch (error) {
            state.actorOptions = [];
            if (!quiet) { setStatus(error.message || "Could not load flowsheet actors.", "error"); }
            return [];
        } finally {
            state.loadingBuild = false;
            syncBuildSelect();
            renderWorkflow();
        }
    }

    function applyRecipeData(recipeData, { preserveActorOptions = false } = {}) {
        if (!recipeData) {
            state.recipeId = null;
            state.reactorBuildId = null;
            state.steps = [];
            state.activeStepIndex = null;
            if (!preserveActorOptions) { state.actorOptions = []; }
            if (dom.recipeSelect) { dom.recipeSelect.value = ""; }
            if (dom.titleInput) { dom.titleInput.value = ""; }
            if (dom.operatorInput) { dom.operatorInput.value = ""; }
            updateStatusBadge("draft");
            markSaved();
            syncBuildSelect();
            renderWorkflow();
            setStatus("");
            return;
        }

        state.recipeId = parseId(recipeData.recipe_id);
        state.reactorBuildId = parseId(recipeData.reactor_build_id);
        state.steps = Array.isArray(recipeData.steps) ? recipeData.steps.map(normalizeLoadedStep) : [];
        state.activeStepIndex = state.steps.length > 0 ? 0 : null;

        if (dom.recipeSelect) { dom.recipeSelect.value = state.recipeId ? String(state.recipeId) : ""; }
        if (dom.titleInput) { dom.titleInput.value = asString(recipeData.title); }
        if (dom.operatorInput) { dom.operatorInput.value = asString(recipeData.operator_name); }

        updateStatusBadge(asString(recipeData.status, "draft"));
        markSaved();
        syncBuildSelect();
        renderWorkflow();

        if (state.reactorBuildId && invalidActorCount() > 0) {
            setStatus("One or more device assignments no longer match the selected flowsheet.", "error");
        } else if (recipeData.updated_at) {
            setStatus(`Last saved: ${recipeData.updated_at}`);
        } else {
            setStatus("");
        }
    }

    async function loadRecipeById(recipeId) {
        setSaveState("Loading...", "badge-muted");
        setStatus("");
        const recipeData = await fetchJson(`/api/recipes/${recipeId}`);
        await refreshActorOptions(recipeData.reactor_build_id, { quiet: true });
        applyRecipeData(recipeData, { preserveActorOptions: true });
        window.history.replaceState(null, "", `/recipes?recipe_id=${recipeId}`);
    }

    // ── Payload collection ───────────────────────────────────────────────────
    function readStepNumericValue(rowIndex, fieldName) {
        const input = dom.workflowContainer?.querySelector(`input[data-row="${rowIndex}"][data-field="${fieldName}"]`);
        if (!input) { return null; }
        const raw = input.value.trim();
        if (!raw) { return null; }
        const n = Number.parseFloat(raw);
        return Number.isFinite(n) ? n : null;
    }

    function readActorParamValue(rowIndex, actorId, paramField) {
        const input = dom.workflowContainer?.querySelector(
            `input[data-row="${rowIndex}"][data-actor="${escapeSelectorValue(actorId)}"][data-param="${paramField}"]`,
        );
        if (!input || input.disabled) { return null; }
        const raw = input.value.trim();
        if (!raw) { return null; }
        const n = Number.parseFloat(raw);
        return Number.isFinite(n) ? n : null;
    }

    function readActorPriority(rowIndex, actorId, fallback) {
        const input = dom.workflowContainer?.querySelector(
            `input[data-row="${rowIndex}"][data-actor="${escapeSelectorValue(actorId)}"][data-field="priority"]`,
        );
        if (!input) { return fallback; }
        return priorityFrom(input.value, null);
    }

    function readActorStatusValue(rowIndex, actorId) {
        const select = dom.workflowContainer?.querySelector(
            `select[data-row="${rowIndex}"][data-actor="${escapeSelectorValue(actorId)}"][data-param="status_on"]`,
        );
        if (!select || select.disabled) { return null; }
        if (select.value === "on") { return true; }
        if (select.value === "off") { return false; }
        return null;
    }

    function collectPayload() {
        const steps = [];
        let hasInvalidPriority = false;

        for (let rowIndex = 0; rowIndex < state.steps.length; rowIndex++) {
            const step = emptyStep();
            const taskInput = dom.workflowContainer?.querySelector(`input[data-row="${rowIndex}"][data-field="task"]`);

            step.actors = sortedActorRefsForStep(state.steps[rowIndex]).map((ref) => {
                const actorId = actorIdForRef(ref);
                const priority = readActorPriority(rowIndex, actorId, ref.priority);
                if (!priority) { hasInvalidPriority = true; }
                const option = actorOption(actorId);
                return {
                    actor_id: actorId,
                    actor_type: asString(ref.actor_type, option?.actor_type || actorTypeForProfile(option?.profile_id, option?.symbol_id)),
                    priority: priority || ref.priority,
                    params: {
                        status_on: readActorStatusValue(rowIndex, actorId),
                        target_temp_c: readActorParamValue(rowIndex, actorId, "target_temp_c"),
                        pressure_mbar_a: readActorParamValue(rowIndex, actorId, "pressure_mbar_a"),
                        rpm: readActorParamValue(rowIndex, actorId, "rpm"),
                    },
                };
            });

            step.task = taskInput ? taskInput.value : asString(state.steps[rowIndex].task);
            for (const fieldName of STEP_NUMERIC_FIELDS) {
                step[fieldName] = readStepNumericValue(rowIndex, fieldName);
            }
            step.delta_min = step.delta_time;

            const isEmpty = step.actors.length === 0 && !step.task && STEP_NUMERIC_FIELDS.every((f) => step[f] == null);
            if (!isEmpty) { steps.push(step); }
        }

        const operatorName = dom.operatorInput ? dom.operatorInput.value.trim() : "";
        return {
            title: dom.titleInput ? dom.titleInput.value.trim() : "",
            operator_name: operatorName,
            created_by: operatorName || "unknown",
            updated_by: operatorName || "unknown",
            reactor_build_id: state.reactorBuildId,
            steps,
            has_invalid_priority: hasInvalidPriority,
        };
    }

    async function saveRecipe() {
        const payload = collectPayload();

        if (!payload.title) {
            setStatus("Recipe Title is required before saving.", "error");
            dom.titleInput?.focus();
            return;
        }
        if (!payload.operator_name) {
            setStatus("Operator is required before saving.", "error");
            dom.operatorInput?.focus();
            return;
        }
        if (!payload.reactor_build_id) {
            setStatus("Select a flowsheet before saving the recipe.", "error");
            dom.buildSelect?.focus();
            return;
        }
        if (state.actorOptions.length === 0) {
            setStatus("The selected flowsheet does not contain any actors.", "error");
            return;
        }
        if (payload.has_invalid_priority) {
            delete payload.has_invalid_priority;
            setStatus("Priority must be an integer from 1 to 10.", "error");
            return;
        }
        delete payload.has_invalid_priority;

        const invalidStepIndex = payload.steps.findIndex(
            (step) => !step.actors.length || step.actors.some((ref) => !isKnownActor(ref.actor_id || ref.actor)),
        );
        if (invalidStepIndex !== -1) {
            state.activeStepIndex = invalidStepIndex;
            setStatus("At least one actor from the selected flowsheet is required for every step before saving.", "error");
            renderWorkflow();
            dom.workflowContainer?.querySelector(`[data-row="${invalidStepIndex}"]`)?.scrollIntoView({ behavior: "smooth", block: "nearest" });
            return;
        }

        const isCreate = !state.recipeId;
        const requestUrl = isCreate ? "/api/recipes" : `/api/recipes/${state.recipeId}`;
        const requestMethod = isCreate ? "POST" : "PATCH";
        if (!isCreate) { delete payload.created_by; }

        const headers = { "Content-Type": "application/json" };
        if (metaData.apiAuthRequired && metaData.recipeWriteToken) {
            headers["X-Recipe-Token"] = metaData.recipeWriteToken;
        }

        setSaveState("Saving...", "badge-warning");
        setStatus("");

        try {
            const savedRecipe = await fetchJson(requestUrl, { method: requestMethod, headers, body: JSON.stringify(payload) });
            state.recipeId = parseId(savedRecipe.recipe_id);
            state.steps = Array.isArray(savedRecipe.steps) ? savedRecipe.steps.map(normalizeLoadedStep) : [];
            updateStatusBadge(asString(savedRecipe.status, "draft"));
            markSaved();
            updateSelectorOption(savedRecipe);
            renderWorkflow();
            setStatus(`Saved at ${savedRecipe.updated_at || new Date().toISOString()}`);
            window.history.replaceState(null, "", `/recipes?recipe_id=${savedRecipe.recipe_id}`);
        } catch (error) {
            setSaveState("Error", "badge-danger");
            setStatus(error.message || "Recipe save failed.", "error");
        }
    }

    function updateSelectorOption(savedRecipe) {
        if (!dom.recipeSelect) { return; }
        const value = String(savedRecipe.recipe_id);
        const label = `${savedRecipe.title} | ${savedRecipe.status || "draft"} | ${savedRecipe.updated_by || savedRecipe.created_by || ""}`;
        let option = dom.recipeSelect.querySelector(`option[value="${value}"]`);
        if (!option) {
            option = document.createElement("option");
            option.value = value;
            dom.recipeSelect.appendChild(option);
        }
        option.textContent = label;
        dom.recipeSelect.value = value;
    }

    function confirmDiscardDirtyChanges() {
        if (!state.dirty) { return true; }
        return window.confirm("Discard unsaved changes?");
    }

    function revertRecipeSelection() {
        if (!dom.recipeSelect) { return; }
        dom.recipeSelect.value = state.recipeId ? String(state.recipeId) : "";
    }

    async function onRecipeSelectChange() {
        const selectedRecipeId = parseId(dom.recipeSelect?.value);
        if (!selectedRecipeId) {
            if (!confirmDiscardDirtyChanges()) { revertRecipeSelection(); return; }
            await refreshActorOptions(null, { quiet: true });
            applyRecipeData(null, { preserveActorOptions: true });
            window.history.replaceState(null, "", "/recipes");
            return;
        }
        if (!confirmDiscardDirtyChanges()) { revertRecipeSelection(); return; }
        try {
            await loadRecipeById(selectedRecipeId);
        } catch (error) {
            setSaveState("Error", "badge-danger");
            setStatus(error.message || "Could not load the selected recipe.", "error");
            revertRecipeSelection();
        }
    }

    function onHeaderInput() { markUnsaved(); }

    async function onBuildSelectChange() {
        const nextBuildId = parseId(dom.buildSelect?.value);
        await refreshActorOptions(nextBuildId, { renderPending: true });
        markUnsaved();
        if (!state.reactorBuildId) { setStatus("Select a flowsheet before adding steps.", "error"); return; }
        if (state.actorOptions.length === 0) { setStatus("The selected flowsheet does not contain any actors.", "error"); return; }
        if (invalidActorCount() > 0) { setStatus("Check device assignments after changing the flowsheet.", "error"); return; }
        setStatus("Device list updated from the selected flowsheet.");
    }

    async function initializePage() {
        const initialBuildId = parseId(currentRecipeData?.reactor_build_id);
        if (initialBuildId) {
            await refreshActorOptions(initialBuildId, { quiet: true });
            applyRecipeData(currentRecipeData, { preserveActorOptions: true });
            return;
        }
        applyRecipeData(currentRecipeData, { preserveActorOptions: false });
    }

    function init() {
        dom.titleInput?.addEventListener("input", onHeaderInput);
        dom.operatorInput?.addEventListener("input", onHeaderInput);
        dom.newButton?.addEventListener("click", async () => {
            if (!confirmDiscardDirtyChanges()) { return; }
            if (dom.recipeSelect) { dom.recipeSelect.value = ""; }
            await refreshActorOptions(null, { quiet: true });
            applyRecipeData(null, { preserveActorOptions: true });
            window.history.replaceState(null, "", "/recipes");
        });
        dom.saveButton?.addEventListener("click", () => { void saveRecipe(); });
        dom.recipeSelect?.addEventListener("change", () => { void onRecipeSelectChange(); });
        dom.buildSelect?.addEventListener("change", () => { void onBuildSelectChange(); });

        document.addEventListener("keydown", (event) => {
            if ((event.ctrlKey || event.metaKey) && event.key === "s") {
                event.preventDefault();
                void saveRecipe();
            }
        });

        window.addEventListener("beforeunload", (event) => {
            if (!state.dirty) { return; }
            event.preventDefault();
            event.returnValue = "";
        });

        void initializePage();
    }

    init();
})();
