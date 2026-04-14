(function () {
    "use strict";

    const form = document.getElementById("data-export-form");
    const deviceSelect = document.getElementById("data-export-device");
    const rangeSelect = document.getElementById("data-export-range");
    const exportBtn = document.getElementById("data-export-btn");
    const countEl = document.getElementById("data-export-count");
    const channelTable = document.getElementById("data-channel-table");

    if (!form) return;

    // ── Row-count preview ──────────────────────────────────────────────────

    let fetchAbortController = null;
    let fetchTimer = null;

    function scheduleCountUpdate() {
        clearTimeout(fetchTimer);
        fetchTimer = setTimeout(updateCount, 280);
    }

    async function updateCount() {
        if (!countEl) return;

        if (fetchAbortController) {
            fetchAbortController.abort();
        }
        fetchAbortController = new AbortController();

        const params = new URLSearchParams();
        const deviceId = deviceSelect ? deviceSelect.value : "";
        const sinceDays = rangeSelect ? rangeSelect.value : "";
        if (deviceId) params.set("device_id", deviceId);
        if (sinceDays) params.set("since_days", sinceDays);

        countEl.textContent = "…";

        try {
            const url = "/data/count.json" + (params.toString() ? "?" + params.toString() : "");
            const response = await fetch(url, {
                signal: fetchAbortController.signal,
                credentials: "same-origin",
                cache: "no-store",
                headers: { Accept: "application/json" },
            });
            if (!response.ok) throw new Error("HTTP " + response.status);
            const data = await response.json();
            if (data.count == null) {
                countEl.textContent = "n/a";
            } else {
                countEl.textContent = Number(data.count).toLocaleString();
            }
        } catch (err) {
            if (err.name !== "AbortError") {
                countEl.textContent = "n/a";
            }
        }
    }

    if (deviceSelect) deviceSelect.addEventListener("change", scheduleCountUpdate);
    if (rangeSelect) rangeSelect.addEventListener("change", scheduleCountUpdate);

    // ── Channel table: filter highlight ───────────────────────────────────

    function filterChannelTable() {
        if (!channelTable || !deviceSelect) return;
        const selectedDeviceId = deviceSelect.value;
        const rows = channelTable.querySelectorAll("tbody tr[data-device-id]");
        const groupHeaders = channelTable.querySelectorAll("tbody tr.data-device-row");

        if (!selectedDeviceId) {
            // Show all
            rows.forEach(function (r) { r.style.display = ""; });
            groupHeaders.forEach(function (r) { r.style.display = ""; });
            return;
        }

        // Hide rows that don't match, show matching ones
        const visibleGroups = new Set();
        rows.forEach(function (r) {
            const match = r.dataset.deviceId === selectedDeviceId;
            r.style.display = match ? "" : "none";
            if (match) visibleGroups.add(r.closest("tbody").querySelector(
                'tr.data-device-row[data-device-group="' + _groupIndexOf(r) + '"]'
            ));
        });

        // Hide device group headers for non-visible groups
        groupHeaders.forEach(function (header) {
            const groupIdx = header.dataset.deviceGroup;
            const hasVisible = channelTable.querySelector(
                'tbody tr[data-device-id="' + selectedDeviceId + '"]'
            );
            header.style.display = hasVisible ? "" : "none";
        });
    }

    function _groupIndexOf(row) {
        // Walk backwards to find the preceding .data-device-row
        let prev = row.previousElementSibling;
        while (prev) {
            if (prev.classList.contains("data-device-row")) {
                return prev.dataset.deviceGroup;
            }
            prev = prev.previousElementSibling;
        }
        return null;
    }

    if (deviceSelect) deviceSelect.addEventListener("change", filterChannelTable);

    // ── Download button: loading state ────────────────────────────────────

    form.addEventListener("submit", function () {
        if (!exportBtn) return;
        exportBtn.disabled = true;
        exportBtn.textContent = "Preparing…";

        // Re-enable after a few seconds so the user can retry if needed
        setTimeout(function () {
            exportBtn.disabled = false;
            exportBtn.innerHTML =
                '<svg width="16" height="16" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true" style="margin-right:0.4em;flex-shrink:0;">' +
                '<path fill-rule="evenodd" d="M3 17a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zm3.293-7.707a1 1 0 011.414 0L9 10.586V3a1 1 0 112 0v7.586l1.293-1.293a1 1 0 111.414 1.414l-3 3a1 1 0 01-1.414 0l-3-3a1 1 0 010-1.414z" clip-rule="evenodd"/>' +
                "</svg>" +
                "Download ZIP";
        }, 6000);
    });
})();
