(function () {
    var STORAGE_KEY = "reactor_ctrl.theme.v1";
    var root = document.documentElement;
    var toggles = Array.prototype.slice.call(document.querySelectorAll("[data-theme-toggle]"));
    var systemThemeQuery = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

    function getStoredTheme() {
        try {
            return window.localStorage.getItem(STORAGE_KEY);
        } catch (error) {
            return null;
        }
    }

    function persistTheme(theme) {
        try {
            window.localStorage.setItem(STORAGE_KEY, theme);
        } catch (error) {
            return;
        }
    }

    function resolveTheme() {
        var storedTheme = getStoredTheme();
        if (storedTheme === "dark" || storedTheme === "light") {
            return storedTheme;
        }
        return systemThemeQuery && systemThemeQuery.matches ? "dark" : "light";
    }

    function syncToggles(theme) {
        toggles.forEach(function (toggle) {
            var isDark = theme === "dark";
            toggle.checked = isDark;
            toggle.setAttribute("aria-checked", isDark ? "true" : "false");
            toggle.title = isDark ? "Switch to light mode" : "Switch to dark mode";
        });
    }

    function applyTheme(theme, persistSelection) {
        var previousTheme = root.dataset.theme;
        root.dataset.theme = theme;
        root.style.colorScheme = theme;
        syncToggles(theme);
        if (persistSelection) {
            persistTheme(theme);
        }
        if (previousTheme !== theme) {
            window.dispatchEvent(new CustomEvent("reactor:themechange", {
                detail: { theme: theme },
            }));
        }
    }

    toggles.forEach(function (toggle) {
        toggle.addEventListener("change", function (event) {
            applyTheme(event.currentTarget.checked ? "dark" : "light", true);
        });
    });

    if (systemThemeQuery) {
        var handleSystemThemeChange = function (event) {
            var storedTheme = getStoredTheme();
            if (storedTheme === "dark" || storedTheme === "light") {
                return;
            }
            applyTheme(event.matches ? "dark" : "light", false);
        };

        if (typeof systemThemeQuery.addEventListener === "function") {
            systemThemeQuery.addEventListener("change", handleSystemThemeChange);
        } else if (typeof systemThemeQuery.addListener === "function") {
            systemThemeQuery.addListener(handleSystemThemeChange);
        }
    }

    applyTheme(resolveTheme(), false);
})();
