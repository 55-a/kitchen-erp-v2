/**
 * Общий код всех Mini App: Telegram WebApp, запросы к API, форматирование, вкладки, уведомления.
 *
 * Исправления из технического обзора:
 *  - все запросы идут по ОТНОСИТЕЛЬНЫМ путям (/api/...), без захардкоженного домена;
 *  - response.ok проверяется ДО парсинга JSON, ошибки сервера показываются пользователю;
 *  - initData передаётся в заголовке X-Telegram-Init-Data (сервер проверяет подпись → 403).
 */
(function () {
  "use strict";

  const tg = window.Telegram ? window.Telegram.WebApp : null;

  /** Ошибка API с HTTP-статусом и телом detail */
  class ApiError extends Error {
    constructor(status, detail) {
      super(typeof detail === "string" ? detail : (detail && detail.message) || "HTTP " + status);
      this.status = status;
      this.detail = detail;
    }
  }

  /**
   * Запрос к API.
   * @param {string} path  относительный путь, например "/api/stock"
   * @param {object} opts  { method, json, form, query }
   */
  async function api(path, opts = {}) {
    if (!path.startsWith("/")) throw new Error("API path must be relative and start with /");
    const headers = { "X-Telegram-Init-Data": tg ? tg.initData : "" };
    let body;
    if (opts.json !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(opts.json);
    } else if (opts.form) {
      body = opts.form; // FormData — Content-Type с boundary проставит браузер
    }
    let url = path;
    if (opts.query) {
      const q = new URLSearchParams();
      Object.entries(opts.query).forEach(([k, v]) => { if (v !== undefined && v !== null && v !== "") q.set(k, v); });
      const qs = q.toString();
      if (qs) url += (url.includes("?") ? "&" : "?") + qs;
    }

    let resp;
    try {
      resp = await fetch(url, { method: opts.method || (body ? "POST" : "GET"), headers, body });
    } catch (e) {
      throw new ApiError(0, null); // сеть недоступна
    }

    // Сначала проверяем статус, и только потом парсим тело
    if (!resp.ok) {
      let detail = null;
      try {
        const data = await resp.json();
        detail = data && data.detail !== undefined ? data.detail : data;
      } catch (e) { /* тело не JSON — оставляем null */ }
      throw new ApiError(resp.status, detail);
    }
    const type = resp.headers.get("Content-Type") || "";
    if (type.includes("application/json")) return resp.json();
    if (type.includes("application/pdf")) return resp.blob();
    return resp.text();
  }

  /** Человекочитаемый текст ошибки на текущем языке */
  function errorText(err) {
    const t = I18N.t;
    if (!(err instanceof ApiError)) return (err && err.message) || t("error");
    if (err.status === 0) return t("network_error");
    if (err.status === 403) return t("forbidden");
    const d = err.detail;
    if (d && typeof d === "object" && d.code) {
      let text = I18N.has("err_" + d.code) ? t("err_" + d.code) : d.message || t("error");
      if (d.code === "insufficient_stock" && Array.isArray(d.shortages)) {
        text += ":\n" + d.shortages.map((s) => `• ${s.product}: ${fmtQty(s.need)} / ${fmtQty(s.available)} ${s.unit}`).join("\n");
      }
      if (d.code === "validation_error" && Array.isArray(d.errors)) {
        text += ": " + d.errors.map((e) => e.field).filter(Boolean).join(", ");
      }
      return text;
    }
    if (I18N.has("err_" + err.status)) return t("err_" + err.status);
    return typeof d === "string" ? d : t("error") + " " + err.status;
  }

  const numberLocale = () => ({ ru: "ru-RU", en: "en-US", uz: "uz-UZ", zh: "zh-CN" }[I18N.lang] || "ru-RU");

  function fmtMoney(v) {
    return Number(v || 0).toLocaleString(numberLocale(), { maximumFractionDigits: 0 }) + " " + I18N.t("currency");
  }
  function fmtQty(v) {
    return Number(v || 0).toLocaleString(numberLocale(), { maximumFractionDigits: 3 });
  }
  function fmtDate(iso) {
    if (!iso) return "";
    const [y, m, d] = iso.split("-");
    return `${d}.${m}.${y}`;
  }
  function today() {
    const d = new Date();
    return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  }

  /** Экранирование HTML для вставки пользовательских строк в innerHTML */
  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  /** Всплывающее уведомление внизу экрана */
  function toast(message, type = "ok") {
    let el = document.getElementById("toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "toast";
      document.body.appendChild(el);
    }
    el.textContent = message;
    el.className = "toast show " + type;
    clearTimeout(el._timer);
    el._timer = setTimeout(() => { el.className = "toast"; }, type === "error" ? 5000 : 2500);
    if (tg && tg.HapticFeedback) {
      tg.HapticFeedback.notificationOccurred(type === "error" ? "error" : "success");
    }
  }

  function showError(err) {
    console.error(err);
    toast(errorText(err), "error");
  }

  /** Вкладки: .tabs button[data-tab] + section[data-panel]. Стартовая вкладка — из ?tab= */
  function initTabs(onChange) {
    const buttons = document.querySelectorAll(".tabs [data-tab]");
    const panels = document.querySelectorAll("[data-panel]");
    function activate(name) {
      let found = false;
      buttons.forEach((b) => { const on = b.dataset.tab === name; b.classList.toggle("active", on); found ||= on; });
      if (!found && buttons.length) return activate(buttons[0].dataset.tab);
      panels.forEach((p) => { p.hidden = p.dataset.panel !== name; });
      if (onChange) onChange(name);
    }
    buttons.forEach((b) => b.addEventListener("click", () => activate(b.dataset.tab)));
    activate(new URLSearchParams(location.search).get("tab") || (buttons[0] && buttons[0].dataset.tab));
    return activate;
  }

  /** Общая инициализация страницы */
  function init() {
    if (tg) {
      tg.ready();
      tg.expand();
    }
    I18N.apply();
    if (!tg || !tg.initData) {
      const banner = document.createElement("div");
      banner.className = "banner";
      banner.textContent = I18N.t("open_in_telegram");
      document.body.prepend(banner);
    }
  }

  window.App = { tg, api, ApiError, errorText, fmtMoney, fmtQty, fmtDate, today, esc, toast, showError, initTabs, init };
})();
