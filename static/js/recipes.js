(function () {
    "use strict";

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
    const TARGET_FIELD_BADGES = {
        target_temp_c: "T",
        pressure_mbar_a: "P",
        rpm: "RPM",
    };
    const STATUS_OPTIONS = [
        { value: "", label: "No change" },
        { value: "on", label: "ON" },
        { value: "off", label: "OFF" },
    ];

    function parseJsonScript(id, fallback) {
        const element = document.getElementById(id);
        if (!element) {
            return fallback;
        }
        try {
            return JSON.parse(element.textContent);
        } catch (_error) {
            return fallback;
        }
    }

    function asString(value, fallback = "") {
        if (value == null) {
            return fallback;
        }
        const normalized = String(value).trim();
        return normalized || fallback;
    }

    function parseId(value) {
        if (value == null || value === "") {
            return null;
        }
        const parsed = Number.parseInt(String(value), 10);
        return Number.isFinite(parsed) ? parsed : null;
    }

    function parseOptionalNumber(value) {
        if (value == null || value === "") {
            return null;
        }
        const parsed = Number.parseFloat(String(value));
        return Number.isFinite(parsed) ? parsed : null;
    }

    function priorityFrom(value, fallback = null) {
        if (value == null || value === "") {
            return fallback;
        }
        const normalized = String(value).trim();
        if (!/^(?:[1-9]|10)$/.test(normalized)) {
            return fallback;
        }
        const parsed = Number.parseInt(normalized, 10);
        return parsed >= PRIORITY_MIN && parsed <= PRIORITY_MAX ? parsed : fallback;
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
        const responseText = await response.text();
        let payload = {};

        if (responseText) {
            try {
                payload = JSON.parse(responseText);
            } catch (_error) {
                payload = {};
            }
        }

        if (!response.ok) {
            throw new Error(payload.error || `Request failed (HTTP ${response.status}).`);
        }
        return payload;
    }

    function emptyStep() {
        return {
            actors: [],
            task: "",
            delta_time: null,
        };
    }

    function emptyActorParams() {
        return {
            status_on: null,
            target_temp_c: null,
            pressure_mbar_a: null,
            rpm: null,
        };
    }

    function actorIdForRef(ref) {
        return asString(ref?.actor_id || ref?.actor);
    }

    function actorTypeForProfile(profileId, symbolId, fallback = "") {
        const profile = asString(profileId).toLowerCase();
        const symbol = asString(symbolId).toLowerCase();
        if (profile === "hc_system_temperature" || symbol === "hc_system") {
            return "H/C System";
        }
        if (profile === "motor_rpm" || symbol === "motor") {
            return "Motor";
        }
        if (profile === "pump_rpm" || symbol === "pump") {
            return "Pump";
        }
        if (profile.includes("pressure") || profile.includes("vacuum") || symbol.includes("pressure") || symbol.includes("vacuum")) {
            return "Pressure";
        }
        return fallback || "Actor";
    }

    function parseOptionalStatus(value) {
        if (value == null) {
            return null;
        }
        if (value === true || value === false) {
            return value;
        }
        return null;
    }

    function normalizeActorParams(rawParams) {
        const params = emptyActorParams();
        const source = rawParams && typeof rawParams === "object" ? rawParams : {};
        params.status_on = parseOptionalStatus(source.status_on);
        for (const paramField of ACTOR_NUMERIC_PARAM_FIELDS) {
            params[paramField] = parseOptionalNumber(source[paramField]);
        }
        return params;
    }

    function nextAvailablePriority(refs) {
        const used = new Set(refs.map((ref) => priorityFrom(ref.priority)).filter(Boolean));
        for (let priority = PRIORITY_MIN; priority <= PRIORITY_MAX; priority += 1) {
            if (!used.has(priority)) {
                return priority;
            }
        }
        return PRIORITY_MAX;
    }

    function normalizeActorRefs(rawActors) {
        const rawRefs = Array.isArray(rawActors) ? rawActors : [];
        const rawItems = rawRefs.length > 0 ? rawRefs : [];
        const refs = [];
        const seen = new Set();

        rawItems.forEach((rawRef, index) => {
            const rawObject = rawRef && typeof rawRef === "object" ? rawRef : {};
            const actorId = asString(rawObject.actor_id || rawObject.actor);
            const key = actorId.toLowerCase();
            if (!actorId || seen.has(key)) {
                return;
            }
            seen.add(key);

            const option = actorOption(actorId);
            const fallbackPriority = Math.min(index + 1, PRIORITY_MAX);
            const priority = priorityFrom(rawObject.priority, fallbackPriority);
            refs.push({
                actor: actorId,
                actor_id: actorId,
                actor_type: asString(rawObject.actor_type, option?.actor_type || actorTypeForProfile(option?.profile_id, option?.symbol_id)),
                priority,
                params: normalizeActorParams(rawObject.params),
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
        if (!node || typeof node !== "object") {
            return false;
        }
        if (asString(node.category).toLowerCase() === "actuators") {
            return true;
        }
        const control = node.control;
        return Boolean(control && typeof control === "object" && asString(control.profile_id));
    }

    function actorOptionsForBuild(buildData) {
        const definition = buildData && typeof buildData === "object" && buildData.definition_json && typeof buildData.definition_json === "object"
            ? buildData.definition_json
            : {};
        const rawNodes = Array.isArray(definition.nodes) ? definition.nodes : [];
        const seenActorIds = new Set();
        const options = [];

        for (const rawNode of rawNodes) {
            if (!isActorNode(rawNode)) {
                continue;
            }

            const instanceId = asString(rawNode.instance_id);
            if (!instanceId) {
                continue;
            }

            const lookupKey = instanceId.toLowerCase();
            if (seenActorIds.has(lookupKey)) {
                continue;
            }
            seenActorIds.add(lookupKey);

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

        options.sort((left, right) => left.value.localeCompare(right.value, undefined, { sensitivity: "base" }));
        return options;
    }

    const metaData = parseJsonScript("recipe-meta", {});
    const currentRecipeData = parseJsonScript("recipe-current-data", null);

    const state = {
        recipeId: parseId(metaData.selectedRecipeId),
        reactorBuildId: parseId(currentRecipeData && currentRecipeData.reactor_build_id),
        actorOptions: [],
        steps: [],
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
        tableBody: document.getElementById("recipe-tbody"),
        statusMessage: document.getElementById("recipe-status-msg"),
        flowHint: document.getElementById("recipe-no-flowsheet-hint"),
    };

    function actorLookup() {
        return new Map(state.actorOptions.map((option) => [option.value.toLowerCase(), option]));
    }

    function isKnownActor(value) {
        const normalized = asString(value).toLowerCase();
        return Boolean(normalized) && actorLookup().has(normalized);
    }

    function actorRefsForStep(step) {
        const normalized = normalizeActorRefs(step?.actors);
        if (step && step.actors !== normalized) {
            step.actors = normalized;
        }
        return normalized;
    }

    function sortedActorRefsForStep(step) {
        return actorRefsForStep(step)
            .map((ref, index) => ({ ref, index }))
            .sort((left, right) => {
                const leftPriority = priorityFrom(left.ref.priority, PRIORITY_MAX);
                const rightPriority = priorityFrom(right.ref.priority, PRIORITY_MAX);
                if (leftPriority !== rightPriority) {
                    return leftPriority - rightPriority;
                }
                return left.index - right.index;
            })
            .map((item) => item.ref);
    }

    function actorOption(value) {
        const normalized = asString(value).toLowerCase();
        return normalized ? actorLookup().get(normalized) || null : null;
    }

    function targetFieldsForActor(actorValue) {
        const option = actorOption(actorValue);
        const profileId = asString(option?.profile_id).toLowerCase();
        const symbolId = asString(option?.symbol_id).toLowerCase();
        if (profileId === "hc_system_temperature") {
            return ["target_temp_c"];
        }
        if (profileId === "motor_rpm" || profileId === "pump_rpm") {
            return ["rpm"];
        }
        if (profileId.includes("pressure") || profileId.includes("vacuum") || symbolId.includes("pressure") || symbolId.includes("vacuum")) {
            return ["pressure_mbar_a"];
        }
        if (profileId.includes("pump") || symbolId.includes("pump")) {
            return ["rpm"];
        }
        return ACTOR_NUMERIC_PARAM_FIELDS;
    }

    function statusSupportedForActor(actorValue) {
        const option = actorOption(actorValue);
        const profileId = asString(option?.profile_id).toLowerCase();
        const symbolId = asString(option?.symbol_id).toLowerCase();
        return (
            profileId === "hc_system_temperature" ||
            profileId === "motor_rpm" ||
            symbolId === "hc_system" ||
            symbolId === "motor"
        );
    }

    function activeTargetFields(step) {
        const fields = new Set();
        for (const ref of actorRefsForStep(step)) {
            for (const fieldName of targetFieldsForActor(ref.actor)) {
                fields.add(fieldName);
            }
        }
        return fields;
    }

    function actorFieldHint(actorValue) {
        const fields = targetFieldsForActor(actorValue);
        return fields.map((fieldName) => TARGET_FIELD_LABELS[fieldName] || fieldName).join(", ");
    }

    function stepActorsValid(step) {
        const refs = actorRefsForStep(step);
        return refs.length > 0 && refs.every((ref) => isKnownActor(actorIdForRef(ref)));
    }

    function duplicatePriorities(step) {
        const counts = new Map();
        for (const ref of actorRefsForStep(step)) {
            const priority = priorityFrom(ref.priority);
            if (!priority) {
                continue;
            }
            counts.set(priority, (counts.get(priority) || 0) + 1);
        }
        return [...counts.entries()]
            .filter((item) => item[1] > 1)
            .map((item) => item[0])
            .sort((left, right) => left - right);
    }

    function updateStepCount() {
        if (dom.stepCount) {
            dom.stepCount.textContent = String(state.steps.length);
        }
    }

    function setStatus(message, tone = "muted") {
        if (!dom.statusMessage) {
            return;
        }
        dom.statusMessage.textContent = message || "";
        dom.statusMessage.className = tone === "error" ? "error-text" : "muted";
    }

    function setSaveState(label, badgeClass) {
        if (!dom.saveStateBadge) {
            return;
        }
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
        if (!dom.statusBadge) {
            return;
        }

        const normalizedStatus = asString(status, "draft");
        let badgeClass = "badge-muted";
        if (normalizedStatus === "approved") {
            badgeClass = "badge-success";
        } else if (normalizedStatus === "archived") {
            badgeClass = "badge-warning";
        }

        dom.statusBadge.textContent = normalizedStatus;
        dom.statusBadge.className = `badge ${badgeClass}`;
    }

    function syncBuildSelect() {
        if (!dom.buildSelect) {
            return;
        }
        dom.buildSelect.value = state.reactorBuildId ? String(state.reactorBuildId) : "";
    }

    function currentFlowHint() {
        if (state.loadingBuild) {
            return "Loading actor list from the selected flowsheet.";
        }
        if (!state.reactorBuildId) {
            return "Select a flowsheet above to populate the Actor dropdown.";
        }
        if (state.actorOptions.length === 0) {
            return "The selected flowsheet does not contain any actors.";
        }
        return "";
    }

    function syncFlowHint() {
        if (!dom.flowHint) {
            return;
        }
        const message = currentFlowHint();
        dom.flowHint.textContent = message;
        dom.flowHint.classList.toggle("is-hidden", !message);
    }

    function invalidActorCount() {
        if (!state.reactorBuildId) {
            return 0;
        }
        let invalidCount = 0;
        for (const step of state.steps) {
            if (!stepActorsValid(step)) {
                invalidCount += 1;
            }
        }
        return invalidCount;
    }

    function canEditSteps() {
        return Boolean(state.reactorBuildId) && state.actorOptions.length > 0 && !state.loadingBuild;
    }

    function createStepFromPrevious() {
        if (!canEditSteps()) {
            return null;
        }

        const nextStep = emptyStep();
        const previousStep = state.steps.length > 0 ? state.steps[state.steps.length - 1] : null;
        if (previousStep) {
            nextStep.delta_time = previousStep.delta_time;
        }

        state.steps.push(nextStep);
        markUnsaved();
        return state.steps.length - 1;
    }

    function actorDisplayHtml(ref) {
        const actorId = actorIdForRef(ref);
        const option = actorOption(actorId);
        const label = option?.label || `${actorId} (not in flowsheet)`;
        const actorType = asString(ref.actor_type, option?.actor_type || actorTypeForProfile(option?.profile_id, option?.symbol_id));
        const badges = targetFieldsForActor(actorId)
            .map((fieldName) => `<span class="recipe-actor-field-badge">${escapeHtml(TARGET_FIELD_BADGES[fieldName] || fieldName)}</span>`)
            .join("");
        const invalidClass = option ? "" : " is-invalid";
        return `
            <div class="recipe-actor-chip${invalidClass}" title="${escapeHtml(`${label}: ${actorFieldHint(actorId) || "No recipe fields"}`)}">
                <span class="recipe-actor-chip-main">
                    <span class="recipe-actor-chip-label">${escapeHtml(label)}</span>
                    <span class="recipe-actor-type">${escapeHtml(actorType)}</span>
                </span>
                <span class="recipe-actor-chip-fields">${badges}</span>
            </div>
        `;
    }

    function makeActorPicker(step, rowIndex, isEmpty, disabled) {
        const refs = actorRefsForStep(step);
        const selectedKeys = new Set(refs.map((ref) => actorIdForRef(ref).toLowerCase()));
        let placeholder = "Add actor";
        if (!state.reactorBuildId) {
            placeholder = "Select flowsheet first";
        } else if (state.actorOptions.length === 0) {
            placeholder = "No actors available";
        } else if (isEmpty) {
            placeholder = "Select actor...";
        }

        let html = `<div class="recipe-actor-picker${isEmpty ? " recipe-input-empty" : ""}">`;
        html += `<select data-field="actor" data-row="${rowIndex}" class="recipe-actor-select"${disabled ? " disabled" : ""}>`;
        html += `<option value="">${escapeHtml(placeholder)}</option>`;

        for (const option of state.actorOptions) {
            if (selectedKeys.has(option.value.toLowerCase())) {
                continue;
            }
            html += `<option value="${escapeHtml(option.value)}">${escapeHtml(option.label)} | ${escapeHtml(actorFieldHint(option.value))}</option>`;
        }

        html += "</select>";
        html += "</div>";
        return html;
    }

    function makeTextInput(value, fieldName, rowIndex, isEmpty, placeholder, disabled) {
        const normalizedValue = asString(value);
        return `<input type="text" maxlength="255" data-field="${fieldName}" data-row="${rowIndex}" class="recipe-text-input${isEmpty ? " recipe-input-empty" : ""}" value="${escapeHtml(normalizedValue)}"${placeholder ? ` placeholder="${escapeHtml(placeholder)}"` : ""}${disabled ? " disabled" : ""}>`;
    }

    function makeStepNumericInput(value, fieldName, rowIndex, isEmpty, disabled) {
        const normalizedValue = value == null ? "" : String(value);
        return `<input type="number" step="0.01" min="0" data-field="${fieldName}" data-row="${rowIndex}" class="recipe-num-input${isEmpty ? " recipe-input-empty" : ""}" value="${escapeHtml(normalizedValue)}"${isEmpty ? ' placeholder="..."' : ""}${disabled ? " disabled" : ""}>`;
    }

    function makePriorityInput(ref, rowIndex, disabled) {
        const actorId = actorIdForRef(ref);
        const value = priorityFrom(ref.priority, nextAvailablePriority(actorRefsForStep(state.steps[rowIndex]))) || PRIORITY_MAX;
        const priorityClass = value <= 3 ? " is-high" : value <= 7 ? " is-medium" : " is-low";
        return `<input type="number" inputmode="numeric" min="${PRIORITY_MIN}" max="${PRIORITY_MAX}" step="1" data-field="priority" data-row="${rowIndex}" data-actor="${escapeHtml(actorId)}" class="recipe-priority-input${priorityClass}" value="${escapeHtml(value)}"${disabled ? " disabled" : ""}>`;
    }

    function statusSelectValue(ref) {
        const value = ref.params && typeof ref.params === "object" ? ref.params.status_on : null;
        if (value === true) {
            return "on";
        }
        if (value === false) {
            return "off";
        }
        return "";
    }

    function makeActorStatusSelect(ref, rowIndex, disabled) {
        const actorId = actorIdForRef(ref);
        const supported = statusSupportedForActor(actorId);
        const value = supported ? statusSelectValue(ref) : "";
        const selectDisabled = disabled || !supported;
        const title = supported ? "Set actor status for this step." : "This actor has no ON/OFF command in recipes.";
        const options = STATUS_OPTIONS.map((option) => (
            `<option value="${escapeHtml(option.value)}"${option.value === value ? " selected" : ""}>${escapeHtml(option.label)}</option>`
        )).join("");
        return `<select data-param="status_on" data-row="${rowIndex}" data-actor="${escapeHtml(actorId)}" class="recipe-status-select" title="${escapeHtml(title)}"${selectDisabled ? " disabled" : ""}>${options}</select>`;
    }

    function makeActorParamInput(ref, paramField, rowIndex, disabled) {
        const actorId = actorIdForRef(ref);
        const activeFields = new Set(targetFieldsForActor(actorId));
        const fieldActive = activeFields.has(paramField);
        const value = ref.params && typeof ref.params === "object" ? ref.params[paramField] : null;
        const normalizedValue = value == null ? "" : String(value);
        const minAttr = paramField === "target_temp_c" ? ' min="-40"' : ' min="0"';
        const maxAttr = paramField === "rpm" ? ' max="2000"' : "";
        const statusOff = ref.params && typeof ref.params === "object" && ref.params.status_on === false;
        const targetDisabled = disabled || !fieldActive || statusOff;
        let title = "";
        if (!fieldActive) {
            title = `${TARGET_FIELD_LABELS[paramField] || paramField} is not used by this actor.`;
        } else if (statusOff) {
            title = "Status OFF sends no setpoint for this actor.";
        }
        const fieldTitle = title ? ` title="${escapeHtml(title)}"` : "";
        return `<input type="number" step="0.01"${minAttr}${maxAttr} data-param="${paramField}" data-row="${rowIndex}" data-actor="${escapeHtml(actorId)}" class="recipe-num-input${fieldActive ? "" : " recipe-num-input-inactive"}" value="${escapeHtml(normalizedValue)}"${targetDisabled ? " disabled" : ""}${fieldTitle}>`;
    }

    function duplicatePriorityWarningHtml(step) {
        const duplicates = duplicatePriorities(step);
        if (!duplicates.length) {
            return "";
        }
        return `<div class="recipe-priority-warning">Duplicate priority ${escapeHtml(duplicates.join(", "))}. Equal priorities run in table order.</div>`;
    }

    function makeActorInlineBlock(ref, index, disabled) {
        const actorId = actorIdForRef(ref);
        const activeFields = targetFieldsForActor(actorId);
        const hasStatus = statusSupportedForActor(actorId);
        let html = `<div class="recipe-actor-inline">`;
        html += actorDisplayHtml(ref);
        html += `<span class="recipe-actor-inline-controls">`;
        html += `<span class="recipe-priority-cell">${makePriorityInput(ref, index, disabled)}</span>`;
        if (hasStatus) {
            html += `<span class="recipe-status-cell">${makeActorStatusSelect(ref, index, disabled)}</span>`;
        }
        for (const field of activeFields) {
            html += `<span class="recipe-param-cell">${makeActorParamInput(ref, field, index, disabled)}</span>`;
        }
        html += `<button type="button" class="recipe-actor-remove" data-row="${index}" data-actor="${escapeHtml(actorId)}" title="Remove actor"${disabled ? " disabled" : ""}>×</button>`;
        html += `</span></div>`;
        return html;
    }

    function renderTable() {
        if (!dom.tableBody) {
            return;
        }

        const controlsDisabled = !canEditSteps();
        let html = "";

        for (let index = 0; index < state.steps.length; index += 1) {
            const step = state.steps[index];
            const actorRequired = Boolean(state.reactorBuildId) && !stepActorsValid(step);
            const refs = sortedActorRefsForStep(step);

            html += `<tr class="recipe-step-row" data-row="${index}">`;
            html += `<td class="recipe-num-cell recipe-step-cell">`;
            html += `<span>${index + 1}</span>`;
            html += `<button class="btn recipe-del-btn" data-del="${index}" type="button" title="Delete step"${controlsDisabled ? " disabled" : ""}>×</button>`;
            html += `</td>`;
            html += `<td class="recipe-step-cell recipe-task-cell">${makeTextInput(step.task, "task", index, false, "", controlsDisabled)}</td>`;
            html += `<td class="recipe-step-cell recipe-delta-cell">${makeStepNumericInput(step.delta_time, "delta_time", index, false, controlsDisabled)}</td>`;
            html += `<td class="recipe-actors-combined-cell${actorRequired ? " recipe-cell-required" : ""}">`;
            for (const ref of refs) {
                html += makeActorInlineBlock(ref, index, controlsDisabled);
            }
            if (!refs.length) {
                html += `<div class="recipe-actor-placeholder-row"><span class="recipe-actor-placeholder">No actor selected</span></div>`;
            }
            html += makeActorPicker(step, index, false, controlsDisabled);
            html += duplicatePriorityWarningHtml(step);
            html += `</td>`;
            html += `</tr>`;
        }

        const emptyRowIndex = state.steps.length;
        const emptyDraftStep = emptyStep();
        html += `<tr class="recipe-empty-row" data-row="${emptyRowIndex}">`;
        html += `<td class="recipe-num-cell recipe-num-cell-empty">+</td>`;
        html += `<td>${makeTextInput("", "task", emptyRowIndex, true, "Click to add step...", controlsDisabled)}</td>`;
        html += `<td>${makeStepNumericInput("", "delta_time", emptyRowIndex, true, controlsDisabled)}</td>`;
        html += `<td>${makeActorPicker(emptyDraftStep, emptyRowIndex, true, controlsDisabled)}</td>`;
        html += `</tr>`;

        dom.tableBody.innerHTML = html;
        updateStepCount();
        syncFlowHint();
        attachRowListeners();
    }

    function attachRowListeners() {
        if (!dom.tableBody) {
            return;
        }

        for (const button of dom.tableBody.querySelectorAll("[data-del]")) {
            button.addEventListener("click", onDeleteRow);
        }

        for (const input of dom.tableBody.querySelectorAll('input[data-field="task"]')) {
            input.addEventListener("input", onTaskInput);
            input.addEventListener("focus", onRowControlFocus);
        }

        for (const input of dom.tableBody.querySelectorAll('input[data-field="delta_time"]')) {
            input.addEventListener("input", onStepNumericInput);
            input.addEventListener("focus", onRowControlFocus);
        }

        for (const input of dom.tableBody.querySelectorAll("input[data-param]")) {
            input.addEventListener("input", onActorParamInput);
        }

        for (const select of dom.tableBody.querySelectorAll('select[data-param="status_on"]')) {
            select.addEventListener("change", onActorStatusChange);
        }

        for (const input of dom.tableBody.querySelectorAll('input[data-field="priority"]')) {
            input.addEventListener("input", onPriorityInput);
            input.addEventListener("change", onPriorityChange);
        }

        for (const select of dom.tableBody.querySelectorAll('select[data-field="actor"]')) {
            select.addEventListener("change", onActorChange);
            select.addEventListener("focus", onRowControlFocus);
        }

        for (const button of dom.tableBody.querySelectorAll(".recipe-actor-remove")) {
            button.addEventListener("click", onActorRemove);
        }

    }

    function onDeleteRow(event) {
        const rowIndex = parseId(event.currentTarget.getAttribute("data-del"));
        if (rowIndex == null || rowIndex < 0 || rowIndex >= state.steps.length) {
            return;
        }
        state.steps.splice(rowIndex, 1);
        markUnsaved();
        renderTable();
    }

    function onRowControlFocus(event) {
        const control = event.currentTarget;
        const rowIndex = parseId(control.getAttribute("data-row"));
        if (rowIndex == null || rowIndex !== state.steps.length) {
            return;
        }

        const newRowIndex = createStepFromPrevious();
        if (newRowIndex == null) {
            if (!state.reactorBuildId) {
                setStatus("Select a flowsheet before adding steps.", "error");
            } else if (state.actorOptions.length === 0) {
                setStatus("The selected flowsheet does not contain any actors.", "error");
            }
            return;
        }

        const fieldName = control.getAttribute("data-field");
        renderTable();

        const selector = fieldName === "actor"
            ? `select[data-row="${newRowIndex}"][data-field="${fieldName}"]`
            : `input[data-row="${newRowIndex}"][data-field="${fieldName}"]`;
        const target = dom.tableBody ? dom.tableBody.querySelector(selector) : null;
        if (target) {
            target.focus();
        }
    }

    function onTaskInput(event) {
        const input = event.currentTarget;
        const rowIndex = parseId(input.getAttribute("data-row"));
        if (rowIndex == null || rowIndex >= state.steps.length) {
            return;
        }
        state.steps[rowIndex].task = input.value;
        markUnsaved();
    }

    function onStepNumericInput(event) {
        const input = event.currentTarget;
        const rowIndex = parseId(input.getAttribute("data-row"));
        const fieldName = input.getAttribute("data-field");
        if (rowIndex == null || rowIndex >= state.steps.length || !fieldName) {
            return;
        }

        const rawValue = input.value.trim();
        state.steps[rowIndex][fieldName] = rawValue === "" ? null : Number.parseFloat(rawValue);
        markUnsaved();
    }

    function actorRefForRow(rowIndex, actorId) {
        if (rowIndex == null || rowIndex >= state.steps.length || !actorId) {
            return null;
        }
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
        if (!ref || !ACTOR_PARAM_FIELDS.includes(paramField)) {
            return;
        }
        const rawValue = input.value.trim();
        ref.params = ref.params && typeof ref.params === "object" ? ref.params : emptyActorParams();
        ref.params[paramField] = rawValue === "" ? null : Number.parseFloat(rawValue);
        markUnsaved();
    }

    function onActorStatusChange(event) {
        const select = event.currentTarget;
        const rowIndex = parseId(select.getAttribute("data-row"));
        const actorId = asString(select.getAttribute("data-actor"));
        const ref = actorRefForRow(rowIndex, actorId);
        if (!ref) {
            return;
        }
        ref.params = ref.params && typeof ref.params === "object" ? ref.params : emptyActorParams();
        if (select.value === "on") {
            ref.params.status_on = true;
        } else if (select.value === "off") {
            ref.params.status_on = false;
            for (const fieldName of ACTOR_NUMERIC_PARAM_FIELDS) {
                ref.params[fieldName] = null;
            }
        } else {
            ref.params.status_on = null;
        }
        markUnsaved();
        renderTable();
    }

    function syncPriorityInputValidity(input) {
        const valid = priorityInputValid(input.value);
        input.classList.toggle("is-invalid", !valid);
        input.setAttribute("aria-invalid", String(!valid));
        input.setCustomValidity(valid ? "" : "Priority must be an integer from 1 to 10.");
        return valid;
    }

    function onPriorityInput(event) {
        syncPriorityInputValidity(event.currentTarget);
    }

    function onPriorityChange(event) {
        const input = event.currentTarget;
        if (!syncPriorityInputValidity(input)) {
            setStatus("Priority must be an integer from 1 to 10.", "error");
            return;
        }
        const rowIndex = parseId(input.getAttribute("data-row"));
        const actorId = asString(input.getAttribute("data-actor"));
        const ref = actorRefForRow(rowIndex, actorId);
        if (!ref) {
            return;
        }
        ref.priority = priorityFrom(input.value, ref.priority);
        markUnsaved();
        renderTable();
        const duplicates = duplicatePriorities(state.steps[rowIndex] || {});
        if (duplicates.length) {
            setStatus("Duplicate priorities are allowed; equal priorities run in table order.", "muted");
        }
    }

    function onActorChange(event) {
        const select = event.currentTarget;
        const rowIndex = parseId(select.getAttribute("data-row"));
        if (rowIndex == null || rowIndex >= state.steps.length) {
            return;
        }

        const actor = asString(select.value);
        if (!actor) {
            return;
        }
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
        renderTable();
    }

    function onActorRemove(event) {
        const button = event.currentTarget;
        const rowIndex = parseId(button.getAttribute("data-row"));
        const actor = asString(button.getAttribute("data-actor"));
        if (rowIndex == null || rowIndex >= state.steps.length || !actor) {
            return;
        }

        const refs = actorRefsForStep(state.steps[rowIndex]).filter((ref) => actorIdForRef(ref).toLowerCase() !== actor.toLowerCase());
        state.steps[rowIndex].actors = refs;
        markUnsaved();
        renderTable();
    }

    async function loadBuildActors(buildId, { quiet = false } = {}) {
        state.reactorBuildId = parseId(buildId);
        state.actorOptions = [];
        syncBuildSelect();

        if (!state.reactorBuildId) {
            return [];
        }

        const buildData = await fetchJson(`/api/reactor-builds/${state.reactorBuildId}`);
        state.actorOptions = actorOptionsForBuild(buildData);
        if (!quiet && state.actorOptions.length === 0) {
            setStatus("The selected flowsheet does not contain any actors.", "error");
        }
        return state.actorOptions;
    }

    async function refreshActorOptions(buildId, { quiet = false, renderPending = false } = {}) {
        state.loadingBuild = true;
        if (renderPending) {
            renderTable();
        }

        try {
            return await loadBuildActors(buildId, { quiet });
        } catch (error) {
            state.actorOptions = [];
            if (!quiet) {
                setStatus(error.message || "Could not load flowsheet actors.", "error");
            }
            return [];
        } finally {
            state.loadingBuild = false;
            syncBuildSelect();
            renderTable();
        }
    }

    function applyRecipeData(recipeData, { preserveActorOptions = false } = {}) {
        if (!recipeData) {
            state.recipeId = null;
            state.reactorBuildId = null;
            state.steps = [];
            if (!preserveActorOptions) {
                state.actorOptions = [];
            }
            if (dom.recipeSelect) {
                dom.recipeSelect.value = "";
            }
            if (dom.titleInput) {
                dom.titleInput.value = "";
            }
            if (dom.operatorInput) {
                dom.operatorInput.value = "";
            }
            updateStatusBadge("draft");
            markSaved();
            syncBuildSelect();
            renderTable();
            setStatus("");
            return;
        }

        state.recipeId = parseId(recipeData.recipe_id);
        state.reactorBuildId = parseId(recipeData.reactor_build_id);
        state.steps = Array.isArray(recipeData.steps) ? recipeData.steps.map(normalizeLoadedStep) : [];

        if (dom.recipeSelect) {
            dom.recipeSelect.value = state.recipeId ? String(state.recipeId) : "";
        }
        if (dom.titleInput) {
            dom.titleInput.value = asString(recipeData.title);
        }
        if (dom.operatorInput) {
            dom.operatorInput.value = asString(recipeData.operator_name);
        }

        updateStatusBadge(asString(recipeData.status, "draft"));
        markSaved();
        syncBuildSelect();
        renderTable();

        if (state.reactorBuildId && invalidActorCount() > 0) {
            setStatus("One or more actor assignments no longer match the selected flowsheet.", "error");
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

    function readStepNumericValue(rowIndex, fieldName) {
        const input = dom.tableBody
            ? dom.tableBody.querySelector(`input[data-row="${rowIndex}"][data-field="${fieldName}"]`)
            : null;
        if (!input) {
            return null;
        }
        const rawValue = input.value.trim();
        if (!rawValue) {
            return null;
        }
        const parsed = Number.parseFloat(rawValue);
        return Number.isFinite(parsed) ? parsed : null;
    }

    function readActorParamValue(rowIndex, actorId, paramField) {
        const input = dom.tableBody
            ? dom.tableBody.querySelector(`input[data-row="${rowIndex}"][data-actor="${escapeSelectorValue(actorId)}"][data-param="${paramField}"]`)
            : null;
        if (!input || input.disabled) {
            return null;
        }
        const rawValue = input.value.trim();
        if (!rawValue) {
            return null;
        }
        const parsed = Number.parseFloat(rawValue);
        return Number.isFinite(parsed) ? parsed : null;
    }

    function readActorPriority(rowIndex, actorId, fallback) {
        const input = dom.tableBody
            ? dom.tableBody.querySelector(`input[data-row="${rowIndex}"][data-actor="${escapeSelectorValue(actorId)}"][data-field="priority"]`)
            : null;
        if (!input) {
            return fallback;
        }
        return priorityFrom(input.value, null);
    }

    function readActorStatusValue(rowIndex, actorId) {
        const select = dom.tableBody
            ? dom.tableBody.querySelector(`select[data-row="${rowIndex}"][data-actor="${escapeSelectorValue(actorId)}"][data-param="status_on"]`)
            : null;
        if (!select || select.disabled) {
            return null;
        }
        if (select.value === "on") {
            return true;
        }
        if (select.value === "off") {
            return false;
        }
        return null;
    }

    function collectPayload() {
        const steps = [];
        let hasInvalidPriority = false;

        for (let rowIndex = 0; rowIndex < state.steps.length; rowIndex += 1) {
            const step = emptyStep();
            const taskInput = dom.tableBody
                ? dom.tableBody.querySelector(`input[data-row="${rowIndex}"][data-field="task"]`)
                : null;

            step.actors = sortedActorRefsForStep(state.steps[rowIndex]).map((ref) => {
                const actorId = actorIdForRef(ref);
                const priority = readActorPriority(rowIndex, actorId, ref.priority);
                if (!priority) {
                    hasInvalidPriority = true;
                }
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

            const isCompletelyEmpty = step.actors.length === 0 && !step.task && STEP_NUMERIC_FIELDS.every((fieldName) => step[fieldName] == null);
            if (!isCompletelyEmpty) {
                steps.push(step);
            }
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

        const invalidStep = payload.steps.find((step) => !step.actors.length || step.actors.some((ref) => !isKnownActor(ref.actor_id || ref.actor)));
        if (invalidStep) {
            setStatus("At least one actor from the selected flowsheet is required for every step before saving.", "error");
            return;
        }

        const isCreate = !state.recipeId;
        const requestUrl = isCreate ? "/api/recipes" : `/api/recipes/${state.recipeId}`;
        const requestMethod = isCreate ? "POST" : "PATCH";
        if (!isCreate) {
            delete payload.created_by;
        }

        const headers = { "Content-Type": "application/json" };
        if (metaData.apiAuthRequired && metaData.recipeWriteToken) {
            headers["X-Recipe-Token"] = metaData.recipeWriteToken;
        }

        setSaveState("Saving...", "badge-warning");
        setStatus("");

        try {
            const savedRecipe = await fetchJson(requestUrl, {
                method: requestMethod,
                headers,
                body: JSON.stringify(payload),
            });

            state.recipeId = parseId(savedRecipe.recipe_id);
            state.steps = Array.isArray(savedRecipe.steps) ? savedRecipe.steps.map(normalizeLoadedStep) : [];
            updateStatusBadge(asString(savedRecipe.status, "draft"));
            markSaved();
            updateSelectorOption(savedRecipe);
            renderTable();
            setStatus(`Saved at ${savedRecipe.updated_at || new Date().toISOString()}`);
            window.history.replaceState(null, "", `/recipes?recipe_id=${savedRecipe.recipe_id}`);
        } catch (error) {
            setSaveState("Error", "badge-danger");
            setStatus(error.message || "Recipe save failed.", "error");
        }
    }

    function updateSelectorOption(savedRecipe) {
        if (!dom.recipeSelect) {
            return;
        }

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
        if (!state.dirty) {
            return true;
        }
        return window.confirm("Discard unsaved changes?");
    }

    function revertRecipeSelection() {
        if (!dom.recipeSelect) {
            return;
        }
        dom.recipeSelect.value = state.recipeId ? String(state.recipeId) : "";
    }

    async function onRecipeSelectChange() {
        const selectedRecipeId = parseId(dom.recipeSelect?.value);
        if (!selectedRecipeId) {
            if (!confirmDiscardDirtyChanges()) {
                revertRecipeSelection();
                return;
            }
            await refreshActorOptions(null, { quiet: true });
            applyRecipeData(null, { preserveActorOptions: true });
            window.history.replaceState(null, "", "/recipes");
            return;
        }

        if (!confirmDiscardDirtyChanges()) {
            revertRecipeSelection();
            return;
        }

        try {
            await loadRecipeById(selectedRecipeId);
        } catch (error) {
            setSaveState("Error", "badge-danger");
            setStatus(error.message || "Could not load the selected recipe.", "error");
            revertRecipeSelection();
        }
    }

    function onHeaderInput() {
        markUnsaved();
    }

    async function onBuildSelectChange() {
        const nextBuildId = parseId(dom.buildSelect?.value);
        await refreshActorOptions(nextBuildId, { renderPending: true });
        markUnsaved();

        if (!state.reactorBuildId) {
            setStatus("Select a flowsheet before adding steps.", "error");
            return;
        }
        if (state.actorOptions.length === 0) {
            setStatus("The selected flowsheet does not contain any actors.", "error");
            return;
        }
        if (invalidActorCount() > 0) {
            setStatus("Check actor assignments after changing the flowsheet.", "error");
            return;
        }
        setStatus("Actor list updated from the selected flowsheet.");
    }

    async function initializePage() {
        const initialBuildId = parseId(currentRecipeData && currentRecipeData.reactor_build_id);
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
            if (!confirmDiscardDirtyChanges()) {
                return;
            }
            if (dom.recipeSelect) {
                dom.recipeSelect.value = "";
            }
            await refreshActorOptions(null, { quiet: true });
            applyRecipeData(null, { preserveActorOptions: true });
            window.history.replaceState(null, "", "/recipes");
        });
        dom.saveButton?.addEventListener("click", () => {
            void saveRecipe();
        });
        dom.recipeSelect?.addEventListener("change", () => {
            void onRecipeSelectChange();
        });
        dom.buildSelect?.addEventListener("change", () => {
            void onBuildSelectChange();
        });

        document.addEventListener("keydown", (event) => {
            if ((event.ctrlKey || event.metaKey) && event.key === "s") {
                event.preventDefault();
                void saveRecipe();
            }
        });

        window.addEventListener("beforeunload", (event) => {
            if (!state.dirty) {
                return;
            }
            event.preventDefault();
            event.returnValue = "";
        });

        void initializePage();
    }

    init();
})();
