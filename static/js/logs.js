(function () {
    "use strict";

    const root = document.getElementById("logs-root");
    const tbody = document.getElementById("logs-table-body");
    const emptyState = document.getElementById("logs-empty");
    const liveStatus = document.getElementById("logs-live-status");
    const totalEl = document.getElementById("logs-total");
    const errorsEl = document.getElementById("logs-errors");
    const warningsEl = document.getElementById("logs-warnings");
    const retentionEl = document.getElementById("logs-retention");

    if (!root || !tbody) return;

    const pollIntervalMs = 2500;
    const hiddenPollIntervalMs = 10000;
    const requestTimeoutMs = 8000;
    const retentionDays = clampInt(root.dataset.retentionDays, 1, 365, 7);
    const limit = clampInt(root.dataset.limit, 1, 300, 120);
    let knownKeys = collectCurrentKeys();
    let firstRefresh = true;
    let refreshTimer = null;
    let activeController = null;

    function clampInt(value, min, max, fallback) {
        const parsed = Number.parseInt(value, 10);
        if (!Number.isFinite(parsed)) return fallback;
        return Math.min(max, Math.max(min, parsed));
    }

    function collectCurrentKeys() {
        return new Set(
            Array.from(tbody.querySelectorAll("tr[data-log-key]"))
                .map((row) => row.dataset.logKey)
                .filter(Boolean)
        );
    }

    function escapeHtml(value) {
        return String(value == null ? "" : value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function normalizeSeverity(value) {
        const severity = String(value || "info").trim().toLowerCase();
        if (["info", "success", "warning", "error"].includes(severity)) return severity;
        return "info";
    }

    function badgeClass(value) {
        const severity = normalizeSeverity(value);
        if (severity === "success") return "badge-success";
        if (severity === "warning") return "badge-warning";
        if (severity === "error") return "badge-danger";
        return "badge-info";
    }

    function formatTimestamp(value) {
        if (!value) return "-";
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return String(value);
        return new Intl.DateTimeFormat(undefined, {
            year: "numeric",
            month: "2-digit",
            day: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
        }).format(date);
    }

    function statusLine(item) {
        if (!item.status) return "";
        return '<div class="muted">' + escapeHtml(item.status) + "</div>";
    }

    function detailsCell(item) {
        if (!item.details) return '<span class="muted">-</span>';
        return "<code>" + escapeHtml(item.details) + "</code>";
    }

    function eventKeyFor(item) {
        return item.event_key || [
            item.source,
            item.timestamp,
            item.category,
            item.title,
            item.message,
        ].join(":");
    }

    function rowHtml(item, isNew) {
        const severity = normalizeSeverity(item.severity);
        const eventKey = eventKeyFor(item);

        return (
            '<tr class="log-row log-row-' + severity + (isNew ? ' is-new' : '') + '" data-log-key="' + escapeHtml(eventKey) + '">' +
            "<td>" + escapeHtml(formatTimestamp(item.timestamp)) + "</td>" +
            '<td><span class="badge ' + badgeClass(severity) + '">' + escapeHtml(severity) + "</span></td>" +
            "<td>" + escapeHtml(item.category || "-") + "</td>" +
            "<td>" + escapeHtml(item.actor || "-") + "</td>" +
            "<td><strong>" + escapeHtml(item.title || "-") + "</strong>" + statusLine(item) + "</td>" +
            "<td>" + escapeHtml(item.message || "-") + "</td>" +
            "<td>" + detailsCell(item) + "</td>" +
            "</tr>"
        );
    }

    function updateSummary(summary, retentionDaysValue) {
        if (totalEl) totalEl.textContent = Number(summary && summary.total ? summary.total : 0).toLocaleString();
        if (errorsEl) errorsEl.textContent = Number(summary && summary.errors ? summary.errors : 0).toLocaleString();
        if (warningsEl) warningsEl.textContent = Number(summary && summary.warnings ? summary.warnings : 0).toLocaleString();
        if (retentionEl && retentionDaysValue) retentionEl.textContent = String(retentionDaysValue) + "d";
    }

    function setLiveStatus(message, tone) {
        if (!liveStatus) return;
        liveStatus.textContent = message;
        liveStatus.classList.toggle("is-error", tone === "error");
        liveStatus.classList.toggle("is-ok", tone === "ok");
    }

    function renderItems(items) {
        const nextKeys = new Set();
        let newCount = 0;
        const rows = items.map((item) => {
            const eventKey = String(eventKeyFor(item) || "");
            const isNew = Boolean(eventKey && !knownKeys.has(eventKey) && !firstRefresh);
            if (eventKey) nextKeys.add(eventKey);
            if (isNew) newCount += 1;
            return rowHtml(item, isNew);
        });

        tbody.innerHTML = rows.join("");
        if (emptyState) emptyState.classList.toggle("is-hidden", items.length > 0);
        knownKeys = nextKeys;
        firstRefresh = false;
        return newCount;
    }

    function latestStatusText(newCount) {
        const now = new Date();
        const time = new Intl.DateTimeFormat(undefined, {
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
        }).format(now);
        if (newCount > 0) {
            return newCount + " new event" + (newCount === 1 ? "" : "s") + " - updated " + time;
        }
        return "Live update active - updated " + time;
    }

    async function refreshLogs() {
        if (activeController) {
            activeController.abort();
        }

        const controller = new AbortController();
        activeController = controller;
        const timeout = window.setTimeout(() => controller.abort(), requestTimeoutMs);
        const params = new URLSearchParams({ days: String(retentionDays), limit: String(limit) });

        try {
            const response = await fetch("/api/logs?" + params.toString(), {
                signal: controller.signal,
                credentials: "same-origin",
                cache: "no-store",
                headers: { Accept: "application/json" },
            });
            if (!response.ok) throw new Error("HTTP " + response.status);

            const payload = await response.json();
            const items = Array.isArray(payload.items) ? payload.items : [];
            const newCount = renderItems(items);
            updateSummary(payload.summary || {}, payload.retention_days || retentionDays);
            setLiveStatus(latestStatusText(newCount), "ok");
        } catch (err) {
            if (err.name !== "AbortError") {
                setLiveStatus("Live update delayed - " + err.message, "error");
            }
        } finally {
            window.clearTimeout(timeout);
            if (activeController === controller) {
                activeController = null;
                scheduleRefresh();
            }
        }
    }

    function scheduleRefresh(delay) {
        window.clearTimeout(refreshTimer);
        const nextDelay = delay == null
            ? (document.hidden ? hiddenPollIntervalMs : pollIntervalMs)
            : delay;
        refreshTimer = window.setTimeout(refreshLogs, nextDelay);
    }

    document.addEventListener("visibilitychange", function () {
        if (!document.hidden) scheduleRefresh(0);
    });

    setLiveStatus("Live update active", "ok");
    scheduleRefresh(500);
})();
