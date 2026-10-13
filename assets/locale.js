(() => {
  "use strict";

  const supported = new Set(["de", "en", "ru", "uk", "tr", "es", "it"]);
  const locale = document.documentElement.dataset.localePage || "de";

  if (supported.has(locale)) {
    try {
      window.localStorage.setItem("linguoai.locale", locale);
    } catch (_error) {
      // The language links continue to work when storage is unavailable.
    }
  }

  document.querySelectorAll("[data-locale-link]").forEach((link) => {
    const targetLocale = link.dataset.localeLink;
    link.addEventListener("click", () => {
      if (!supported.has(targetLocale)) return;
      try {
        window.localStorage.setItem("linguoai.locale", targetLocale);
      } catch (_error) {
        // Navigation itself does not depend on storage.
      }
    });
  });

  document.addEventListener("click", (event) => {
    document.querySelectorAll("[data-language-switcher][open]").forEach((switcher) => {
      if (!switcher.contains(event.target)) switcher.removeAttribute("open");
    });
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    document.querySelectorAll("[data-language-switcher][open]").forEach((switcher) => {
      switcher.removeAttribute("open");
      switcher.querySelector("summary")?.focus();
    });
  });
})();
