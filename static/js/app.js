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
    // Любая другая 5xx — «Сервер недоступен», а не голый код ошибки
    if (err.status >= 500) return t("err_500");
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

  // ---------- Модальные окна (вместо alert/confirm) ----------
  let openModals = 0;

  /**
   * Открывает модальное окно.
   * @param {object} o  { title, body: строка HTML или DOM-узел, actions: [{ label, kind, onClick }] }
   *   kind: "primary" | "secondary" | "success" | "danger".
   *   onClick(m) может быть async; если вернёт false — окно не закрывается.
   * @returns {{ el, body, close }}
   */
  function modal(o) {
    const backdrop = document.createElement("div");
    backdrop.className = "modal-backdrop";
    backdrop.innerHTML = `
      <div class="modal" role="dialog" aria-modal="true">
        <div class="modal-head"><h2></h2><button type="button" class="icon-btn" aria-label="Close">✕</button></div>
        <div class="modal-body"></div>
        <div class="modal-foot"></div>
      </div>`;
    backdrop.querySelector("h2").textContent = o.title || "";
    const body = backdrop.querySelector(".modal-body");
    if (typeof o.body === "string") body.innerHTML = o.body; else if (o.body) body.appendChild(o.body);
    const foot = backdrop.querySelector(".modal-foot");
    const actions = o.actions || [];
    if (!actions.length) foot.remove();
    if (actions.length === 1) foot.classList.add("single");

    let closed = false;
    const onBack = () => close();
    function close() {
      if (closed) return;
      closed = true;
      openModals -= 1;
      document.removeEventListener("keydown", onKey);
      if (tg && tg.BackButton) { tg.BackButton.offClick(onBack); if (!openModals) tg.BackButton.hide(); }
      backdrop.classList.add("closing");
      setTimeout(() => backdrop.remove(), 200);
      if (o.onClose) o.onClose();
    }
    const onKey = (e) => { if (e.key === "Escape") close(); };

    actions.forEach((a) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "btn" + (a.kind && a.kind !== "primary" ? " " + a.kind : "");
      b.textContent = a.label;
      b.addEventListener("click", async () => {
        if (!a.onClick) return close();
        const buttons = foot.querySelectorAll("button");
        buttons.forEach((x) => { x.disabled = true; });
        const label = b.textContent;
        b.innerHTML = `<span class="spinner"></span>`;
        let keepOpen = false;
        try { keepOpen = (await a.onClick(m)) === false; } catch (err) { showError(err); keepOpen = true; }
        finally { buttons.forEach((x) => { x.disabled = false; }); b.textContent = label; }
        if (!keepOpen) close();
      });
      foot.appendChild(b);
    });

    backdrop.addEventListener("click", (e) => { if (e.target === backdrop) close(); });
    backdrop.querySelector(".icon-btn").addEventListener("click", close);
    document.addEventListener("keydown", onKey);
    if (tg && tg.BackButton && tg.initData) { tg.BackButton.onClick(onBack); tg.BackButton.show(); }
    document.body.appendChild(backdrop);
    openModals += 1;
    I18N.apply(backdrop);
    const m = { el: backdrop, body, close };
    return m;
  }

  /**
   * Подтверждение действия. Возвращает Promise<boolean>.
   * @param {string} text  вопрос (можно HTML — экранируйте данные через esc)
   */
  function confirmDialog(text, { title, okLabel, kind = "primary", icon = "❓" } = {}) {
    return new Promise((resolve) => {
      let result = false;
      modal({
        title: title || I18N.t("confirm_title"),
        body: `<div class="confirm-icon">${icon}</div><div class="confirm-text">${text}</div>`,
        actions: [
          { label: I18N.t("cancel"), kind: "secondary" },
          { label: okLabel || I18N.t("confirm"), kind, onClick: () => { result = true; } },
        ],
        onClose: () => resolve(result),
      });
    });
  }

  /** Инициалы для аватара: «NO NAMELA» → «NN» */
  function initials(name) {
    return String(name || "?").split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join("").toUpperCase();
  }

  /** Короткая сумма для карточек: 22 170 000 → «22,17 млн» */
  function fmtShort(v) {
    const n = Number(v || 0);
    const abs = Math.abs(n);
    const loc = numberLocale();
    if (abs >= 1e6) return (n / 1e6).toLocaleString(loc, { maximumFractionDigits: 2 }) + " " + I18N.t("mln");
    if (abs >= 1e4) return (n / 1e3).toLocaleString(loc, { maximumFractionDigits: 1 }) + " " + I18N.t("thousand");
    return n.toLocaleString(loc, { maximumFractionDigits: 0 });
  }

  /** Пустое состояние списка */
  function emptyHtml(icon, textKey) {
    return `<div class="empty"><span class="e-icon">${icon}</span>${esc(I18N.t(textKey))}</div>`;
  }

  /** Скелетон-заглушка на время загрузки */
  function skeletonHtml(lines = 3) {
    return Array.from({ length: lines }, () => `<div class="skeleton line"></div>`).join("");
  }

  /** Единые цвета графиков Chart.js под тёмную тему */
  function setupCharts() {
    if (typeof Chart === "undefined") return false;
    Chart.defaults.color = "#94a3b8";
    Chart.defaults.borderColor = "rgba(148, 163, 184, 0.12)";
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    Chart.defaults.font.size = 12;
    Chart.defaults.plugins.legend.labels.usePointStyle = true;
    Chart.defaults.plugins.legend.labels.boxWidth = 8;
    Chart.defaults.plugins.tooltip.backgroundColor = "#0b1222";
    Chart.defaults.plugins.tooltip.borderColor = "rgba(148, 163, 184, 0.3)";
    Chart.defaults.plugins.tooltip.borderWidth = 1;
    Chart.defaults.plugins.tooltip.padding = 10;
    Chart.defaults.plugins.tooltip.cornerRadius = 10;
    return true;
  }

  /** Вертикальный градиент для столбцов/заливки графика */
  function chartGradient(ctx, from, to) {
    const { chartArea } = ctx.chart;
    if (!chartArea) return from;
    const g = ctx.chart.ctx.createLinearGradient(0, chartArea.bottom, 0, chartArea.top);
    g.addColorStop(0, to);
    g.addColorStop(1, from);
    return g;
  }

  /** Подпись месяца для графика: «2026-10» → «окт 26» */
  function fmtMonth(ym) {
    const [y, m] = String(ym).split("-").map(Number);
    if (!y || !m) return ym;
    return new Date(y, m - 1, 1).toLocaleDateString(numberLocale(), { month: "short", year: "2-digit" });
  }

  /** Общая инициализация страницы */
  function init() {
    if (tg) {
      tg.ready();
      tg.expand();
      // Шапка и фон Telegram в цвет тёмной темы приложения
      try { tg.setHeaderColor("#0f172a"); tg.setBackgroundColor("#0f172a"); } catch (e) { /* старый клиент */ }
    }
    I18N.apply();
    if (!tg || !tg.initData) {
      const banner = document.createElement("div");
      banner.className = "banner";
      banner.textContent = I18N.t("open_in_telegram");
      document.body.prepend(banner);
    }
  }

  window.App = {
    tg, api, ApiError, errorText, fmtMoney, fmtQty, fmtDate, fmtMonth, fmtShort, today, esc, toast, showError,
    initTabs, init, modal, confirm: confirmDialog, initials, emptyHtml, skeletonHtml, setupCharts, chartGradient,
  };
})();
