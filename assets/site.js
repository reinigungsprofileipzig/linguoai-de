(() => {
  "use strict";

  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const header = document.querySelector("[data-header]");
  const progressBar = document.querySelector(".scroll-progress span");
  const menuButton = document.querySelector("[data-menu-button]");
  const menu = document.querySelector("[data-menu]");
  const toast = document.querySelector("[data-toast]");
  let toastTimer = 0;
  let scrollFrame = 0;

  document.querySelectorAll("[data-current-year]").forEach((node) => {
    node.textContent = String(new Date().getFullYear());
  });

  const updateScrollUi = () => {
    scrollFrame = 0;
    const scrollTop = window.scrollY || document.documentElement.scrollTop;
    const scrollable = document.documentElement.scrollHeight - window.innerHeight;
    const ratio = scrollable > 0 ? Math.min(1, Math.max(0, scrollTop / scrollable)) : 0;

    header?.classList.toggle("is-scrolled", scrollTop > 18);
    if (progressBar) {
      progressBar.style.transform = `scaleX(${ratio})`;
    }
  };

  const requestScrollUpdate = () => {
    if (!scrollFrame) {
      scrollFrame = window.requestAnimationFrame(updateScrollUi);
    }
  };

  window.addEventListener("scroll", requestScrollUpdate, { passive: true });
  window.addEventListener("resize", requestScrollUpdate, { passive: true });
  updateScrollUi();

  const closeMenu = ({ returnFocus = false } = {}) => {
    if (!menuButton || !menu) return;
    menuButton.setAttribute("aria-expanded", "false");
    menuButton.querySelector(".sr-only").textContent = "Navigation öffnen";
    menu.classList.remove("is-open");
    document.body.classList.remove("menu-open");
    if (returnFocus) menuButton.focus();
  };

  menuButton?.addEventListener("click", () => {
    const open = menuButton.getAttribute("aria-expanded") === "true";
    if (open) {
      closeMenu();
      return;
    }
    menuButton.setAttribute("aria-expanded", "true");
    menuButton.querySelector(".sr-only").textContent = "Navigation schließen";
    menu?.classList.add("is-open");
    document.body.classList.add("menu-open");
  });

  menu?.querySelectorAll("a").forEach((link) => {
    link.addEventListener("click", () => closeMenu());
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && menu?.classList.contains("is-open")) {
      closeMenu({ returnFocus: true });
    }
  });

  window.matchMedia("(min-width: 1051px)").addEventListener("change", (event) => {
    if (event.matches) closeMenu();
  });

  const revealNodes = [...document.querySelectorAll(".reveal, .hardware-map")];
  if (reduceMotion || !("IntersectionObserver" in window)) {
    revealNodes.forEach((node) => node.classList.add("is-visible"));
  } else {
    document.documentElement.classList.add("reveal-ready");
    const revealObserver = new IntersectionObserver(
      (entries, observer) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          entry.target.classList.add("is-visible");
          observer.unobserve(entry.target);
        });
      },
      { rootMargin: "0px 0px -8%", threshold: 0.12 },
    );
    revealNodes.forEach((node) => revealObserver.observe(node));
  }

  const tabs = [...document.querySelectorAll("[data-tab]")];
  const panels = [...document.querySelectorAll("[data-panel]")];

  const activateTab = (tab, { focus = false } = {}) => {
    const target = tab.dataset.tab;
    tabs.forEach((item) => {
      const active = item === tab;
      item.classList.toggle("is-active", active);
      item.setAttribute("aria-selected", String(active));
      item.tabIndex = active ? 0 : -1;
    });
    panels.forEach((panel) => {
      const active = panel.dataset.panel === target;
      panel.classList.toggle("is-active", active);
      panel.toggleAttribute("hidden", !active);
    });
    if (focus) tab.focus();
  };

  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => activateTab(tab));
    tab.addEventListener("keydown", (event) => {
      let nextIndex = index;
      if (event.key === "ArrowRight" || event.key === "ArrowDown") {
        nextIndex = (index + 1) % tabs.length;
      } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
        nextIndex = (index - 1 + tabs.length) % tabs.length;
      } else if (event.key === "Home") {
        nextIndex = 0;
      } else if (event.key === "End") {
        nextIndex = tabs.length - 1;
      } else {
        return;
      }
      event.preventDefault();
      activateTab(tabs[nextIndex], { focus: true });
    });
  });

  const selectedTab = tabs.find((tab) => tab.classList.contains("is-active")) ?? tabs[0];
  if (selectedTab) activateTab(selectedTab);

  const prepareHashTarget = (identifier) => {
    const panelRoute = {
      colab: "colab",
      "panel-desktop": "desktop",
      "panel-cli": "cli",
    }[identifier];
    if (panelRoute) {
      const routeTab = tabs.find((tab) => tab.dataset.tab === panelRoute);
      if (routeTab) activateTab(routeTab);
    }
    return identifier ? document.getElementById(identifier) : null;
  };

  const scrollToTarget = (target) => {
    const previousBehavior = document.documentElement.style.scrollBehavior;
    document.documentElement.style.scrollBehavior = "auto";
    target.scrollIntoView({ block: "start", behavior: "auto" });
    window.requestAnimationFrame(() => {
      document.documentElement.style.scrollBehavior = previousBehavior;
    });
  };

  window.addEventListener(
    "load",
    () => {
      const identifier = decodeURIComponent(window.location.hash.slice(1));
      const target = prepareHashTarget(identifier);
      if (!target) return;
      window.requestAnimationFrame(() => {
        scrollToTarget(target);
      });
    },
    { once: true },
  );

  document.querySelectorAll('a[href="#colab"], a[href="#panel-desktop"], a[href="#panel-cli"]').forEach((link) => {
    link.addEventListener("click", (event) => {
      const identifier = link.getAttribute("href").slice(1);
      const target = prepareHashTarget(identifier);
      if (!target) return;
      event.preventDefault();
      window.history.pushState(null, "", `#${identifier}`);
      scrollToTarget(target);
      closeMenu();
    });
  });

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
    const copied = document.execCommand("copy");
    textarea.remove();
    return copied;
  };

  document.querySelectorAll("[data-copy]").forEach((button) => {
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
        copied = fallbackCopy(text);
      }

      const label = button.querySelector("[data-copy-label]");
      const oldLabel = label?.textContent;
      if (label) label.textContent = copied ? "Kopiert" : "Bitte markieren";
      showToast(copied ? "In die Zwischenablage kopiert." : "Kopieren war nicht möglich.");
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
})();
