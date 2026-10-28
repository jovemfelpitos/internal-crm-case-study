(function () {
  const storageKey = "crm-theme";
  const root = document.documentElement;

  function currentTheme() {
    return root.dataset.theme === "dark" ? "dark" : "light";
  }

  function applyTheme(theme) {
    root.dataset.theme = theme;
    root.dataset.bsTheme = theme;
    localStorage.setItem(storageKey, theme);
    document.querySelectorAll(".js-theme-toggle").forEach((button) => {
      button.setAttribute("aria-pressed", theme === "dark" ? "true" : "false");
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    applyTheme(currentTheme());

    const scrollStoragePrefix = "crm-scroll:";
    const scrollKey = `${scrollStoragePrefix}${window.location.pathname}${window.location.search}`;
    const scrollTargets = [
      window,
      ...document.querySelectorAll(".workflow-board, .support-board, .table-responsive"),
    ];

    function targetKey(target, index) {
      if (target === window) {
        return "window";
      }
      return target.id || target.dataset.scrollKey || `${target.className || "scroll"}:${index}`;
    }

    function readScroll(target) {
      if (target === window) {
        return { x: window.scrollX || 0, y: window.scrollY || 0 };
      }
      return { x: target.scrollLeft || 0, y: target.scrollTop || 0 };
    }

    function writeScroll(target, position) {
      if (!position) return;
      if (target === window) {
        window.scrollTo(position.x || 0, position.y || 0);
        return;
      }
      target.scrollLeft = position.x || 0;
      target.scrollTop = position.y || 0;
    }

    function saveScrollState() {
      const payload = {};
      scrollTargets.forEach((target, index) => {
        payload[targetKey(target, index)] = readScroll(target);
      });
      sessionStorage.setItem(scrollKey, JSON.stringify(payload));
    }

    try {
      if ("scrollRestoration" in history) {
        history.scrollRestoration = "manual";
      }
      const savedScroll = JSON.parse(sessionStorage.getItem(scrollKey) || "{}");
      requestAnimationFrame(() => {
        scrollTargets.forEach((target, index) => {
          writeScroll(target, savedScroll[targetKey(target, index)]);
        });
      });
      window.addEventListener("pagehide", saveScrollState);
      window.addEventListener("beforeunload", saveScrollState);
    } catch (_error) {
      // Scroll persistence is progressive enhancement only.
    }

    document.querySelectorAll(".js-theme-toggle").forEach((button) => {
      button.addEventListener("click", function () {
        applyTheme(currentTheme() === "dark" ? "light" : "dark");
      });
    });

    document.querySelectorAll("[data-multi-filter]").forEach((filter) => {
      const search = filter.querySelector("[data-multi-filter-search]");
      const options = Array.from(filter.querySelectorAll("[data-multi-filter-option]"));
      const checkboxes = Array.from(filter.querySelectorAll("input[type='checkbox']"));
      const valueLabel = filter.querySelector("[data-multi-filter-value]");
      const clearButton = filter.querySelector("[data-multi-filter-clear]");

      function refreshLabel() {
        if (!valueLabel) return;
        const selectedCount = checkboxes.filter((checkbox) => checkbox.checked).length;
        if (!selectedCount) {
          valueLabel.textContent = filter.dataset.emptyLabel || "Todos";
          return;
        }

        const noun = selectedCount === 1
          ? filter.dataset.singular || "selecionado"
          : filter.dataset.plural || "selecionados";
        valueLabel.textContent = `${selectedCount} ${noun}`;
      }

      function applySearch() {
        const term = (search?.value || "").trim().toLowerCase();
        options.forEach((option) => {
          const text = option.textContent.trim().toLowerCase();
          option.classList.toggle("is-hidden", Boolean(term) && !text.includes(term));
        });
      }

      checkboxes.forEach((checkbox) => {
        checkbox.addEventListener("change", refreshLabel);
      });

      search?.addEventListener("input", applySearch);
      search?.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
        }
      });

      clearButton?.addEventListener("click", () => {
        checkboxes.forEach((checkbox) => {
          checkbox.checked = false;
        });
        if (search) {
          search.value = "";
        }
        applySearch();
        refreshLabel();
      });

      refreshLabel();
    });

    document.addEventListener("click", (event) => {
      document.querySelectorAll("[data-multi-filter][open]").forEach((filter) => {
        if (!filter.contains(event.target)) {
          filter.removeAttribute("open");
        }
      });
    });
  });
})();
