(function () {
    "use strict";

    const NUMERIC_FIELDS = ["delta_time", "temp", "pressure", "rpm"];
    const TARGET_FIELDS = ["temp", "pressure", "rpm"];
    const DEFAULT_PROFILE_BY_SYMBOL = {
        motor: "motor_rpm",
        hc_system: "hc_system_temperature",
        pump: "pump_rpm",
    };
    const TARGET_FIELD_LABELS = {
        temp: "Temp",
        pressure: "Pressure",
        rpm: "RPM",
    };
    const TARGET_FIELD_BADGES = {
        temp: "T",
        pressure: "P",
        rpm: "RPM",
    };

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

    function escapeHtml(value) {
        return String(value ?? "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
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
            actor: "",
            actors: [],
            task: "",
            delta_time: null,
            temp: null,
            pressure: null,
            rpm: null,
        };
    }

    function normalizeActorRefs(rawActors, fallbackActor = "") {
        const rawRefs = Array.isArray(rawActors) ? rawActors : [];
        const refs = [];
        if (rawRefs.length > 0) {
            for (const rawRef of rawRefs) {
                let actor = "";
                let priority = null;
                if (typeof rawRef === "string") {
                    actor = asString(rawRef);
                } else if (rawRef && typeof rawRef === "object") {
                    actor = asString(rawRef.actor);
                    const parsedPriority = Number.parseInt(String(rawRef.priority ?? ""), 10);
                    priority = Number.isFinite(parsedPriority) ? parsedPriority : null;
                }
                if (actor) {
                    refs.push({ actor, priority });
                }
            }
        } else if (fallbackActor) {
            refs.push({ actor: fallbackActor, priority: null });
        }

        const seen = new Set();
        return refs.filter((ref) => {
            const key = asString(ref.actor).toLowerCase();
            if (!key || seen.has(key)) {
                return false;
            }
            seen.add(key);
            return true;
        });
    }

    function normalizeLoadedStep(rawStep) {
        const payload = rawStep && typeof rawStep === "object" ? rawStep : {};
        const step = emptyStep();
        step.actors = normalizeActorRefs(payload.actors, asString(payload.actor));
        step.actor = step.actors[0]?.actor || "";
        step.task = asString(payload.task);
        for (const fieldName of NUMERIC_FIELDS) {
            const rawValue = payload[fieldName];
            if (rawValue == null || rawValue === "") {
                step[fieldName] = null;
                continue;
            }
            const parsed = Number.parseFloat(rawValue);
            step[fieldName] = Number.isFinite(parsed) ? parsed : null;
        }
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
            options.push({
                value: instanceId,
                label: labelText && labelText !== instanceId ? `${instanceId} | ${labelText}` : instanceId,
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
        const normalized = normalizeActorRefs(step?.actors, asString(step?.actor));
        if (step && (step.actors !== normalized || step.actor !== (normalized[0]?.actor || ""))) {
            step.actors = normalized;
            step.actor = normalized[0]?.actor || "";
        }
        return normalized;
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
            return ["temp"];
        }
        if (profileId === "motor_rpm" || profileId === "pump_rpm") {
            return ["rpm"];
        }
        if (profileId.includes("pressure") || profileId.includes("vacuum") || symbolId.includes("pressure") || symbolId.includes("vacuum")) {
            return ["pressure"];
        }
        if (profileId.includes("pump") || symbolId.includes("pump")) {
            return ["rpm"];
        }
        return TARGET_FIELDS;
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
        return refs.length > 0 && refs.every((ref) => isKnownActor(ref.actor));
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
            nextStep.actors = actorRefsForStep(previousStep)
                .filter((ref) => isKnownActor(ref.actor))
                .map((ref) => ({ ...ref }));
            nextStep.actor = nextStep.actors[0]?.actor || "";
            for (const fieldName of NUMERIC_FIELDS) {
                nextStep[fieldName] = previousStep[fieldName];
            }
        }

        state.steps.push(nextStep);
        markUnsaved();
        return state.steps.length - 1;
    }

    function actorChipHtml(ref, rowIndex, disabled) {
        const option = actorOption(ref.actor);
        const label = option?.label || `${ref.actor} (not in flowsheet)`;
        const badges = targetFieldsForActor(ref.actor)
            .map((fieldName) => `<span class="recipe-actor-field-badge">${escapeHtml(TARGET_FIELD_BADGES[fieldName] || fieldName)}</span>`)
            .join("");
        const invalidClass = option ? "" : " is-invalid";
        return `
            <span class="recipe-actor-chip${invalidClass}" title="${escapeHtml(`${label}: ${actorFieldHint(ref.actor) || "No recipe fields"}`)}">
                <span class="recipe-actor-chip-label">${escapeHtml(label)}</span>
                <span class="recipe-actor-chip-fields">${badges}</span>
                <button type="button" class="recipe-actor-remove" data-row="${rowIndex}" data-actor="${escapeHtml(ref.actor)}" title="Remove actor"${disabled ? " disabled" : ""}>x</button>
            </span>
        `;
    }

    function makeActorPicker(step, rowIndex, isEmpty, disabled) {
        const refs = actorRefsForStep(step);
        const selectedKeys = new Set(refs.map((ref) => ref.actor.toLowerCase()));
        let placeholder = "Add actor";
        if (!state.reactorBuildId) {
            placeholder = "Select flowsheet first";
        } else if (state.actorOptions.length === 0) {
            placeholder = "No actors available";
        } else if (isEmpty) {
            placeholder = "Select actor...";
        }

        let html = `<div class="recipe-actor-picker${isEmpty ? " recipe-input-empty" : ""}">`;
        html += '<div class="recipe-actor-chip-row">';
        html += refs.length
            ? refs.map((ref) => actorChipHtml(ref, rowIndex, disabled)).join("")
            : '<span class="recipe-actor-placeholder">No actor selected</span>';
        html += "</div>";
        html += `<select data-field="actor" data-row="${rowIndex}" class="recipe-actor-select"${disabled ? " disabled" : ""}>`;
        html += `<option value="">${escapeHtml(placeholder)}</option>`;

        for (const option of state.actorOptions) {
            if (selectedKeys.has(option.value.toLowerCase())) {
                continue;
            }
            html += `<option value="${escapeHtml(option.value)}">${escapeHtml(option.label)} | ${escapeHtml(actorFieldHint(option.value))}</option>`;
        }

        html += "</select>";
        if (refs.length > 1) {
            html += '<details class="recipe-actor-advanced">';
            html += '<summary>Advanced</summary>';
            html += '<div class="recipe-priority-grid">';
            for (const ref of refs) {
                const priorityValue = ref.priority == null ? "" : String(ref.priority);
                html += `
                    <label class="recipe-priority-field">
                        <span>${escapeHtml(ref.actor)}</span>
                        <input type="number" step="1" data-field="priority" data-row="${rowIndex}" data-actor="${escapeHtml(ref.actor)}" value="${escapeHtml(priorityValue)}" placeholder="0"${disabled ? " disabled" : ""}>
                    </label>
                `;
            }
            html += "</div>";
            html += "</details>";
        }
        html += "</div>";
        return html;
    }

    function makeTextInput(value, fieldName, rowIndex, isEmpty, placeholder, disabled) {
        const normalizedValue = asString(value);
        return `<input type="text" maxlength="255" data-field="${fieldName}" data-row="${rowIndex}" class="recipe-text-input${isEmpty ? " recipe-input-empty" : ""}" value="${escapeHtml(normalizedValue)}"${placeholder ? ` placeholder="${escapeHtml(placeholder)}"` : ""}${disabled ? " disabled" : ""}>`;
    }

    function makeNumericInput(value, fieldName, rowIndex, isEmpty, disabled, fieldActive = true) {
        const normalizedValue = value == null ? "" : String(value);
        const minAttr = fieldName === "temp" ? ' min="-40"' : ' min="0"';
        const maxAttr = fieldName === "rpm" ? ' max="2000"' : "";
        const targetDisabled = disabled || !fieldActive;
        const fieldTitle = fieldActive ? "" : ` title="${escapeHtml(`${TARGET_FIELD_LABELS[fieldName] || fieldName} is not used by the selected actor(s). Existing values are kept but ignored.`)}"`;
        return `<input type="number" step="0.01"${minAttr}${maxAttr} data-field="${fieldName}" data-row="${rowIndex}" class="recipe-num-input${isEmpty ? " recipe-input-empty" : ""}${fieldActive ? "" : " recipe-num-input-inactive"}" value="${escapeHtml(normalizedValue)}"${isEmpty ? ' placeholder="..."' : ""}${targetDisabled ? " disabled" : ""}${fieldTitle}>`;
    }

    function renderTable() {
        if (!dom.tableBody) {
            return;
        }

        const controlsDisabled = !canEditSteps();
        let html = "";

        for (let index = 0; index < state.steps.length; index += 1) {
            const step = state.steps[index];
            const fields = activeTargetFields(step);
            const actorRequired = Boolean(state.reactorBuildId) && !stepActorsValid(step);
            html += `<tr data-row="${index}">`;
            html += `<td class="recipe-num-cell">${index + 1}</td>`;
            html += `<td class="${actorRequired ? "recipe-cell-required" : ""}">${makeActorPicker(step, index, false, controlsDisabled)}</td>`;
            html += `<td>${makeTextInput(step.task, "task", index, false, "", controlsDisabled)}</td>`;
            html += `<td>${makeNumericInput(step.delta_time, "delta_time", index, false, controlsDisabled)}</td>`;
            html += `<td>${makeNumericInput(step.temp, "temp", index, false, controlsDisabled, fields.has("temp"))}</td>`;
            html += `<td>${makeNumericInput(step.pressure, "pressure", index, false, controlsDisabled, fields.has("pressure"))}</td>`;
            html += `<td>${makeNumericInput(step.rpm, "rpm", index, false, controlsDisabled, fields.has("rpm"))}</td>`;
            html += `<td><button class="btn recipe-del-btn" data-del="${index}" type="button" title="Delete row"${controlsDisabled ? " disabled" : ""}>X</button></td>`;
            html += "</tr>";
        }

        const emptyRowIndex = state.steps.length;
        const emptyDraftStep = emptyStep();
        html += `<tr class="recipe-empty-row" data-row="${emptyRowIndex}">`;
        html += '<td class="recipe-num-cell recipe-num-cell-empty">.</td>';
        html += `<td>${makeActorPicker(emptyDraftStep, emptyRowIndex, true, controlsDisabled)}</td>`;
        html += `<td>${makeTextInput("", "task", emptyRowIndex, true, "Click to add step...", controlsDisabled)}</td>`;
        html += `<td>${makeNumericInput("", "delta_time", emptyRowIndex, true, controlsDisabled)}</td>`;
        html += `<td>${makeNumericInput("", "temp", emptyRowIndex, true, true, false)}</td>`;
        html += `<td>${makeNumericInput("", "pressure", emptyRowIndex, true, true, false)}</td>`;
        html += `<td>${makeNumericInput("", "rpm", emptyRowIndex, true, true, false)}</td>`;
        html += "<td></td>";
        html += "</tr>";

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

        for (const input of dom.tableBody.querySelectorAll("input[type='number']:not([data-field='priority'])")) {
            input.addEventListener("input", onNumericInput);
            input.addEventListener("focus", onRowControlFocus);
        }

        for (const select of dom.tableBody.querySelectorAll('select[data-field="actor"]')) {
            select.addEventListener("change", onActorChange);
            select.addEventListener("focus", onRowControlFocus);
        }

        for (const button of dom.tableBody.querySelectorAll(".recipe-actor-remove")) {
            button.addEventListener("click", onActorRemove);
        }

        for (const input of dom.tableBody.querySelectorAll('input[data-field="priority"]')) {
            input.addEventListener("input", onPriorityInput);
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

    function onNumericInput(event) {
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
        if (!refs.some((ref) => ref.actor.toLowerCase() === actor.toLowerCase())) {
            refs.push({ actor, priority: null });
        }
        state.steps[rowIndex].actors = refs;
        state.steps[rowIndex].actor = refs[0]?.actor || "";
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

        const refs = actorRefsForStep(state.steps[rowIndex]).filter(
            (ref) => ref.actor.toLowerCase() !== actor.toLowerCase(),
        );
        state.steps[rowIndex].actors = refs;
        state.steps[rowIndex].actor = refs[0]?.actor || "";
        markUnsaved();
        renderTable();
    }

    function onPriorityInput(event) {
        const input = event.currentTarget;
        const rowIndex = parseId(input.getAttribute("data-row"));
        const actor = asString(input.getAttribute("data-actor"));
        if (rowIndex == null || rowIndex >= state.steps.length || !actor) {
            return;
        }

        const rawValue = input.value.trim();
        const refs = actorRefsForStep(state.steps[rowIndex]).map((ref) => {
            if (ref.actor.toLowerCase() !== actor.toLowerCase()) {
                return ref;
            }
            const priority = rawValue === "" ? null : Number.parseInt(rawValue, 10);
            return { ...ref, priority: Number.isFinite(priority) ? priority : null };
        });
        state.steps[rowIndex].actors = refs;
        state.steps[rowIndex].actor = refs[0]?.actor || "";
        markUnsaved();
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

    function readNumericValue(rowIndex, fieldName) {
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

    function collectPayload() {
        const steps = [];

        for (let rowIndex = 0; rowIndex < state.steps.length; rowIndex += 1) {
            const step = emptyStep();
            const taskInput = dom.tableBody
                ? dom.tableBody.querySelector(`input[data-row="${rowIndex}"][data-field="task"]`)
                : null;

            step.actors = actorRefsForStep(state.steps[rowIndex]).map((ref) => ({ ...ref }));
            step.actor = step.actors[0]?.actor || "";
            step.task = taskInput ? taskInput.value : asString(state.steps[rowIndex].task);
            for (const fieldName of NUMERIC_FIELDS) {
                step[fieldName] = readNumericValue(rowIndex, fieldName);
            }

            const isCompletelyEmpty = step.actors.length === 0 && !step.task && NUMERIC_FIELDS.every((fieldName) => step[fieldName] == null);
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

        const invalidStep = payload.steps.find((step) => !step.actors.length || step.actors.some((ref) => !isKnownActor(ref.actor)));
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
