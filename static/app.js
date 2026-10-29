"use strict";
(() => {
  const tg = window.Telegram?.WebApp;
  const initData = tg?.initData || "";
  const $ = (id) => document.getElementById(id);
  const booleans = ["paused", "show_sma", "show_delta", "show_heatmap"];
  const fields = ["direction", "pump_threshold", "volume_min_usd", "timeframe", "theme", ...booleans];
  let imageUrls = [];
  let loadingHistory = false;
  if (tg) {
    tg.ready();
    tg.expand();
    if (tg.colorScheme === "light") document.documentElement.dataset.theme = "light";
  }
  function banner(message = "") {
    $("banner").textContent = message;
    $("banner").hidden = !message;
  }
  async function api(path, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(path, {
        ...options,
        headers: {"X-Telegram-Init-Data": initData, "Content-Type": "application/json", ...options.headers},
        signal: controller.signal,
        cache: "no-store",
      });
      const body = await response.json();
      if (!response.ok) {
        const detail = typeof body.detail === "string" ? body.detail : "Проверьте поля или откройте монитор заново.";
        throw new Error(response.status === 401 ? "Сессия недействительна. Закройте и снова откройте монитор через Telegram." : detail);
      }
      return body;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("Сервер не ответил за 15 секунд. Повторите попытку.");
      throw error;
    } finally { clearTimeout(timer); }
  }
  function fillSettings(settings) {
    for (const key of fields) {
      const input = $(key);
      if (key === "paused") input.checked = !settings.paused;
      else if (booleans.includes(key)) input.checked = Boolean(settings[key]);
      else input.value = settings[key];
    }
  }
  async function refreshStatus() {
    const status = await api("/api/status");
    $("pairs").textContent = status.pairs ?? "—";
    $("pair-limit").textContent = `Лимит списка: ${status.max_pairs ?? "—"}`;
    $("window").textContent = status.window_sec ? `${status.window_sec / 60} мин` : "—";
    $("sent").textContent = status.sent_alerts ?? "—";
    $("last-poll").textContent = status.last_poll ? new Date(status.last_poll).toLocaleTimeString("ru-RU", {hour12: false, timeZone: "UTC"}) : "—";
    $("poll-interval").textContent = status.poll_interval_sec ? `Опрос: ${status.poll_interval_sec} с · время UTC` : "Ожидание данных";
    $("connection").textContent = status.ready ? "Данные поступают" : "Ожидание / сбой";
    $("connection").className = `pill ${status.ready ? "ok" : "warn"}`;
    $("system-dot").className = `status-dot ${status.ready && status.polling ? "ok" : ""}`;
    $("system-detail").textContent = status.ready && status.polling
      ? `Telegram подключён. В очереди: ${status.queued_alerts}. Время работы: ${Math.floor(status.uptime_sec / 60)} мин.`
      : "Сервис прогревает историю или восстанавливает соединение. После запуска требуется время.";
  }
  function clearImages() { imageUrls.forEach(URL.revokeObjectURL); imageUrls = []; }
  async function showImage(row, button, holder) {
    button.disabled = true;
    try {
      const response = await fetch(row.image_url, {headers: {"X-Telegram-Init-Data": initData}, cache: "no-store"});
      if (!response.ok) throw new Error("График удалён по сроку хранения или временно недоступен.");
      const url = URL.createObjectURL(await response.blob());
      imageUrls.push(url);
      const image = document.createElement("img");
      image.className = "alert-image";
      image.src = url;
      image.alt = `График ${row.symbol}`;
      holder.append(image);
      button.remove();
    } catch (error) { banner(error.message); button.disabled = false; }
  }
  async function refreshHistory() {
    if (loadingHistory) return;
    loadingHistory = true;
    $("refresh-history").disabled = true;
    try {
      const {alerts} = await api("/api/alerts?limit=10");
      clearImages();
      const container = $("alerts");
      container.replaceChildren();
      if (!alerts.length) {
        const empty = document.createElement("div");
        empty.className = "panel empty";
        empty.textContent = "Пока нет доставленных алертов. Настройте фильтры и дождитесь движения рынка.";
        container.append(empty);
        return;
      }
      for (const row of alerts) {
        const article = document.createElement("article");
        article.className = "panel alert-row";
        const content = document.createElement("div");
        content.className = "alert-content";
        const top = document.createElement("h3");
        top.textContent = `${row.direction} · ${row.symbol}`;
        const change = document.createElement("div");
        change.className = `alert-change ${row.direction === "DUMP" ? "dump" : ""}`;
        change.textContent = `${row.change_percent > 0 ? "+" : ""}${row.change_percent.toFixed(2)}%`;
        const detail = document.createElement("p");
        detail.textContent = `Цена: ${row.price.toPrecision(8)} USDT · ${new Date(row.timestamp).toLocaleString("ru-RU", {timeZone: "UTC"})} UTC`;
        content.append(top, change, detail);
        if (row.image_url) {
          const holder = document.createElement("div");
          const button = document.createElement("button");
          button.className = "secondary small chart-button";
          button.textContent = "Показать график";
          button.addEventListener("click", () => showImage(row, button, holder));
          content.append(button, holder);
        }
        article.append(content);
        container.append(article);
      }
    } finally { loadingHistory = false; $("refresh-history").disabled = false; }
  }
  document.querySelectorAll(".tab").forEach((button) => button.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((tab) => {
      const active = tab === button;
      tab.classList.toggle("active", active);
      if (active) tab.setAttribute("aria-current", "page"); else tab.removeAttribute("aria-current");
    });
    document.querySelectorAll(".view").forEach((view) => { view.hidden = view.id !== button.dataset.view; });
    if (button.dataset.view === "history") refreshHistory().catch((error) => banner(error.message));
  }));
  $("refresh-status").addEventListener("click", () => refreshStatus().catch((error) => banner(error.message)));
  $("refresh-history").addEventListener("click", () => refreshHistory().catch((error) => banner(error.message)));
  $("settings-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = $("save-settings");
    button.disabled = true;
    $("save-result").textContent = "";
    banner();
    const payload = {};
    for (const key of fields) {
      const input = $(key);
      payload[key] = key === "paused" ? !input.checked : booleans.includes(key) ? input.checked
        : ["pump_threshold", "volume_min_usd"].includes(key) ? Number(input.value) : input.value;
    }
    try {
      const result = await api("/api/settings", {method: "POST", body: JSON.stringify(payload)});
      fillSettings(result.settings);
      $("save-result").textContent = "✓ Настройки сохранены для вашего чата.";
      tg?.HapticFeedback?.notificationOccurred("success");
    } catch (error) { banner(error.message); }
    finally { button.disabled = false; }
  });
  async function initialize() {
    if (!initData) {
      banner("Откройте монитор кнопкой в Telegram-боте. Вне Telegram доступ к настройкам и истории закрыт.");
      $("connection").textContent = "Нужен Telegram";
      $("system-detail").textContent = "Для входа требуется подписанная сессия Telegram.";
      return;
    }
    try {
      const [settings] = await Promise.all([api("/api/settings"), refreshStatus()]);
      fillSettings(settings);
      $("settings-fields").disabled = false;
    } catch (error) { banner(error.message); }
  }
  setInterval(() => {
    if (initData && !document.hidden && !$("overview").hidden) refreshStatus().catch(() => {
      $("connection").textContent = "Нет связи";
      $("connection").className = "pill warn";
    });
  }, 15000);
  window.addEventListener("pagehide", clearImages);
  initialize();
})();
