const CART_KEY = "crossword_cart";

function getGuestCart() {
  try {
    const raw = localStorage.getItem(CART_KEY);
    const parsed = raw ? JSON.parse(raw) : { items: [] };
    if (!parsed.items) parsed.items = [];
    return parsed;
  } catch {
    return { items: [] };
  }
}

function setGuestCart(cart) {
  localStorage.setItem(CART_KEY, JSON.stringify(cart));
}

function guestCartCount() {
  return getGuestCart().items.reduce((s, i) => s + (i.quantity || 0), 0);
}

function showLoading(show) {
  const el = document.getElementById("loading-root");
  if (el) el.hidden = !show;
}

function toast(message, type = "info") {
  const root = document.getElementById("toast-root");
  if (!root) return;
  const t = document.createElement("div");
  t.className = "toast toast--" + type;
  t.textContent = message;
  root.appendChild(t);
  requestAnimationFrame(() => t.classList.add("toast--show"));
  setTimeout(() => {
    t.classList.remove("toast--show");
    setTimeout(() => t.remove(), 300);
  }, 3200);
}

function initTheme() {
  const saved = localStorage.getItem("crossword_theme");
  if (saved === "dark" || (!saved && window.matchMedia("(prefers-color-scheme: dark)").matches)) {
    document.documentElement.classList.add("theme-dark");
  }
}

function toggleTheme() {
  document.documentElement.classList.toggle("theme-dark");
  const dark = document.documentElement.classList.contains("theme-dark");
  localStorage.setItem("crossword_theme", dark ? "dark" : "light");
}

async function refreshCartBadge() {
  const badges = document.querySelectorAll("[data-cart-count]");
  let count = 0;
  if (getToken()) {
    try {
      const data = await apiFetch("/cart");
      count = (data.items || []).reduce((s, it) => s + (it.quantity || 0), 0);
    } catch {
      count = guestCartCount();
    }
  } else {
    count = guestCartCount();
  }
  badges.forEach((b) => {
    b.textContent = String(count);
    b.style.display = count > 0 ? "flex" : "none";
  });
}

function updateNavAuth() {
  const user = getUser();
  const loginLinks = document.querySelectorAll(".nav-login");
  const logoutBtns = document.querySelectorAll(".nav-logout");
  const adminNav = document.getElementById("nav-admin");
  if (adminNav) {
    adminNav.hidden = !(user && user.role === "admin");
  }
  loginLinks.forEach((a) => {
    if (user) {
      a.textContent = user.name.split(" ")[0];
      a.href = "/";
      a.title = "Logged in";
    } else {
      a.textContent = "Login";
      a.href = "/login";
    }
  });
  logoutBtns.forEach((b) => {
    b.hidden = !user;
  });
}

function bindLogoutButtons() {
  document.querySelectorAll(".nav-logout").forEach((btn) => {
    btn.addEventListener("click", () => {
      logout();
    });
  });
}

function initMobileNav() {
  const btn = document.querySelector(".mobile-menu-btn");
  const nav = document.getElementById("mobile-nav");
  if (!btn || !nav) return;
  btn.addEventListener("click", () => {
    const open = nav.hasAttribute("hidden");
    if (open) nav.removeAttribute("hidden");
    else nav.setAttribute("hidden", "");
    btn.setAttribute("aria-expanded", String(open));
  });
}

async function mergeGuestCartToServer() {
  const guest = getGuestCart();
  if (!guest.items.length) return;
  for (const line of guest.items) {
    try {
      await apiFetch("/cart/add", {
        method: "POST",
        body: JSON.stringify({ product_id: line.product_id, quantity: line.quantity }),
      });
    } catch {
      /* ignore */
    }
  }
  localStorage.removeItem(CART_KEY);
}

document.addEventListener("DOMContentLoaded", () => {
  initTheme();
  initMobileNav();
  bindLogoutButtons();
  updateNavAuth();
  refreshCartBadge();

  document.querySelectorAll(".theme-toggle").forEach((btn) => {
    btn.addEventListener("click", () => {
      toggleTheme();
      btn.textContent = document.documentElement.classList.contains("theme-dark") ? "☀️" : "🌙";
    });
    btn.textContent = document.documentElement.classList.contains("theme-dark") ? "☀️" : "🌙";
  });
});
