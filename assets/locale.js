(() => {
  "use strict";

  const supported = new Set(["de", "en", "ru", "uk", "tr", "es", "it"]);
  const locale = document.documentElement.dataset.localePage || "de";

  // Carry an explicit German choice through guides even if storage is blocked.
  if (locale === "de" && new URLSearchParams(window.location.search).get("lang") === "de") {
    document.querySelectorAll("a[href]:not([data-locale-link])").forEach((link) => {
      if (link.getAttribute("href").startsWith("#")) return;
      const target = new URL(link.href, window.location.href);
      if (target.origin !== window.location.origin) return;
      if (!["/", "/index.html", "/colab.html", "/anleitung.html"].includes(target.pathname)) return;
      target.searchParams.set("lang", "de");
      link.href = target.href;
    });
  }

  if (supported.has(locale)) {
    try {
      window.localStorage.setItem("linguoai.locale", locale);
    } catch (_error) {
      // The language links continue to work when storage is unavailable.
    }
  }

  document.querySelectorAll("[data-locale-link]").forEach((link) => {
    const targetLocale = link.dataset.localeLink;
    const syncAnchor = () => {
      const target = new URL(link.href, window.location.href);
      target.searchParams.set("lang", targetLocale);
      target.hash = window.location.hash;
      link.href = target.href;
    };
    syncAnchor();
    window.addEventListener("hashchange", syncAnchor);
    // replaceState (the version selector) does not dispatch hashchange.
    document.addEventListener("change", syncAnchor);
    link.addEventListener("click", () => {
      syncAnchor();
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
