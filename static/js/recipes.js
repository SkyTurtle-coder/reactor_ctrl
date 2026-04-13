(function () {
    "use strict";

    // -------------------------------------------------------------------------
    // Helpers
    // -------------------------------------------------------------------------

    function parseJsonScript(id) {
        var el = document.getElementById(id);
        if (!el) return null;
        try { return JSON.parse(el.textContent); } catch (_) { return null; }
    }

    function escapeHtml(str) {
        if (str === null || str === undefined) return "";
        return String(str)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
    }

    function setStatus(msg, tone) {
        var el = document.getElementById("recipe-status-msg");
        if (!el) return;
        el.textContent = msg || "";
        el.className = tone === "error" ? "error-text" : "muted";
    }

    function setSaveState(label, cssClass) {
        var el = document.getElementById("recipe-save-state");
        if (!el) return;
        el.textContent = label;
        el.className = "badge " + (cssClass || "badge-muted");
    }

    function markUnsaved() {
        setSaveState("Unsaved", "badge-warning");
        _state.dirty = true;
    }

    function markSaved() {
        setSaveState("Saved", "badge-muted");
        _state.dirty = false;
    }

    // -------------------------------------------------------------------------
    // State
    // -------------------------------------------------------------------------

    var _meta = parseJsonScript("recipe-meta") || {};
    var _savedList = parseJsonScript("recipe-saved-list") || [];
    var _currentData = parseJsonScript("recipe-current-data");

    var _state = {
        recipeId: _meta.selectedRecipeId || null,
        steps: [],     // real data rows (never includes the empty trailing row)
        dirty: false,
    };

    // Numeric fields that are copied when the user focuses the empty row
    var NUMERIC_FIELDS = ["delta_time", "temp", "pressure", "rpm"];

    // -------------------------------------------------------------------------
    // DOM refs
    // -------------------------------------------------------------------------

    var dom = {
        select: document.getElementById("recipe-select"),
        newBtn: document.getElementById("recipe-new-btn"),
        saveBtn: document.getElementById("recipe-save-btn"),
        title: document.getElementById("recipe-title"),
        operator: document.getElementById("recipe-operator"),
        statusBadge: document.getElementById("recipe-status-badge"),
        stepCount: document.getElementById("recipe-step-count"),
        tbody: document.getElementById("recipe-tbody"),
    };

    // -------------------------------------------------------------------------
    // Row rendering
    // -------------------------------------------------------------------------

    function makeNumericInput(value, fieldName, rowIndex, isEmpty) {
        var val = (value === null || value === undefined) ? "" : value;
        return (
            '<input type="number" step="0.01" min="0"' +
            ' data-field="' + fieldName + '"' +
            ' data-row="' + rowIndex + '"' +
            ' class="recipe-num-input' + (isEmpty ? " recipe-input-empty" : "") + '"' +
            ' value="' + escapeHtml(val) + '"' +
            (isEmpty ? ' placeholder="—"' : "") +
            '>'
        );
    }

    function makeTextInput(value, fieldName, rowIndex, isEmpty, placeholder) {
        var val = value || "";
        return (
            '<input type="text" maxlength="255"' +
            ' data-field="' + fieldName + '"' +
            ' data-row="' + rowIndex + '"' +
            ' class="recipe-text-input' + (isEmpty ? " recipe-input-empty" : "") + '"' +
            ' value="' + escapeHtml(val) + '"' +
            (isEmpty && placeholder ? ' placeholder="' + escapeHtml(placeholder) + '"' : "") +
            '>'
        );
    }

    function renderTable() {
        var rows = _state.steps;
        var html = "";

        // Data rows
        for (var i = 0; i < rows.length; i++) {
            var step = rows[i];
            var num = i + 1;
            html += '<tr data-row="' + i + '">';
            html += '<td class="recipe-num-cell">' + num + '</td>';
            html += '<td>' + makeTextInput(step.actor, "actor", i, false) + '</td>';
            html += '<td>' + makeTextInput(step.task, "task", i, false) + '</td>';
            html += '<td>' + makeNumericInput(step.delta_time, "delta_time", i, false) + '</td>';
            html += '<td>' + makeNumericInput(step.temp, "temp", i, false) + '</td>';
            html += '<td>' + makeNumericInput(step.pressure, "pressure", i, false) + '</td>';
            html += '<td>' + makeNumericInput(step.rpm, "rpm", i, false) + '</td>';
            html += '<td><button class="btn recipe-del-btn" data-del="' + i + '" type="button" title="Delete row">&#x2715;</button></td>';
            html += '</tr>';
        }

        // Always one empty trailing row
        var emptyIdx = rows.length;
        html += '<tr class="recipe-empty-row" data-row="' + emptyIdx + '">';
        html += '<td class="recipe-num-cell recipe-num-cell-empty"></td>';
        html += '<td>' + makeTextInput("", "actor", emptyIdx, true, "Actor…") + '</td>';
        html += '<td>' + makeTextInput("", "task", emptyIdx, true, "Click to add step…") + '</td>';
        html += '<td>' + makeNumericInput("", "delta_time", emptyIdx, true) + '</td>';
        html += '<td>' + makeNumericInput("", "temp", emptyIdx, true) + '</td>';
        html += '<td>' + makeNumericInput("", "pressure", emptyIdx, true) + '</td>';
        html += '<td>' + makeNumericInput("", "rpm", emptyIdx, true) + '</td>';
        html += '<td></td>';
        html += '</tr>';

        dom.tbody.innerHTML = html;

        if (dom.stepCount) {
            dom.stepCount.textContent = rows.length;
        }

        attachRowListeners();
    }

    // -------------------------------------------------------------------------
    // Row event listeners (re-attached after each render)
    // -------------------------------------------------------------------------

    function attachRowListeners() {
        // Delete buttons
        var delBtns = dom.tbody.querySelectorAll("[data-del]");
        for (var i = 0; i < delBtns.length; i++) {
            delBtns[i].addEventListener("click", onDeleteRow);
        }

        // All inputs — data sync
        var inputs = dom.tbody.querySelectorAll("input");
        for (var j = 0; j < inputs.length; j++) {
            inputs[j].addEventListener("input", onCellInput);
            inputs[j].addEventListener("focus", onCellFocus);
        }
    }

    function onDeleteRow(e) {
        var idx = parseInt(e.currentTarget.getAttribute("data-del"), 10);
        if (isNaN(idx) || idx < 0 || idx >= _state.steps.length) return;
        _state.steps.splice(idx, 1);
        markUnsaved();
        renderTable();
    }

    function onCellFocus(e) {
        var input = e.currentTarget;
        var rowIdx = parseInt(input.getAttribute("data-row"), 10);
        var isEmptyRow = rowIdx === _state.steps.length;
        if (!isEmptyRow) return;

        // Copy numeric values from previous row (if any), leave actor and task empty
        if (_state.steps.length > 0) {
            var prev = _state.steps[_state.steps.length - 1];
            var newStep = { actor: "", task: "" };
            for (var k = 0; k < NUMERIC_FIELDS.length; k++) {
                newStep[NUMERIC_FIELDS[k]] = prev[NUMERIC_FIELDS[k]];
            }
            _state.steps.push(newStep);
        } else {
            _state.steps.push(emptyStep());
        }
        markUnsaved();
        renderTable();

        // Restore focus to the same field in the newly rendered row (now second-to-last)
        var field = input.getAttribute("data-field");
        var newRowIdx = _state.steps.length - 1;
        var selector = 'input[data-row="' + newRowIdx + '"][data-field="' + field + '"]';
        var target = dom.tbody.querySelector(selector);
        if (target) target.focus();
    }

    function onCellInput(e) {
        var input = e.currentTarget;
        var rowIdx = parseInt(input.getAttribute("data-row"), 10);
        var field = input.getAttribute("data-field");

        // Only update real (non-empty-trailing) rows here
        // The empty row becomes real on focus (onCellFocus handles that)
        if (rowIdx >= _state.steps.length) return;

        var step = _state.steps[rowIdx];
        if (field === "task" || field === "actor") {
            step[field] = input.value;
        } else {
            var raw = input.value.trim();
            step[field] = raw === "" ? null : parseFloat(raw);
        }
        markUnsaved();

        // Update step count without full re-render
        if (dom.stepCount) {
            dom.stepCount.textContent = _state.steps.length;
        }
    }

    // -------------------------------------------------------------------------
    // Load / init
    // -------------------------------------------------------------------------

    function emptyStep() {
        return { actor: "", task: "", delta_time: null, temp: null, pressure: null, rpm: null };
    }

    function loadRecipe(data) {
        if (!data) {
            _state.recipeId = null;
            _state.steps = [];
            if (dom.title) dom.title.value = "";
            if (dom.operator) dom.operator.value = "";
            updateStatusBadge("draft");
            markSaved();
            renderTable();
            setStatus("");
            return;
        }
        _state.recipeId = data.recipe_id || null;
        _state.steps = Array.isArray(data.steps) ? data.steps.slice() : [];
        if (dom.title) dom.title.value = data.title || "";
        if (dom.operator) dom.operator.value = data.operator_name || "";
        updateStatusBadge(data.status || "draft");
        markSaved();
        renderTable();
        setStatus(data.updated_at ? "Last saved: " + data.updated_at : "");
    }

    function updateStatusBadge(status) {
        if (!dom.statusBadge) return;
        dom.statusBadge.textContent = status || "draft";
        var cls = "badge-muted";
        if (status === "approved") cls = "badge-success";
        else if (status === "archived") cls = "badge-warning";
        dom.statusBadge.className = "badge " + cls;
    }

    // -------------------------------------------------------------------------
    // Save
    // -------------------------------------------------------------------------

    function collectPayload() {
        // Read current input values directly from the DOM for the real rows
        // (in case the user typed without triggering onCellInput on the last row)
        var rows = dom.tbody.querySelectorAll("tr[data-row]");
        var steps = [];

        for (var i = 0; i < _state.steps.length; i++) {
            var step = Object.assign({}, _state.steps[i]);
            // Re-read from DOM in case of stale state
            var actorEl = dom.tbody.querySelector('input[data-row="' + i + '"][data-field="actor"]');
            if (actorEl) step.actor = actorEl.value;
            var taskEl = dom.tbody.querySelector('input[data-row="' + i + '"][data-field="task"]');
            if (taskEl) step.task = taskEl.value;
            for (var k = 0; k < NUMERIC_FIELDS.length; k++) {
                var f = NUMERIC_FIELDS[k];
                var numEl = dom.tbody.querySelector('input[data-row="' + i + '"][data-field="' + f + '"]');
                if (numEl) {
                    var raw = numEl.value.trim();
                    step[f] = raw === "" ? null : parseFloat(raw);
                }
            }
            // Skip fully empty steps
            var allNull = NUMERIC_FIELDS.every(function (f) { return step[f] === null || step[f] === undefined || isNaN(step[f]); });
            if (!step.actor && !step.task && allNull) continue;
            steps.push(step);
        }

        return {
            title: (dom.title ? dom.title.value.trim() : ""),
            operator_name: (dom.operator ? dom.operator.value.trim() : ""),
            created_by: (dom.operator ? dom.operator.value.trim() : "unknown"),
            updated_by: (dom.operator ? dom.operator.value.trim() : "unknown"),
            steps: steps,
        };
    }

    function saveRecipe() {
        var payload = collectPayload();
        if (!payload.title) {
            setStatus("Recipe Title is required before saving.", "error");
            if (dom.title) dom.title.focus();
            return;
        }
        if (!payload.operator_name) {
            setStatus("Operator is required before saving.", "error");
            if (dom.operator) dom.operator.focus();
            return;
        }

        var url = _state.recipeId ? "/api/recipes/" + _state.recipeId : "/api/recipes";
        var method = _state.recipeId ? "PATCH" : "POST";

        if (!_state.recipeId) {
            // On create, created_by comes from operator
            payload.created_by = payload.operator_name;
        }
        payload.updated_by = payload.operator_name;

        setSaveState("Saving…", "badge-warning");
        setStatus("");

        var headers = { "Content-Type": "application/json" };
        if (_meta.apiAuthRequired && _meta.recipeWriteToken) {
            headers["X-Recipe-Token"] = _meta.recipeWriteToken;
        }

        fetch(url, {
            method: method,
            headers: headers,
            body: JSON.stringify(payload),
        })
        .then(function (resp) {
            return resp.json().then(function (data) {
                return { ok: resp.ok, status: resp.status, data: data };
            });
        })
        .then(function (result) {
            if (!result.ok) {
                var msg = (result.data && result.data.error) ? result.data.error : "Save failed (HTTP " + result.status + ").";
                setSaveState("Error", "badge-danger");
                setStatus(msg, "error");
                return;
            }
            var saved = result.data;
            _state.recipeId = saved.recipe_id;
            _state.steps = Array.isArray(saved.steps) ? saved.steps.slice() : [];
            updateStatusBadge(saved.status || "draft");
            markSaved();
            setStatus("Saved at " + (saved.updated_at || new Date().toISOString()));

            // Update the recipe selector option
            updateSelectorOption(saved);
            renderTable();
        })
        .catch(function (err) {
            setSaveState("Error", "badge-danger");
            setStatus("Network error: " + err.message, "error");
        });
    }

    function updateSelectorOption(saved) {
        if (!dom.select) return;
        var existing = dom.select.querySelector('option[value="' + saved.recipe_id + '"]');
        var label = saved.title + " | " + (saved.status || "draft") + " | " + (saved.updated_by || saved.created_by || "");
        if (existing) {
            existing.textContent = label;
        } else {
            var opt = document.createElement("option");
            opt.value = saved.recipe_id;
            opt.textContent = label;
            dom.select.appendChild(opt);
        }
        dom.select.value = String(saved.recipe_id);
    }

    // -------------------------------------------------------------------------
    // Recipe selector
    // -------------------------------------------------------------------------

    function onSelectChange() {
        var val = dom.select.value;
        if (val === "") {
            if (_state.dirty && !confirm("Discard unsaved changes?")) {
                dom.select.value = _state.recipeId ? String(_state.recipeId) : "";
                return;
            }
            loadRecipe(null);
            window.history.replaceState(null, "", "/recipes");
            return;
        }

        var id = parseInt(val, 10);
        if (isNaN(id)) return;

        if (_state.dirty && !confirm("Discard unsaved changes?")) {
            dom.select.value = _state.recipeId ? String(_state.recipeId) : "";
            return;
        }

        setSaveState("Loading…", "badge-muted");
        setStatus("");

        fetch("/api/recipes/" + id)
            .then(function (r) { return r.json(); })
            .then(function (data) {
                _state.recipeId = data.recipe_id;
                _state.steps = Array.isArray(data.steps) ? data.steps.slice() : [];
                if (dom.title) dom.title.value = data.title || "";
                if (dom.operator) dom.operator.value = data.operator_name || "";
                updateStatusBadge(data.status || "draft");
                markSaved();
                renderTable();
                setStatus(data.updated_at ? "Last saved: " + data.updated_at : "");
                window.history.replaceState(null, "", "/recipes?recipe_id=" + id);
            })
            .catch(function (err) {
                setSaveState("Error", "badge-danger");
                setStatus("Could not load recipe: " + err.message, "error");
            });
    }

    // -------------------------------------------------------------------------
    // Unsaved changes warning
    // -------------------------------------------------------------------------

    window.addEventListener("beforeunload", function (e) {
        if (_state.dirty) {
            e.preventDefault();
            e.returnValue = "";
        }
    });

    // -------------------------------------------------------------------------
    // Title / operator change tracking
    // -------------------------------------------------------------------------

    function onHeaderInput() {
        markUnsaved();
    }

    // -------------------------------------------------------------------------
    // Init
    // -------------------------------------------------------------------------

    function init() {
        // Wire up header events
        if (dom.title) dom.title.addEventListener("input", onHeaderInput);
        if (dom.operator) dom.operator.addEventListener("input", onHeaderInput);

        // Toolbar
        if (dom.newBtn) {
            dom.newBtn.addEventListener("click", function () {
                if (_state.dirty && !confirm("Discard unsaved changes?")) return;
                if (dom.select) dom.select.value = "";
                loadRecipe(null);
                window.history.replaceState(null, "", "/recipes");
            });
        }
        if (dom.saveBtn) {
            dom.saveBtn.addEventListener("click", saveRecipe);
        }
        if (dom.select) {
            dom.select.addEventListener("change", onSelectChange);
        }

        // Keyboard shortcut: Ctrl+S
        document.addEventListener("keydown", function (e) {
            if ((e.ctrlKey || e.metaKey) && e.key === "s") {
                e.preventDefault();
                saveRecipe();
            }
        });

        // Load initial data
        loadRecipe(_currentData);
    }

    init();

})();
