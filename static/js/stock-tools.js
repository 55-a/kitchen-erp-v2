/**
 * Склад: «Сырьё» (справочник продуктов) и «Инвентаризация» (пересчёт остатков).
 * Раньше это было у повара; теперь повар склад не ведёт — пересчёт делает закупщик (или владелец)
 * раз в неделю. Использование: StockTools.openRaw(), StockTools.openInventory({ after }).
 */
(function () {
  "use strict";
  const { api, fmtMoney, fmtQty, esc, toast, modal, confirm } = App;
  const t = I18N.t;
  let stock = [];
  async function loadStock() {
    try { stock = await api("/api/stock"); } catch (e) { stock = []; }
    return stock;
  }

  // ---------- Сырьё: справочник продуктов ----------
  function openRawMaterials() {
    let cat = "all";
    const body = document.createElement("div");
    body.className = "form";
    body.innerHTML = `
      <div class="search"><input type="search" data-role="q" autocomplete="off" placeholder="${esc(t("search_product"))}"></div>
      <div class="chips" data-role="cats"></div>
      <div class="row">
        <button type="button" class="btn" data-role="new">＋ ${esc(t("new_product"))}</button>
        <button type="button" class="btn secondary" data-role="auto">🪄 ${esc(t("auto_categories"))}</button>
      </div>
      <div class="list" data-role="list"></div>`;
    const draw = () => {
      const counts = {};
      stock.forEach((p) => { counts[p.category] = (counts[p.category] || 0) + 1; });
      body.querySelector('[data-role="cats"]').innerHTML = [["all", `🧺 ${t("all")} · ${stock.length}`]]
        .concat(App.CATEGORY_KEYS.filter((c) => counts[c]).map((c) => [c, `${App.CATEGORY_ICONS[c]} ${t("cat_" + c)} · ${counts[c]}`]))
        .map(([c, l]) => `<button type="button" class="chip ${c === cat ? "active" : ""}" data-cat="${c}">${esc(l)}</button>`).join("");
      body.querySelector('[data-role="auto"]').hidden = !(counts.other > 3);
      const q = body.querySelector('[data-role="q"]').value.trim().toLowerCase();
      body.querySelector('[data-role="list"]').innerHTML = stock
        .filter((p) => (cat === "all" || p.category === cat) && (!q || p.name.toLowerCase().includes(q)))
        .map((p) => `<div class="list-item tappable" data-pid="${p.id}"><div class="lead">${App.CATEGORY_ICONS[p.category] || "🧺"}</div>
          <div class="grow"><div class="title">${esc(p.name)}</div><div class="hint">${esc(t("cat_" + p.category))} · ${fmtMoney(p.price_per_unit)} / ${esc(p.unit)}</div></div>
          <div class="amount ${p.quantity <= 0 ? "danger" : ""}">${fmtQty(p.quantity)} ${esc(p.unit)}</div></div>`).join("")
        || App.emptyHtml("🔍", "nothing_found");
    };
    body.addEventListener("input", draw);
    body.addEventListener("click", async (ev) => {
      const c = ev.target.closest("[data-cat]");
      if (c) { cat = c.dataset.cat; draw(); return; }
      if (ev.target.closest('[data-role="new"]')) {
        const p = await App.createProduct({ name: body.querySelector('[data-role="q"]').value.trim() });
        if (p) { await loadStock(); draw(); }
        return;
      }
      if (ev.target.closest('[data-role="auto"]')) {
        // Раскладываем «Прочее» по категориям по названию — одним нажатием
        const todo = stock.filter((p) => p.category === "other" && App.guessCategory(p.name) !== "other");
        if (!todo.length) { toast(t("nothing_found"), "warning"); return; }
        if (!(await confirm(esc(t("auto_categories_confirm", { n: todo.length })), { icon: "🪄" }))) return;
        for (const p of todo) await api(`/api/stock/${p.id}`, { method: "PATCH", json: { category: App.guessCategory(p.name) } });
        toast(t("saved")); await loadStock(); draw();
        return;
      }
      const it = ev.target.closest("[data-pid]");
      if (it) editProduct(stock.find((p) => String(p.id) === it.dataset.pid), draw);
    });
    draw();
    modal({ title: "🥕 " + t("qa_raw"), body });
  }

  function editProduct(p, after) {
    const body = document.createElement("div");
    body.className = "form";
    body.innerHTML = `
      <div class="field"><label>${esc(t("product_name"))}</label><input name="name" maxlength="255" value="${esc(p.name)}"></div>
      <div class="field"><label>${esc(t("category"))}</label>
        <select name="category">${App.CATEGORY_KEYS.map((c) => `<option value="${c}" ${c === p.category ? "selected" : ""}>${App.CATEGORY_ICONS[c]} ${esc(t("cat_" + c))}</option>`).join("")}</select></div>
      <div class="small muted">${esc(t("in_stock"))}: ${fmtQty(p.quantity)} ${esc(p.unit)} · ${fmtMoney(p.price_per_unit)} / ${esc(p.unit)}</div>`;
    modal({ title: "✏️ " + p.name, body, actions: [
      { label: t("cancel"), kind: "secondary" },
      { label: t("save"), onClick: async () => {
        await api(`/api/stock/${p.id}`, { method: "PATCH", json: { name: body.querySelector('[name="name"]').value.trim(), category: body.querySelector('[name="category"]').value } });
        toast(t("saved")); await loadStock(); after && after();
      } },
    ] });
  }

  // ---------- Инвентаризация: фактические остатки ----------
  async function openInventory() {
    await loadStock();
    const counted = {};   // product_id → {quantity, price}
    const body = document.createElement("div");
    body.className = "form";
    body.innerHTML = `
      <p class="small muted">${esc(t("inventory_hint"))}</p>
      <div class="search"><input type="search" data-role="q" autocomplete="off" placeholder="${esc(t("search_product"))}"></div>
      <div class="usage-list" data-role="list"></div>
      <div class="small muted" data-role="changed"></div>`;
    const draw = () => {
      const q = body.querySelector('[data-role="q"]').value.trim().toLowerCase();
      body.querySelector('[data-role="list"]').innerHTML = stock
        .filter((p) => !q || p.name.toLowerCase().includes(q))
        .map((p) => {
          const c = counted[p.id] || {};
          return `
            <div class="usage-row">
              <div class="u-name"><b>${App.CATEGORY_ICONS[p.category] || ""} ${esc(p.name)}</b>
                <div class="small muted">${esc(t("by_records"))}: ${fmtQty(p.quantity)} ${esc(p.unit)}</div></div>
              <div class="u-input">
                <input type="number" inputmode="decimal" min="0" step="any" data-inv="${p.id}" value="${c.quantity ?? ""}" placeholder="${fmtQty(p.quantity)}">
                <span class="u-unit">${esc(p.unit)}</span>
              </div>
              ${!p.price_per_unit ? `<div class="u-price"><input type="number" inputmode="decimal" min="0" step="any" data-price="${p.id}" value="${c.price ?? ""}" placeholder="${esc(t("price_per_unit"))}, ${esc(t("currency"))}/${esc(p.unit)}"></div>` : ""}
            </div>`;
        }).join("") || App.emptyHtml("🔍", "nothing_found");
      const n = Object.values(counted).filter((c) => c.quantity !== undefined && c.quantity !== "").length;
      body.querySelector('[data-role="changed"]').textContent = n ? t("inventory_counted", { n }) : "";
    };
    body.addEventListener("input", (ev) => {
      if (ev.target.dataset.inv) { (counted[ev.target.dataset.inv] ||= {}).quantity = ev.target.value; }
      else if (ev.target.dataset.price) { (counted[ev.target.dataset.price] ||= {}).price = ev.target.value; }
      else return draw();
      const n = Object.values(counted).filter((c) => c.quantity !== undefined && c.quantity !== "").length;
      body.querySelector('[data-role="changed"]').textContent = n ? t("inventory_counted", { n }) : "";
    });
    draw();
    modal({
      title: "📋 " + t("qa_inventory"),
      body,
      actions: [
        { label: t("cancel"), kind: "secondary" },
        { label: "💾 " + t("save"), onClick: async () => {
          const items = Object.entries(counted)
            .filter(([, c]) => (c.quantity !== undefined && c.quantity !== "") || (c.price !== undefined && c.price !== ""))
            .map(([id, c]) => {
              const p = stock.find((x) => String(x.id) === id);
              return {
                product_id: Number(id),
                quantity: c.quantity !== undefined && c.quantity !== "" ? parseFloat(c.quantity) : p.quantity,
                price_per_unit: c.price !== undefined && c.price !== "" ? parseFloat(c.price) : null,
              };
            });
          if (!items.length) { toast(t("inventory_nothing"), "error"); return false; }
          if (!(await confirm(esc(t("inventory_confirm", { n: items.length })), { icon: "📋" }))) return false;
          const res = await api("/api/stock/inventory", { json: { items } });
          toast(t("inventory_saved", { n: res.count }));
          await loadStock();
        } },
      ],
    });
  }

  window.StockTools = {
    async openRaw() { await loadStock(); openRawMaterials(); },
    openInventory,
  };
})();
