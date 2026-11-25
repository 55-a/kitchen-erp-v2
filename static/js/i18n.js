/**
 * Переводы фронтенда Mini App (RU, EN, UZ, ZH).
 * Ключи кнопок совпадают с bot/translations.py (btn_*, role_*), остальные — для интерфейса.
 *
 * Использование в HTML:  <span data-i18n="revenue"></span>
 *                        <input data-i18n-placeholder="quantity">
 * В JS:                  I18N.t("revenue")
 * Язык берётся из ?lang=..., затем из Telegram (language_code), по умолчанию ru.
 */
(function () {
  "use strict";

  const DICT = {
    ru: {
      loading: "Загрузка…", error: "Ошибка", retry: "Повторить", save: "Сохранить", saving: "Сохранение…",
      success: "Готово", cancel: "Отмена", currency: "сум", date: "Дата", total: "Итого",
      network_error: "Нет связи с сервером", forbidden: "Ошибка доступа. Откройте приложение из бота.",
      open_in_telegram: "Откройте это приложение через Telegram-бота.", nothing_found: "Ничего не найдено",
      btn_stats: "📊 Статистика", btn_clients: "🤝 Клиенты", btn_new_batch: "🍲 Новая партия",
      btn_recipes: "📖 Рецепты", btn_new_purchase: "🛒 Новая закупка", btn_deliveries: "📦 Поставки",
      btn_invoice: "🧾 Счёт", btn_act: "📄 Акт",
      revenue: "Выручка", cost: "Себестоимость", profit: "Прибыль", margin: "Маржа", portions: "Порций",
      stock_value: "Склад (стоимость)", purchases_total: "Закупки", chart_title: "Выручка и прибыль по месяцам",
      period: "Период", period_all: "Всё время", period_month: "Этот месяц", period_year: "Этот год",
      client: "Клиент", debt: "Долг", paid: "Оплачено", charged: "Начислено",
      record_payment: "Внести оплату", record_delivery: "Внести поставку", amount: "Сумма",
      payment_type: "Способ оплаты", cash: "Наличные", card: "Карта", transfer: "Перечисление",
      dish: "Блюдо", portions_count: "Порции", cost_label: "Себестоимость", linked: "Привязан к Telegram",
      not_linked: "Не привязан", choose_client: "Выберите клиента", saved: "Сохранено ✅",
      tab_batch: "Новая партия", tab_recipes: "Рецепты", tab_history: "История",
      recipe: "Рецепт", choose_recipe: "Выберите рецепт", planned_yield: "Плановый выход, порций",
      actual_yield: "Фактический выход, порций", ingredients: "Ингредиенты", need: "Нужно",
      in_stock: "На складе", shortage: "Не хватает", create_batch: "Создать партию",
      batch_created: "Партия создана ✅", cost_per_portion: "Себестоимость порции", base_yield: "Базовый выход",
      product: "Товар", choose_product: "Выберите товар", quantity: "Количество", unit: "Ед. изм.",
      total_price: "Сумма по чеку", payment_method: "Оплата", ai_scan: "📸 Распознать чек (ИИ)",
      ai_processing: "Распознаю чек…", ai_items: "Найдено в чеке", add: "✅ Добавить", skip: "✕ Пропустить",
      added: "Добавлено", skipped: "Пропущено", not_in_stock: "Нет на складе", purchase_saved: "Закупка сохранена ✅",
      recent_purchases: "Последние закупки", product_required: "Выберите товар",
      quantity_required: "Укажите количество больше нуля", price_required: "Укажите сумму",
      tab_deliveries: "Поставки", tab_invoice: "Счёт", tab_act: "Акт сверки",
      download_pdf: "⬇️ Скачать PDF", send_to_chat: "📩 Прислать в чат", sent_to_chat: "Акт отправлен в чат с ботом ✅",
      act_hint: "Акт сверки формируется на вашем языке. Оставьте даты пустыми — за всё время.",
      date_from: "С", date_to: "По", no_deliveries: "Поставок пока нет",
      err_product_not_found: "Товар не найден в справочнике", err_insufficient_stock: "Недостаточно продуктов на складе",
      err_unit_mismatch: "Несовместимые единицы измерения", err_validation_error: "Проверьте заполнение полей",
      err_503: "Сервис распознавания временно недоступен", err_429: "Слишком много запросов, подождите минуту",
      err_502: "Не удалось разобрать чек, попробуйте другое фото", err_404: "Не найдено", err_500: "Сервер недоступен, попробуйте позже", err_401: "Ошибка доступа. Откройте приложение из бота.",
    },
    en: {
      loading: "Loading…", error: "Error", retry: "Retry", save: "Save", saving: "Saving…",
      success: "Done", cancel: "Cancel", currency: "UZS", date: "Date", total: "Total",
      network_error: "No connection to the server", forbidden: "Access denied. Open the app from the bot.",
      open_in_telegram: "Please open this app via the Telegram bot.", nothing_found: "Nothing found",
      btn_stats: "📊 Statistics", btn_clients: "🤝 Clients", btn_new_batch: "🍲 New batch",
      btn_recipes: "📖 Recipes", btn_new_purchase: "🛒 New purchase", btn_deliveries: "📦 Deliveries",
      btn_invoice: "🧾 Invoice", btn_act: "📄 Statement",
      revenue: "Revenue", cost: "Cost", profit: "Profit", margin: "Margin", portions: "Portions",
      stock_value: "Stock value", purchases_total: "Purchases", chart_title: "Revenue and profit by month",
      period: "Period", period_all: "All time", period_month: "This month", period_year: "This year",
      client: "Client", debt: "Balance due", paid: "Paid", charged: "Charged",
      record_payment: "Record payment", record_delivery: "Record delivery", amount: "Amount",
      payment_type: "Payment type", cash: "Cash", card: "Card", transfer: "Bank transfer",
      dish: "Dish", portions_count: "Portions", cost_label: "Cost", linked: "Linked to Telegram",
      not_linked: "Not linked", choose_client: "Choose a client", saved: "Saved ✅",
      tab_batch: "New batch", tab_recipes: "Recipes", tab_history: "History",
      recipe: "Recipe", choose_recipe: "Choose a recipe", planned_yield: "Planned yield, portions",
      actual_yield: "Actual yield, portions", ingredients: "Ingredients", need: "Need",
      in_stock: "In stock", shortage: "Shortage", create_batch: "Create batch",
      batch_created: "Batch created ✅", cost_per_portion: "Cost per portion", base_yield: "Base yield",
      product: "Product", choose_product: "Choose a product", quantity: "Quantity", unit: "Unit",
      total_price: "Receipt amount", payment_method: "Payment", ai_scan: "📸 Scan receipt (AI)",
      ai_processing: "Recognizing the receipt…", ai_items: "Found on the receipt", add: "✅ Add", skip: "✕ Skip",
      added: "Added", skipped: "Skipped", not_in_stock: "Not in stock", purchase_saved: "Purchase saved ✅",
      recent_purchases: "Recent purchases", product_required: "Choose a product",
      quantity_required: "Quantity must be greater than zero", price_required: "Enter the amount",
      tab_deliveries: "Deliveries", tab_invoice: "Invoice", tab_act: "Statement",
      download_pdf: "⬇️ Download PDF", send_to_chat: "📩 Send to chat", sent_to_chat: "Statement sent to the bot chat ✅",
      act_hint: "The statement is generated in your language. Leave dates empty for all time.",
      date_from: "From", date_to: "To", no_deliveries: "No deliveries yet",
      err_product_not_found: "Product not found in the catalog", err_insufficient_stock: "Not enough products in stock",
      err_unit_mismatch: "Incompatible units", err_validation_error: "Please check the fields",
      err_503: "Recognition service is temporarily unavailable", err_429: "Too many requests, wait a minute",
      err_502: "Could not parse the receipt, try another photo", err_404: "Not found", err_500: "Server unavailable, try again later", err_401: "Access error. Open the app from the bot.",
    },
    uz: {
      loading: "Yuklanmoqda…", error: "Xatolik", retry: "Qayta urinish", save: "Saqlash", saving: "Saqlanmoqda…",
      success: "Tayyor", cancel: "Bekor qilish", currency: "so'm", date: "Sana", total: "Jami",
      network_error: "Server bilan aloqa yo'q", forbidden: "Ruxsat yo'q. Ilovani botdan oching.",
      open_in_telegram: "Ilovani Telegram bot orqali oching.", nothing_found: "Hech narsa topilmadi",
      btn_stats: "📊 Statistika", btn_clients: "🤝 Mijozlar", btn_new_batch: "🍲 Yangi partiya",
      btn_recipes: "📖 Retseptlar", btn_new_purchase: "🛒 Yangi xarid", btn_deliveries: "📦 Yetkazmalar",
      btn_invoice: "🧾 Hisob", btn_act: "📄 Dalolatnoma",
      revenue: "Tushum", cost: "Tannarx", profit: "Foyda", margin: "Marja", portions: "Porsiya",
      stock_value: "Ombor qiymati", purchases_total: "Xaridlar", chart_title: "Oylar bo'yicha tushum va foyda",
      period: "Davr", period_all: "Butun davr", period_month: "Shu oy", period_year: "Shu yil",
      client: "Mijoz", debt: "Qarz", paid: "To'langan", charged: "Hisoblangan",
      record_payment: "To'lov kiritish", record_delivery: "Yetkazma kiritish", amount: "Summa",
      payment_type: "To'lov turi", cash: "Naqd", card: "Karta", transfer: "O'tkazma",
      dish: "Taom", portions_count: "Porsiya", cost_label: "Tannarx", linked: "Telegramga bog'langan",
      not_linked: "Bog'lanmagan", choose_client: "Mijozni tanlang", saved: "Saqlandi ✅",
      tab_batch: "Yangi partiya", tab_recipes: "Retseptlar", tab_history: "Tarix",
      recipe: "Retsept", choose_recipe: "Retseptni tanlang", planned_yield: "Rejadagi chiqish, porsiya",
      actual_yield: "Haqiqiy chiqish, porsiya", ingredients: "Masalliqlar", need: "Kerak",
      in_stock: "Omborda", shortage: "Yetishmaydi", create_batch: "Partiya yaratish",
      batch_created: "Partiya yaratildi ✅", cost_per_portion: "Porsiya tannarxi", base_yield: "Asosiy chiqish",
      product: "Mahsulot", choose_product: "Mahsulotni tanlang", quantity: "Miqdor", unit: "O'lchov",
      total_price: "Chek summasi", payment_method: "To'lov", ai_scan: "📸 Chekni tanish (AI)",
      ai_processing: "Chek tanilmoqda…", ai_items: "Chekda topildi", add: "✅ Qo'shish", skip: "✕ O'tkazish",
      added: "Qo'shildi", skipped: "O'tkazildi", not_in_stock: "Omborda yo'q", purchase_saved: "Xarid saqlandi ✅",
      recent_purchases: "So'nggi xaridlar", product_required: "Mahsulotni tanlang",
      quantity_required: "Miqdor noldan katta bo'lishi kerak", price_required: "Summani kiriting",
      tab_deliveries: "Yetkazmalar", tab_invoice: "Hisob", tab_act: "Dalolatnoma",
      download_pdf: "⬇️ PDF yuklab olish", send_to_chat: "📩 Chatga yuborish", sent_to_chat: "Dalolatnoma bot chatiga yuborildi ✅",
      act_hint: "Dalolatnoma sizning tilingizda tuziladi. Butun davr uchun sanalarni bo'sh qoldiring.",
      date_from: "Dan", date_to: "Gacha", no_deliveries: "Hozircha yetkazmalar yo'q",
      err_product_not_found: "Mahsulot ma'lumotnomada topilmadi", err_insufficient_stock: "Omborda mahsulot yetarli emas",
      err_unit_mismatch: "O'lchov birliklari mos emas", err_validation_error: "Maydonlarni tekshiring",
      err_503: "Tanish xizmati vaqtincha ishlamayapti", err_429: "So'rovlar juda ko'p, bir daqiqa kuting",
      err_502: "Chekni o'qib bo'lmadi, boshqa surat yuboring", err_404: "Topilmadi", err_500: "Server ishlamayapti, keyinroq urinib ko'ring", err_401: "Ruxsat xatosi. Ilovani botdan oching.",
    },
    zh: {
      loading: "加载中…", error: "错误", retry: "重试", save: "保存", saving: "保存中…",
      success: "完成", cancel: "取消", currency: "苏姆", date: "日期", total: "合计",
      network_error: "无法连接服务器", forbidden: "无权访问。请从机器人中打开应用。",
      open_in_telegram: "请通过 Telegram 机器人打开此应用。", nothing_found: "未找到",
      btn_stats: "📊 统计", btn_clients: "🤝 客户", btn_new_batch: "🍲 新批次",
      btn_recipes: "📖 配方", btn_new_purchase: "🛒 新采购", btn_deliveries: "📦 送货",
      btn_invoice: "🧾 账单", btn_act: "📄 对账单",
      revenue: "收入", cost: "成本", profit: "利润", margin: "利润率", portions: "份数",
      stock_value: "库存价值", purchases_total: "采购", chart_title: "按月收入与利润",
      period: "期间", period_all: "全部", period_month: "本月", period_year: "今年",
      client: "客户", debt: "欠款", paid: "已付", charged: "应收",
      record_payment: "登记付款", record_delivery: "登记送货", amount: "金额",
      payment_type: "付款方式", cash: "现金", card: "银行卡", transfer: "转账",
      dish: "菜品", portions_count: "份数", cost_label: "成本", linked: "已绑定 Telegram",
      not_linked: "未绑定", choose_client: "选择客户", saved: "已保存 ✅",
      tab_batch: "新批次", tab_recipes: "配方", tab_history: "历史",
      recipe: "配方", choose_recipe: "选择配方", planned_yield: "计划产量（份）",
      actual_yield: "实际产量（份）", ingredients: "原料", need: "需要",
      in_stock: "库存", shortage: "不足", create_batch: "创建批次",
      batch_created: "批次已创建 ✅", cost_per_portion: "每份成本", base_yield: "基础产量",
      product: "商品", choose_product: "选择商品", quantity: "数量", unit: "单位",
      total_price: "收据金额", payment_method: "付款", ai_scan: "📸 识别收据（AI）",
      ai_processing: "正在识别收据…", ai_items: "收据中的商品", add: "✅ 添加", skip: "✕ 跳过",
      added: "已添加", skipped: "已跳过", not_in_stock: "库存中没有", purchase_saved: "采购已保存 ✅",
      recent_purchases: "最近采购", product_required: "请选择商品",
      quantity_required: "数量必须大于零", price_required: "请输入金额",
      tab_deliveries: "送货", tab_invoice: "账单", tab_act: "对账单",
      download_pdf: "⬇️ 下载 PDF", send_to_chat: "📩 发送到聊天", sent_to_chat: "对账单已发送到机器人聊天 ✅",
      act_hint: "对账单将以您的语言生成。日期留空表示全部期间。",
      date_from: "从", date_to: "到", no_deliveries: "暂无送货记录",
      err_product_not_found: "商品目录中未找到该商品", err_insufficient_stock: "库存不足",
      err_unit_mismatch: "单位不兼容", err_validation_error: "请检查填写内容",
      err_503: "识别服务暂时不可用", err_429: "请求过多，请稍等一分钟",
      err_502: "无法解析收据，请换一张照片", err_404: "未找到", err_500: "服务器不可用，请稍后再试", err_401: "访问错误。请从机器人中打开应用。",
    },
  };

  const SUPPORTED = Object.keys(DICT);

  function detectLang() {
    const fromUrl = new URLSearchParams(location.search).get("lang");
    if (fromUrl && SUPPORTED.includes(fromUrl)) return fromUrl;
    const tgLang = window.Telegram?.WebApp?.initDataUnsafe?.user?.language_code;
    const short = (tgLang || "").split("-")[0];
    return SUPPORTED.includes(short) ? short : "ru";
  }

  let lang = detectLang();

  /** Перевод по ключу; {name} в строке заменяется значением params.name */
  function t(key, params) {
    let text = (DICT[lang] && DICT[lang][key]) || DICT.ru[key] || key;
    if (params) {
      Object.keys(params).forEach((k) => { text = text.replaceAll("{" + k + "}", params[k]); });
    }
    return text;
  }

  /** Применяет переводы ко всем элементам с data-i18n* внутри root */
  function apply(root) {
    (root || document).querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
    (root || document).querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
      el.placeholder = t(el.dataset.i18nPlaceholder);
    });
    document.documentElement.lang = lang;
  }

  window.I18N = {
    t, apply, SUPPORTED,
    get lang() { return lang; },
    setLang(code) { if (SUPPORTED.includes(code)) { lang = code; apply(); } },
    has(key) { return Boolean(DICT[lang][key] || DICT.ru[key]); },
  };
})();
