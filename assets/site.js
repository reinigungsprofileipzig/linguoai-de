(() => {
  "use strict";
  document.documentElement.classList.replace("no-js", "has-js");

  // Theme preference, shared by every public page.
  const themeToggles = [...document.querySelectorAll("[data-theme-toggle]")];
  const applyTheme = (theme) => {
    document.documentElement.setAttribute("data-theme", theme);
    const metaThemeColor = document.querySelector('meta[name="theme-color"]');
    if (metaThemeColor) {
      metaThemeColor.setAttribute("content", theme === "light" ? "#f6f7f9" : "#111a23");
    }
    try {
      localStorage.setItem("linguoai-theme", theme);
    } catch (_error) {}
    themeToggles.forEach((button) => {
      button.setAttribute("aria-pressed", theme === "dark" ? "true" : "false");
    });
  };

  if (themeToggles.length) {
    let currentTheme = document.documentElement.getAttribute("data-theme") || "light";
    try {
      const stored = localStorage.getItem("linguoai-theme");
      if (stored === "light" || stored === "dark") {
        currentTheme = stored;
      }
    } catch (_error) {}
    applyTheme(currentTheme);

    themeToggles.forEach((button) => {
      button.addEventListener("click", () => {
        const next = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
        applyTheme(next);
      });
    });
  }

  // The comparison remains usable when JavaScript is unavailable.
  const finder = document.querySelector("[data-version-finder]");
  if (finder) {
    const controls = finder.querySelector("[data-version-controls]");
    const choices = [...controls.querySelectorAll('input[name="version-need"]')];
    const cards = [...finder.querySelectorAll("[data-version]")];
    const grid = finder.querySelector(".version-grid");
    const result = finder.querySelector("[data-version-result]");
    result.classList.add("sr-only");

    const showVersion = (value, announce = true) => {
      const selected = cards.find((card) => card.dataset.version === value);
      cards.forEach((card) => {
        card.hidden = Boolean(selected && card !== selected);
        card.classList.toggle("is-selected", card === selected);
      });
      grid.classList.toggle("has-selection", Boolean(selected));
      choices.forEach((choice) => { choice.checked = choice.value === (selected ? value : "all"); });
      if (announce) {
        result.textContent = selected
          ? `${selected.querySelector("h3").textContent}. ${selected.querySelector("[data-version-description]").textContent}`
          : choices.find((choice) => choice.value === "all").nextElementSibling.textContent;
      }
    };

    const readHash = (event) => {
      const card = cards.find((item) => `#${item.id}` === window.location.hash);
      showVersion(card?.dataset.version || "all", false);
      // Filtering and font loading change positions before native fragment scrolling.
      if (card && !event?.persisted) {
        const alignFinder = () => finder.scrollIntoView({ block: "start", behavior: "instant" });
        if (document.fonts?.ready) document.fonts.ready.then(alignFinder);
        else window.requestAnimationFrame(alignFinder);
      }
    };
    controls.addEventListener("change", (event) => {
      if (!choices.includes(event.target)) return;
      const value = event.target.value;
      showVersion(value);
      try {
        const hash = value === "all" ? "#versionen" : `#version-${value}`;
        window.history.replaceState(null, "", hash);
      } catch (_error) {
        // Selection still works when browser history access is restricted.
      }
    });
    window.addEventListener("hashchange", readHash);
    window.addEventListener("pageshow", readHash);
    readHash();
    controls.hidden = false;
  }

  const locale = document.documentElement.lang.split("-")[0].toLowerCase();
  const uiText = {
    de: {
      menuOpen: "Navigation öffnen",
      menuClose: "Navigation schließen",
      copied: "Kopiert",
      select: "Bitte markieren",
      copiedToast: "In die Zwischenablage kopiert.",
      copyFailed: "Kopieren war nicht möglich.",
    },
    en: {
      menuOpen: "Open navigation",
      menuClose: "Close navigation",
      copied: "Copied",
      select: "Please select manually",
      copiedToast: "Copied to the clipboard.",
      copyFailed: "Could not copy the text.",
    },
    ru: {
      menuOpen: "Открыть навигацию",
      menuClose: "Закрыть навигацию",
      copied: "Скопировано",
      select: "Выделите вручную",
      copiedToast: "Скопировано в буфер обмена.",
      copyFailed: "Не удалось скопировать текст.",
    },
    uk: {
      menuOpen: "Відкрити навігацію",
      menuClose: "Закрити навігацію",
      copied: "Скопійовано",
      select: "Виділіть вручну",
      copiedToast: "Скопійовано в буфер обміну.",
      copyFailed: "Не вдалося скопіювати текст.",
    },
    tr: {
      menuOpen: "Navigasyonu aç",
      menuClose: "Navigasyonu kapat",
      copied: "Kopyalandı",
      select: "Lütfen elle seçin",
      copiedToast: "Panoya kopyalandı.",
      copyFailed: "Metin kopyalanamadı.",
    },
    es: {
      menuOpen: "Abrir navegación",
      menuClose: "Cerrar navegación",
      copied: "Copiado",
      select: "Selecciona el texto manualmente",
      copiedToast: "Copiado al portapapeles.",
      copyFailed: "No se pudo copiar el texto.",
    },
    it: {
      menuOpen: "Apri navigazione",
      menuClose: "Chiudi navigazione",
      copied: "Copiato",
      select: "Seleziona il testo manualmente",
      copiedToast: "Copiato negli appunti.",
      copyFailed: "Impossibile copiare il testo.",
    },
  }[locale] ?? {
    menuOpen: "Open navigation",
    menuClose: "Close navigation",
    copied: "Copied",
    select: "Please select manually",
    copiedToast: "Copied to the clipboard.",
    copyFailed: "Could not copy the text.",
  };

  const header = document.querySelector("[data-header]");
  const menuButton = document.querySelector("[data-menu-button]");
  const menu = document.querySelector("[data-menu]");
  const mobileViewport = window.matchMedia("(max-width: 1050px)");
  const copyButtons = [...document.querySelectorAll("[data-copy]")];
  let toast = document.querySelector("[data-toast]");
  let toastTimer = 0;
  let scrollFrame = 0;

  if (!toast && copyButtons.length) {
    toast = document.createElement("div");
    toast.className = "toast";
    toast.dataset.toast = "";
    toast.setAttribute("role", "status");
    toast.setAttribute("aria-live", "polite");
    toast.setAttribute("aria-atomic", "true");
    document.body.append(toast);
  }

  document.querySelectorAll("[data-current-year]").forEach((node) => {
    node.textContent = String(new Date().getFullYear());
  });

  const updateScrollUi = () => {
    scrollFrame = 0;
    header?.classList.toggle("is-scrolled", window.scrollY > 18);
  };

  const requestScrollUpdate = () => {
    if (!scrollFrame) {
      scrollFrame = window.requestAnimationFrame(updateScrollUi);
    }
  };

  if (header) {
    window.addEventListener("scroll", requestScrollUpdate, { passive: true });
    updateScrollUi();
  }

  const updateMenuAccessibility = () => {
    if (!menu || !menuButton) return;
    const hidden = mobileViewport.matches && menuButton.getAttribute("aria-expanded") !== "true";
    if (hidden && menu.contains(document.activeElement)) menuButton.focus();
    menu.inert = hidden;
    menu.toggleAttribute("inert", hidden);
    if (hidden) menu.setAttribute("aria-hidden", "true");
    else menu.removeAttribute("aria-hidden");
  };

  const closeMenu = ({ returnFocus = false } = {}) => {
    if (!menuButton || !menu) return;
    menuButton.setAttribute("aria-expanded", "false");
    const label = menuButton.querySelector(".sr-only");
    if (label) label.textContent = uiText.menuOpen;
    menu.classList.remove("is-open");
    document.body.classList.remove("menu-open");
    updateMenuAccessibility();
    if (returnFocus) menuButton.focus();
  };

  menuButton?.addEventListener("click", () => {
    const open = menuButton.getAttribute("aria-expanded") === "true";
    if (open) {
      closeMenu();
      return;
    }
    menuButton.setAttribute("aria-expanded", "true");
    const label = menuButton.querySelector(".sr-only");
    if (label) label.textContent = uiText.menuClose;
    menu?.classList.add("is-open");
    document.body.classList.add("menu-open");
    updateMenuAccessibility();
  });

  menu?.querySelectorAll("a").forEach((link) => {
    link.addEventListener("click", () => closeMenu());
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && menu?.classList.contains("is-open")) {
      closeMenu({ returnFocus: true });
    }
  });

  document.addEventListener("click", (event) => {
    if (menu?.classList.contains("is-open") && !menu.contains(event.target) && !menuButton?.contains(event.target)) {
      closeMenu();
    }
  });

  mobileViewport.addEventListener("change", () => closeMenu());
  closeMenu();

  const showToast = (message) => {
    if (!toast) return;
    window.clearTimeout(toastTimer);
    toast.textContent = message;
    toast.classList.add("is-visible");
    toastTimer = window.setTimeout(() => toast.classList.remove("is-visible"), 2200);
  };

  const fallbackCopy = (text) => {
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    document.body.append(textarea);
    textarea.select();
    try {
      return document.execCommand("copy");
    } finally {
      textarea.remove();
    }
  };

  copyButtons.forEach((button) => {
    button.addEventListener("click", async () => {
      const source = document.getElementById(button.dataset.copy);
      if (!source) return;
      const text = source.textContent.trim();
      let copied = false;

      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(text);
          copied = true;
        } else {
          copied = fallbackCopy(text);
        }
      } catch (_error) {
        try {
          copied = fallbackCopy(text);
        } catch (_fallbackError) {
          copied = false;
        }
      }

      const label = button.querySelector("[data-copy-label]");
      const oldLabel = label?.textContent;
      if (label) label.textContent = copied ? uiText.copied : uiText.select;
      showToast(copied ? uiText.copiedToast : uiText.copyFailed);
      window.setTimeout(() => {
        if (label && oldLabel) label.textContent = oldLabel;
      }, 1800);
    });
  });

  document.querySelectorAll(".faq-list details").forEach((details) => {
    details.addEventListener("toggle", () => {
      if (!details.open) return;
      document.querySelectorAll(".faq-list details[open]").forEach((other) => {
        if (other !== details) other.removeAttribute("open");
      });
    });
  });

  // An illustrative text example; it makes no audio or translation API request.
  const sample = document.querySelector('[data-translation-preview]');
  if (sample) {
    const samples = {
      en: {name: 'English', text: "It's good to have you here."},
      es: {name: 'Español', text: 'Qué bien que estés aquí.'},
      fr: {name: 'Français', text: 'Ça fait plaisir de te voir.'},
    };
    const label = sample.querySelector('[data-sample-label]');
    const prefix = label.textContent.split(' · ')[0];
    const text = sample.querySelector('[data-sample-text]');
    const code = sample.querySelector('[data-sample-code]');
    const result = sample.querySelector('[data-sample-result]');
    const controls = sample.querySelector('.sample-languages');
    const updateSample = (lang, announce = true) => {
      const selected = samples[lang];
      if (!selected) return;
      label.textContent = prefix + ' · ' + selected.name;
      text.textContent = selected.text;
      text.lang = lang;
      code.textContent = lang.toUpperCase();
      if (announce) { result.lang = lang; result.textContent = selected.text; }
    };
    controls.addEventListener('change', (event) => {
      if (event.target.matches('input[name="sample-language"]')) updateSample(event.target.value);
    });
    window.addEventListener('pageshow', () => updateSample(controls.querySelector('input:checked').value, false));
    controls.hidden = false;
  }
})();
