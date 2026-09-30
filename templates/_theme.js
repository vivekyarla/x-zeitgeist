try { var p = JSON.parse(localStorage.getItem("tl_prefs") || "{}"); if (p.theme === "light" || p.theme === "dark") document.documentElement.dataset.theme = p.theme; } catch (e) {}
