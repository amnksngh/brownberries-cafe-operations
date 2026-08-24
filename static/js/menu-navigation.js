(function () {
  "use strict";

  function isSubsequence(needle, haystack) {
    let position = 0;
    for (let index = 0; index < haystack.length && position < needle.length; index += 1) {
      if (haystack[index] === needle[position]) position += 1;
    }
    return position === needle.length;
  }

  function fuzzyMatch(query, blob) {
    if (!query) return true;
    const terms = query.split(/\s+/).filter(Boolean);
    const parts = blob.split(/\s+/).filter(Boolean);
    return terms.every((term) => {
      if (blob.includes(term)) return true;
      return parts.some((part) => {
        const prefix = term.length === 1 ? term : term.slice(0, 2);
        return isSubsequence(term, part) || part.startsWith(prefix);
      });
    });
  }

  function create(options) {
    const root = options.root;
    if (!root) return null;
    const searchInput = options.searchInput || null;
    const clearButton = options.clearButton || null;
    const emptyResults = options.emptyResults || null;
    const itemSelector = options.itemSelector;
    const scrollCue = root.querySelector("[data-menu-scroll-cue]");
    const submenuRows = Array.from(root.querySelectorAll("[data-menu-submenu]"));
    let activeGroup = root.dataset.defaultGroup || "food";
    let activeSection = root.dataset.defaultSection || "starters";
    let scrollCueFrame = null;

    function currentToken() {
      return `${activeGroup}:${activeSection}`;
    }

    function syncScrollCue() {
      scrollCueFrame = null;
      const activeRow = submenuRows.find((row) => row.dataset.menuSubmenu === activeGroup);
      if (!activeRow || !scrollCue) return;
      const remaining = activeRow.scrollWidth - activeRow.clientWidth - activeRow.scrollLeft;
      const visible = remaining > 6;
      scrollCue.classList.toggle("is-visible", visible);
      scrollCue.disabled = !visible;
      scrollCue.setAttribute("aria-hidden", visible ? "false" : "true");
    }

    function requestScrollCueSync() {
      if (scrollCueFrame !== null) window.cancelAnimationFrame(scrollCueFrame);
      scrollCueFrame = window.requestAnimationFrame(syncScrollCue);
    }

    function syncButtons() {
      let activeSectionButton = null;
      root.querySelectorAll("[data-menu-group]").forEach((button) => {
        const active = button.dataset.menuGroup === activeGroup;
        button.classList.toggle("active", active);
        button.setAttribute("aria-selected", active ? "true" : "false");
      });
      root.querySelectorAll("[data-menu-submenu]").forEach((row) => {
        const active = row.dataset.menuSubmenu === activeGroup;
        row.classList.toggle("active", active);
        row.hidden = !active;
      });
      root.querySelectorAll("[data-menu-section]").forEach((button) => {
        const active = button.dataset.menuSectionGroup === activeGroup
          && button.dataset.menuSection === activeSection;
        button.classList.toggle("active", active);
        button.setAttribute("aria-pressed", active ? "true" : "false");
        if (active) activeSectionButton = button;
      });
      const activeRow = submenuRows.find((row) => row.dataset.menuSubmenu === activeGroup);
      if (activeRow && activeSectionButton) {
        const targetLeft = Math.max(0, activeSectionButton.offsetLeft - activeRow.offsetLeft - 12);
        activeRow.scrollTo({ left: targetLeft, behavior: "auto" });
      }
      requestScrollCueSync();
    }

    function syncSearchClear() {
      clearButton?.classList.toggle("hidden", !(searchInput?.value || "").trim());
    }

    function applyFilters() {
      const query = (searchInput?.value || "").trim().toLowerCase();
      const token = currentToken();
      let visibleCount = 0;
      document.querySelectorAll(itemSelector).forEach((card) => {
        const tokens = (card.dataset.menuNav || "").split("|").filter(Boolean);
        const categories = (card.dataset.cats || "").split("|").filter(Boolean);
        const blob = `${card.dataset.name || ""} ${card.dataset.short || ""} ${card.dataset.type || ""} ${categories.join(" ")}`;
        const show = tokens.includes(token) && fuzzyMatch(query, blob);
        card.style.display = show ? "" : "none";
        if (show) visibleCount += 1;
      });
      emptyResults?.classList.toggle("hidden", visibleCount > 0);
    }

    root.addEventListener("click", (event) => {
      const groupButton = event.target.closest("[data-menu-group]");
      if (groupButton) {
        activeGroup = groupButton.dataset.menuGroup;
        activeSection = groupButton.dataset.defaultSection;
        syncButtons();
        applyFilters();
        return;
      }
      const sectionButton = event.target.closest("[data-menu-section]");
      if (!sectionButton) return;
      activeGroup = sectionButton.dataset.menuSectionGroup;
      activeSection = sectionButton.dataset.menuSection;
      syncButtons();
      applyFilters();
    });

    scrollCue?.addEventListener("click", () => {
      const activeRow = submenuRows.find((row) => row.dataset.menuSubmenu === activeGroup);
      if (!activeRow) return;
      activeRow.scrollTo({ left: activeRow.scrollWidth, behavior: "smooth" });
      requestScrollCueSync();
    });

    searchInput?.addEventListener("input", () => {
      syncSearchClear();
      applyFilters();
    });
    clearButton?.addEventListener("click", () => {
      searchInput.value = "";
      syncSearchClear();
      applyFilters();
      searchInput.focus();
    });
    submenuRows.forEach((row) => row.addEventListener("scroll", requestScrollCueSync, { passive: true }));
    window.addEventListener("resize", requestScrollCueSync, { passive: true });
    if (window.ResizeObserver) {
      const observer = new ResizeObserver(requestScrollCueSync);
      observer.observe(root);
      submenuRows.forEach((row) => observer.observe(row));
    }

    syncButtons();
    syncSearchClear();
    applyFilters();
    return { applyFilters, syncSearchClear };
  }

  window.BrownberriesMenuNavigation = { create };
}());
