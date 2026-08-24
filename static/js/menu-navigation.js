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
    let activeGroup = root.dataset.defaultGroup || "food";
    let activeSection = root.dataset.defaultSection || "starters";

    function currentToken() {
      return `${activeGroup}:${activeSection}`;
    }

    function syncButtons() {
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
      });
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

    syncButtons();
    syncSearchClear();
    applyFilters();
    return { applyFilters, syncSearchClear };
  }

  window.BrownberriesMenuNavigation = { create };
}());
