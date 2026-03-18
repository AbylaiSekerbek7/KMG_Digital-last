const state = {
  summary: null,
  catalog: null,
  appConfig: null,
  reviewer: null,
  lastRecommendationDestination: null,
  liveState: null,
  operationsMap: null,
  operationsMapLayers: null,
  mapHasFittedBounds: false,
  playbackTimer: null,
  playbackIndex: 0,
  currentPlayback: null,
  playbackSpeed: 1,
  playbackRunning: false,
  playbackSource: null,
  ws: null,
  wsConnected: false,
  wsReconnectTimer: null,
  fallbackPollingEnabled: false,
};

const STRATEGY_LABELS = {
  baseline: "Ближайшая свободная",
  priority_greedy: "Приоритетный жадный",
  multistop_heuristic: "Группировка выездов",
  ortools_solver: "Глобальный OR-Tools",
};

const PRIORITY_LABELS = {
  high: "Высокий",
  medium: "Средний",
  low: "Низкий",
};

const SHIFT_LABELS = {
  day: "День",
  night: "Ночь",
};

const TASK_TYPE_LABELS = {
  acidizing: "Кислотная обработка",
  cementing: "Цементирование",
  inspection: "Инспекция",
  transport: "Транспорт",
};

const STATUS_LABELS = {
  idle: "Свободна",
  waiting: "Ожидает старт",
  driving: "В пути",
  waiting_at_site: "На точке",
  servicing: "В работе",
  completed: "Завершила",
};

document.addEventListener("DOMContentLoaded", () => {
  bindEvents();
  bootstrap();
});

async function bootstrap() {
  await refreshAll();
  initRealtimeSocket();
}

function bindEvents() {
  document.getElementById("reload-dataset-btn").addEventListener("click", async () => {
    await requestJson("/api/dataset/reload", { method: "POST" });
    await refreshAll();
  });
  document.getElementById("logout-btn").addEventListener("click", logoutReviewer);

  document.querySelectorAll('input[name="recommendation-mode"]').forEach((input) => {
    input.addEventListener("change", updateRecommendationMode);
  });

  document.getElementById("run-recommendations-btn").addEventListener("click", runRecommendations);
  document.getElementById("run-route-btn").addEventListener("click", runRoute);
  document.getElementById("run-multitask-btn").addEventListener("click", runMultitask);
  document.getElementById("run-plan-btn").addEventListener("click", runPlan);
  document.getElementById("run-benchmark-btn").addEventListener("click", runBenchmark);
  document.getElementById("run-replay-btn").addEventListener("click", runReplayOnly);
  document.getElementById("run-tuning-btn").addEventListener("click", runTuning);
  document.getElementById("multitask-select-first-btn").addEventListener("click", () => selectMultitaskPreset("first"));
  document.getElementById("multitask-select-day-btn").addEventListener("click", () => selectMultitaskPreset("day"));
  document.getElementById("multitask-select-night-btn").addEventListener("click", () => selectMultitaskPreset("night"));
  document.getElementById("multitask-clear-btn").addEventListener("click", () => selectMultitaskPreset("clear"));
  document.getElementById("refresh-live-state-btn").addEventListener("click", refreshLiveState);
  document.getElementById("refresh-audit-btn").addEventListener("click", refreshAuditTrail);
  document.getElementById("clear-audit-btn").addEventListener("click", clearAuditTrail);
  document.getElementById("pause-playback-btn").addEventListener("click", togglePlaybackPause);
  document.getElementById("step-backward-btn").addEventListener("click", () => stepPlayback(-1));
  document.getElementById("step-forward-btn").addEventListener("click", () => stepPlayback(1));
  document.getElementById("reset-playback-btn").addEventListener("click", resetPlayback);
  document.getElementById("playback-speed-select").addEventListener("change", updatePlaybackSpeed);
  document.getElementById("playback-scrubber").addEventListener("input", onPlaybackScrub);
}

async function refreshAll() {
  setStatus("recommendations-status", "Загружаю набор данных...", "success");
  setStatus("route-status", "", "");
  setStatus("multitask-status", "", "");
  setStatus("plan-status", "", "");
  setStatus("benchmark-status", "", "");
  setStatus("tuning-status", "", "");

  const [appConfig, summary, catalog, authMe] = await Promise.all([
    requestJson("/app-config"),
    requestJson("/api/dataset/summary"),
    requestJson("/api/catalog"),
    requestJson("/auth/me"),
  ]);

  state.appConfig = appConfig;
  state.summary = summary;
  state.catalog = catalog;
  state.reviewer = authMe.reviewer;
  renderReviewerHeader(authMe.reviewer);
  renderSummary(summary);
  hydrateFormOptions(catalog);
  updateRecommendationMode();
  await refreshLiveState();
  await refreshAuditTrail();
  setStatus("recommendations-status", "Данные загружены.", "success");
}

function initRealtimeSocket() {
  if (state.ws && (state.ws.readyState === WebSocket.OPEN || state.ws.readyState === WebSocket.CONNECTING)) {
    return;
  }

  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const url = new URL(`${protocol}://${window.location.host}/ws/live`);
  const socketToken = state.appConfig?.websocket_token;
  if (socketToken) {
    url.searchParams.set("api_key", socketToken);
  }

  setRealtimeConnectionBadge("подключение...", "offline");
  try {
    const socket = new WebSocket(url);
    state.ws = socket;

    socket.addEventListener("open", () => {
      state.wsConnected = true;
      clearWsReconnectTimer();
      disableFallbackPolling();
      setRealtimeConnectionBadge("WebSocket подключен", "online");
      sendWsMessage({ type: "request_snapshot" });
      sendWsMessage({ type: "request_audit", limit: 20 });
    });

    socket.addEventListener("message", (event) => {
      handleRealtimeMessage(event.data);
    });

    socket.addEventListener("close", () => {
      state.wsConnected = false;
      setRealtimeConnectionBadge("режим fallback / polling", "offline");
      enableFallbackPolling();
      scheduleWsReconnect();
    });

    socket.addEventListener("error", () => {
      setRealtimeConnectionBadge("ошибка WebSocket", "error");
    });
  } catch (_error) {
    setRealtimeConnectionBadge("ошибка подключения", "error");
    enableFallbackPolling();
    scheduleWsReconnect();
  }
}

function handleRealtimeMessage(rawMessage) {
  try {
    const message = JSON.parse(rawMessage);
    const payload = message.payload || {};
    switch (message.type) {
      case "snapshot":
        state.liveState = payload;
        renderOperationsMap(payload);
        if (!state.currentPlayback && payload.latest_replay?.total_frames) {
          renderPlaybackMeta(null);
        }
        break;
      case "audit_trail":
        renderAuditTrail(payload);
        break;
      case "playback_started":
        preparePlayback(
          {
            frame_interval_minutes: payload.frame_interval_minutes,
            start_time: payload.start_time,
            end_time: payload.end_time,
            frames: [],
          },
          { autoplay: true, source: "stream" },
        );
        break;
      case "playback_frame":
        consumePlaybackFrame(payload);
        break;
      case "playback_completed":
        state.playbackRunning = false;
        document.getElementById("pause-playback-btn").textContent = "Продолжить";
        renderPlaybackMeta(state.currentPlayback?.frames[state.playbackIndex] || null);
        break;
      case "playback_stopped":
        state.playbackRunning = false;
        document.getElementById("pause-playback-btn").textContent = "Продолжить";
        break;
      case "error":
        setStatus("route-status", payload.detail || "Ошибка realtime-канала.", "error");
        break;
      case "pong":
      case "connection":
      default:
        break;
    }
  } catch (_error) {
    // Ignore malformed realtime payloads in UI.
  }
}

function consumePlaybackFrame(payload) {
  const frame = payload.frame;
  if (!frame) {
    return;
  }
  if (!state.currentPlayback || state.playbackSource !== "stream") {
    preparePlayback(
      {
        frame_interval_minutes: payload.frame_interval_minutes,
        start_time: payload.start_time,
        end_time: payload.end_time,
        frames: [],
      },
      { autoplay: true, source: "stream" },
    );
  }
  state.currentPlayback.frames.push(frame);
  document.getElementById("playback-scrubber").max = String(Math.max(0, state.currentPlayback.frames.length - 1));
  if (state.playbackRunning) {
    state.playbackIndex = state.currentPlayback.frames.length - 1;
    renderPlaybackFrame();
  }
}

function scheduleWsReconnect() {
  if (state.wsReconnectTimer) {
    return;
  }
  state.wsReconnectTimer = window.setTimeout(() => {
    state.wsReconnectTimer = null;
    initRealtimeSocket();
  }, 3000);
}

function clearWsReconnectTimer() {
  if (state.wsReconnectTimer) {
    window.clearTimeout(state.wsReconnectTimer);
    state.wsReconnectTimer = null;
  }
}

function setRealtimeConnectionBadge(text, tone) {
  const badge = document.getElementById("realtime-connection-badge");
  badge.textContent = text;
  badge.classList.remove("badge-online", "badge-offline", "badge-error");
  badge.classList.add(
    tone === "online" ? "badge-online" : tone === "error" ? "badge-error" : "badge-offline",
  );
}

function sendWsMessage(message) {
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
    return;
  }
  state.ws.send(JSON.stringify(message));
}

function renderSummary(summary) {
  const summaryCards = document.getElementById("summary-cards");
  summaryCards.innerHTML = "";
  document.getElementById("dataset-mode-badge").textContent = translateDatasetMode(summary.mode);

  const cardData = [
    ["Узлы графа", summary.nodes],
    ["Ребра графа", summary.edges],
    ["Скважины", summary.wells],
    ["Машины", summary.vehicles],
    ["Заявки", summary.tasks],
    ["Типы работ", (summary.task_types || []).map(translateTaskType).join(", ")],
    ["Дневные заявки", summary.task_breakdown?.by_shift?.day ?? 0],
    ["Ночные заявки", summary.task_breakdown?.by_shift?.night ?? 0],
  ];
  const template = document.getElementById("summary-card-template");
  for (const [label, value] of cardData) {
    const node = template.content.firstElementChild.cloneNode(true);
    node.querySelector(".summary-label").textContent = label;
    node.querySelector(".summary-value").textContent = value;
    summaryCards.appendChild(node);
  }
}

function hydrateFormOptions(catalog) {
  fillSelect(
    document.getElementById("recommendation-task-id"),
    catalog.tasks.map((task) => ({
      value: task.task_id,
      label: `${task.task_id} | ${translatePriority(task.priority)} | ${translateTaskType(task.task_type)} | ${task.destination_uwi}`,
    })),
  );
  fillSelect(
    document.getElementById("recommendation-strategy"),
    catalog.strategies.map((strategy) => ({ value: strategy, label: translateStrategy(strategy) })),
  );
  fillSelect(
    document.getElementById("plan-strategy"),
    catalog.strategies.map((strategy) => ({ value: strategy, label: translateStrategy(strategy) })),
  );
  fillSelect(
    document.getElementById("map-playback-strategy"),
    catalog.strategies.map((strategy) => ({ value: strategy, label: translateStrategy(strategy) })),
  );
  fillSelect(
    document.getElementById("custom-destination-uwi"),
    catalog.wells.map((well) => ({
      value: well.uwi,
      label: `${well.uwi} | ${well.well_name}`,
    })),
  );
  fillSelect(
    document.getElementById("custom-task-type"),
    catalog.task_types.map((taskType) => ({ value: taskType, label: translateTaskType(taskType) })),
  );
  fillSelect(
    document.getElementById("route-vehicle-id"),
    catalog.vehicles.map((vehicle) => ({
      value: String(vehicle.vehicle_id),
      label: `${vehicle.vehicle_id} | ${vehicle.name} | свободна ${formatShortDate(vehicle.available_at)}`,
    })),
  );
  fillSelect(
    document.getElementById("route-well-uwi"),
    catalog.wells.map((well) => ({
      value: well.uwi,
      label: `${well.uwi} | ${well.well_name}`,
    })),
  );
  const defaultStartDay = catalog.tasks[0]?.start_day || "2026-03-17";
  document.getElementById("custom-start-day").value = defaultStartDay;
  renderMultitaskCheckboxes(catalog.tasks);
}

let pollingHandle = null;

function enableFallbackPolling() {
  if (pollingHandle !== null) {
    return;
  }
  state.fallbackPollingEnabled = true;
  pollingHandle = window.setInterval(async () => {
    try {
      await refreshLiveState({ silent: true });
      await refreshAuditTrail();
    } catch (_error) {
      // Keep UI resilient during background polling.
    }
  }, 15000);
}

function disableFallbackPolling() {
  state.fallbackPollingEnabled = false;
  if (pollingHandle !== null) {
    window.clearInterval(pollingHandle);
    pollingHandle = null;
  }
}

function fillSelect(select, options) {
  select.innerHTML = "";
  for (const option of options) {
    const node = document.createElement("option");
    node.value = option.value;
    node.textContent = option.label;
    select.appendChild(node);
  }
}

function renderReviewerHeader(reviewer) {
  const nameNode = document.getElementById("reviewer-name");
  if (!reviewer) {
    nameNode.textContent = "Гость";
    return;
  }
  nameNode.textContent = `Вошел: ${reviewer.display_name || reviewer.username}`;
}

async function logoutReviewer() {
  try {
    await fetch("/auth/logout", { method: "POST" });
  } finally {
    window.location.href = "/login";
  }
}

function renderMultitaskCheckboxes(tasks) {
  const container = document.getElementById("multitask-checkboxes");
  container.innerHTML = "";
  for (const task of tasks) {
    const label = document.createElement("label");
    label.className = "checkbox-item";
    label.innerHTML = `
      <input type="checkbox" value="${task.task_id}" />
      <span>
        <strong>${task.task_id}</strong>
        <small>${translatePriority(task.priority)} | ${translateTaskType(task.task_type)} | ${task.destination_uwi}</small>
      </span>
    `;
    container.appendChild(label);
  }
}

function selectMultitaskPreset(mode) {
  const taskById = new Map((state.catalog?.tasks || []).map((task) => [task.task_id, task]));
  const checkboxes = [...document.querySelectorAll("#multitask-checkboxes input")];
  for (const checkbox of checkboxes) {
    const task = taskById.get(checkbox.value);
    if (mode === "clear") {
      checkbox.checked = false;
      continue;
    }
    if (mode === "first") {
      checkbox.checked = checkboxes.indexOf(checkbox) < 4;
      continue;
    }
    if (mode === "day") {
      checkbox.checked = task?.shift === "day";
      continue;
    }
    if (mode === "night") {
      checkbox.checked = task?.shift === "night";
    }
  }
}

async function refreshLiveState(options = {}) {
  if (!options.silent) {
    setStatus("route-status", "Обновляю текущее состояние техники...", "success");
  }
  try {
    const result = await requestJson("/api/live-state");
    state.liveState = result;
    renderOperationsMap(result);
    if (!state.currentPlayback && result.latest_replay?.playback?.frames?.length) {
      preparePlayback(result.latest_replay.playback, { autoplay: false });
    }
    if (!options.silent) {
      setStatus("route-status", "Текущее состояние обновлено.", "success");
    }
  } catch (error) {
    if (!options.silent) {
      setStatus("route-status", error.message, "error");
    }
  }
}

async function refreshAuditTrail() {
  setStatus("audit-status", "Загружаю журнал решений...", "success");
  try {
    const result = await requestJson("/api/audit/trail?limit=20");
    renderAuditTrail(result);
    setStatus("audit-status", "Журнал решений обновлен.", "success");
  } catch (error) {
    setStatus("audit-status", error.message, "error");
  }
}

async function clearAuditTrail() {
  setStatus("audit-status", "Очищаю журнал решений...", "success");
  try {
    await requestJson("/api/audit/trail", { method: "DELETE" });
    await refreshAuditTrail();
    setStatus("audit-status", "Журнал решений очищен.", "success");
  } catch (error) {
    setStatus("audit-status", error.message, "error");
  }
}

function updateRecommendationMode() {
  const mode = document.querySelector('input[name="recommendation-mode"]:checked').value;
  document.getElementById("custom-task-form").classList.toggle("hidden", mode !== "custom");
  document.getElementById("existing-task-field").classList.toggle("hidden", mode !== "existing");
}

async function runRecommendations() {
  setStatus("recommendations-status", "Подбираю технику под заявку...", "success");
  const mode = document.querySelector('input[name="recommendation-mode"]:checked').value;
  const payload = {
    strategy: document.getElementById("recommendation-strategy").value,
    top_k: Number(document.getElementById("recommendation-top-k").value || 3),
  };

  if (mode === "existing") {
    payload.task_id = document.getElementById("recommendation-task-id").value;
  } else {
    payload.task_id = document.getElementById("custom-task-id").value;
    payload.priority = document.getElementById("custom-priority").value;
    payload.destination_uwi = document.getElementById("custom-destination-uwi").value;
    payload.task_type = document.getElementById("custom-task-type").value;
    payload.planned_start = localDatetimeToIso(document.getElementById("custom-planned-start").value);
    payload.duration_hours = Number(document.getElementById("custom-duration-hours").value || 4);
    payload.shift = document.getElementById("custom-shift").value;
    payload.start_day = document.getElementById("custom-start-day").value;
  }

  try {
    const result = await requestJson("/api/recommendations", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.lastRecommendationDestination =
      payload.destination_uwi || findTaskById(result.task_id)?.destination_uwi || null;
    renderRecommendationsResult(result);
    await refreshAuditTrail();
    setStatus("recommendations-status", "Подбор техники завершен.", "success");
  } catch (error) {
    setStatus("recommendations-status", error.message, "error");
  }
}

function renderRecommendationsResult(result) {
  const container = document.getElementById("recommendations-result");
  if (!result.units?.length) {
    container.innerHTML = `<p class="muted">Система не вернула кандидатов.</p>`;
    return;
  }

  const taskContext = result.task_context || {};
  const warningMarkup = (result.warnings || [])
    .map((warning) => `<div class="warning-box">${warning}</div>`)
    .join("");
  const contextMarkup = `
    <div class="result-grid task-context">
      <div class="metric-box"><span>Заявка</span><strong>${result.task_id}</strong></div>
      <div class="metric-box"><span>Базовое время расчета</span><strong>${formatShortDate(result.reference_time ?? "-")}</strong></div>
      <div class="metric-box"><span>Приоритет</span><strong>${translatePriority(taskContext.priority ?? "-")}</strong></div>
      <div class="metric-box"><span>Тип работ</span><strong>${translateTaskType(taskContext.task_type ?? "-")}</strong></div>
      <div class="metric-box"><span>Смена / день</span><strong>${translateShift(taskContext.shift ?? "-")} / ${taskContext.start_day ?? "-"}</strong></div>
      <div class="metric-box"><span>Плановый старт</span><strong>${formatShortDate(taskContext.planned_start ?? "-")}</strong></div>
      <div class="metric-box"><span>Дедлайн SLA</span><strong>${formatShortDate(taskContext.sla_deadline ?? "-")}</strong></div>
    </div>
  `;

  const rows = result.units
    .map(
      (unit, index) => `
        <tr>
          <td><span class="pill">#${index + 1}</span></td>
          <td>${unit.wialon_id}<br /><small>${unit.name}</small></td>
          <td>${unit.vehicle_type}</td>
          <td>${unit.distance_km} km</td>
          <td>${unit.arrival_minutes} мин до приезда<br /><small>${unit.eta_minutes} мин до начала работ</small></td>
          <td>${unit.score}</td>
          <td>${unit.reason}</td>
          <td><button data-route-unit="${unit.wialon_id}" class="route-from-recommendation-btn">Маршрут</button></td>
        </tr>
      `,
    )
    .join("");

  container.innerHTML = `
    ${contextMarkup}
    ${warningMarkup}
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Место</th>
            <th>Машина</th>
            <th>Тип</th>
            <th>Пробег</th>
            <th>ETA</th>
            <th>Score</th>
            <th>Обоснование</th>
            <th>Действие</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
  `;

  container.querySelectorAll(".route-from-recommendation-btn").forEach((button) => {
    button.addEventListener("click", async () => {
      document.getElementById("route-vehicle-id").value = button.dataset.routeUnit;
      if (state.lastRecommendationDestination) {
        document.getElementById("route-well-uwi").value = state.lastRecommendationDestination;
      }
      await runRoute();
      window.scrollTo({ top: document.getElementById("route-result").offsetTop - 80, behavior: "smooth" });
    });
  });
}

async function runRoute() {
  setStatus("route-status", "Строю маршрут по графу дорог...", "success");
  const payload = {
    from: {
      wialon_id: Number(document.getElementById("route-vehicle-id").value),
    },
    to: {
      uwi: document.getElementById("route-well-uwi").value,
    },
  };
  const speed = document.getElementById("route-speed").value;
  if (speed) {
    payload.speed_kmph = Number(speed);
  }

  try {
    const result = await requestJson("/api/route", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    renderRouteResult(result);
    if (!state.wsConnected) {
      await refreshLiveState({ silent: true });
      await refreshAuditTrail();
    }
    setStatus("route-status", "Маршрут построен.", "success");
  } catch (error) {
    setStatus("route-status", error.message, "error");
  }
}

function renderRouteResult(result) {
  const container = document.getElementById("route-result");
  container.innerHTML = `
    <div class="result-grid">
      <div class="metric-box"><span>Длина маршрута</span><strong>${result.distance_km} km</strong></div>
      <div class="metric-box"><span>Время в пути</span><strong>${result.time_minutes} мин</strong></div>
      <div class="metric-box"><span>Количество узлов</span><strong>${result.nodes.length}</strong></div>
    </div>
  `;
  renderPolylineMap(result.coords, "route-map");
  if (state.operationsMapLayers?.activeRoute) {
    state.operationsMapLayers.activeRoute.clearLayers();
    renderLineOnLeaflet(state.operationsMapLayers.activeRoute, result.coords, "#2257d9", "Последний маршрут");
  }
}

function renderOperationsMap(liveState) {
  if (typeof L === "undefined") {
    return;
  }

  if (!state.operationsMap) {
    state.operationsMap = L.map("operations-map");
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: "&copy; OpenStreetMap contributors",
    }).addTo(state.operationsMap);
    state.operationsMapLayers = {
      vehicles: L.layerGroup().addTo(state.operationsMap),
      tasks: L.layerGroup().addTo(state.operationsMap),
      activeRoute: L.layerGroup().addTo(state.operationsMap),
      latestPlan: L.layerGroup().addTo(state.operationsMap),
      playback: L.layerGroup().addTo(state.operationsMap),
    };
  }

  const defaults = liveState.map_defaults || { lat: 51.64, lon: 68.07, zoom: 11 };
  if (!state.mapHasFittedBounds) {
    state.operationsMap.setView([defaults.lat, defaults.lon], defaults.zoom);
  }

  const { vehicles, tasks, latest_route: latestRoute, latest_plan: latestPlan } = liveState;
  state.operationsMapLayers.vehicles.clearLayers();
  state.operationsMapLayers.tasks.clearLayers();
  state.operationsMapLayers.latestPlan.clearLayers();
  state.operationsMapLayers.activeRoute.clearLayers();
  if (!state.currentPlayback) {
    state.operationsMapLayers.playback.clearLayers();
  }

  const bounds = [];
  if (!state.currentPlayback) {
    for (const vehicle of vehicles || []) {
      const marker = createVehicleLeafletMarker(
        vehicle.lat,
        vehicle.lon,
        "idle",
        shortVehicleLabel(vehicle.name),
        `<strong>${escapeHtml(vehicle.name)}</strong><br />Тип: ${escapeHtml(vehicle.vehicle_type)}<br />Свободна: ${escapeHtml(formatShortDate(vehicle.available_at))}`,
      );
      marker.addTo(state.operationsMapLayers.vehicles);
      bounds.push([vehicle.lat, vehicle.lon]);
    }
  } else {
    for (const vehicle of vehicles || []) {
      bounds.push([vehicle.lat, vehicle.lon]);
    }
  }

  for (const task of tasks || []) {
    const taskColor = task.priority === "high" ? "#c0392b" : task.priority === "medium" ? "#b87005" : "#2257d9";
    const well = (liveState.wells || []).find((item) => item.uwi === task.destination_uwi);
    if (!well) {
      continue;
    }
    const marker = L.circleMarker([well.lat, well.lon], {
      radius: 6,
      color: taskColor,
      fillColor: taskColor,
      fillOpacity: 0.75,
      weight: 2,
    }).bindPopup(
      `<strong>${escapeHtml(task.task_id)}</strong><br />Приоритет: ${escapeHtml(translatePriority(task.priority))}<br />Тип: ${escapeHtml(translateTaskType(task.task_type))}<br />Окно: ${escapeHtml(formatShortDate(task.planned_start))}`,
    );
    marker.addTo(state.operationsMapLayers.tasks);
    bounds.push([well.lat, well.lon]);
  }

  if (latestRoute?.coords) {
    renderLineOnLeaflet(state.operationsMapLayers.activeRoute, latestRoute.coords, "#2257d9", "Последний маршрут");
    for (const coord of latestRoute.coords) {
      bounds.push([coord[1], coord[0]]);
    }
  }

  if (latestPlan?.assignments) {
    const palette = ["#2257d9", "#1d8a52", "#b87005", "#7b3fe4", "#c0392b", "#0097a7"];
    latestPlan.assignments.forEach((assignment, assignmentIndex) => {
      const color = palette[assignmentIndex % palette.length];
      assignment.legs.forEach((leg) => {
        renderLineOnLeaflet(
          state.operationsMapLayers.latestPlan,
          leg.coords,
          color,
          `${assignment.vehicle_name}: заявка ${leg.task_id}`,
        );
        for (const coord of leg.coords) {
          bounds.push([coord[1], coord[0]]);
        }
      });
    });
  }

  if (!state.mapHasFittedBounds && bounds.length) {
    state.operationsMap.fitBounds(bounds, { padding: [24, 24] });
    state.mapHasFittedBounds = true;
  }

  if (!state.currentPlayback) {
    renderVehiclePanel(
      (vehicles || []).map((vehicle) => ({
        vehicle_id: vehicle.vehicle_id,
        name: vehicle.name,
        status: "idle",
        task_id: null,
        lat: vehicle.lat,
        lon: vehicle.lon,
      })),
      liveState.reference_time,
    );
    renderPlaybackMeta(null);
  }
}

function preparePlayback(playback, options = {}) {
  state.currentPlayback = playback;
  state.playbackIndex = 0;
  state.playbackSource = options.source || "local";
  state.playbackSpeed = Number(document.getElementById("playback-speed-select").value || 1);
  document.getElementById("pause-playback-btn").textContent = "Пауза";
  document.getElementById("playback-scrubber").max = String(Math.max(0, playback.frames.length - 1));
  document.getElementById("playback-scrubber").value = "0";
  stopPlaybackTimer();
  renderPlaybackFrame();
  if (options.autoplay) {
    state.playbackRunning = true;
    if (state.playbackSource === "local") {
      schedulePlaybackTimer();
    }
  } else {
    state.playbackRunning = false;
  }
}

function renderPlaybackFrame() {
  if (!state.operationsMapLayers?.playback || !state.currentPlayback) {
    return;
  }
  const frame = state.currentPlayback.frames[state.playbackIndex];
  if (!frame) {
    return;
  }
  state.operationsMapLayers.playback.clearLayers();
  for (const vehicle of frame.vehicles) {
    const marker = createVehicleLeafletMarker(
      vehicle.lat,
      vehicle.lon,
      vehicle.status,
      shortVehicleLabel(vehicle.name),
      `<strong>${escapeHtml(vehicle.name)}</strong><br />Статус: ${escapeHtml(translateStatus(vehicle.status))}<br />Заявка: ${escapeHtml(vehicle.task_id || "-")}<br />Кадр: ${escapeHtml(formatShortDate(frame.timestamp))}`,
    );
    marker.addTo(state.operationsMapLayers.playback);
  }
  document.getElementById("playback-scrubber").value = String(state.playbackIndex);
  renderPlaybackMeta(frame);
  renderVehiclePanel(frame.vehicles, frame.timestamp);
}

function renderLineOnLeaflet(layerGroup, coords, color, label) {
  if (!coords?.length) {
    return;
  }
  const polyline = L.polyline(
    coords.map((coord) => [coord[1], coord[0]]),
    { color, weight: 4, opacity: 0.85 },
  );
  if (label) {
    polyline.bindPopup(label);
  }
  polyline.addTo(layerGroup);
}

function schedulePlaybackTimer() {
  stopPlaybackTimer();
  if (!state.currentPlayback || !state.playbackRunning) {
    return;
  }
  const intervalMs = Math.max(180, 900 / state.playbackSpeed);
  state.playbackTimer = window.setInterval(() => {
    state.playbackIndex += 1;
    if (!state.currentPlayback || state.playbackIndex >= state.currentPlayback.frames.length) {
      resetPlayback({ keepFrame: true, keepPlaybackData: true });
      return;
    }
    renderPlaybackFrame();
  }, intervalMs);
}

function stopPlaybackTimer() {
  if (state.playbackTimer) {
    window.clearInterval(state.playbackTimer);
    state.playbackTimer = null;
  }
}

function updatePlaybackSpeed() {
  state.playbackSpeed = Number(document.getElementById("playback-speed-select").value || 1);
  if (state.playbackRunning && state.playbackSource === "local") {
    schedulePlaybackTimer();
  }
}

function togglePlaybackPause() {
  if (!state.currentPlayback) {
    return;
  }
  state.playbackRunning = !state.playbackRunning;
  document.getElementById("pause-playback-btn").textContent = state.playbackRunning ? "Пауза" : "Продолжить";
  if (state.playbackRunning && state.playbackSource === "local") {
    schedulePlaybackTimer();
  } else {
    stopPlaybackTimer();
  }
  renderPlaybackMeta(state.currentPlayback.frames[state.playbackIndex] || null);
}

function stepPlayback(direction) {
  if (!state.currentPlayback) {
    return;
  }
  state.playbackRunning = false;
  document.getElementById("pause-playback-btn").textContent = "Продолжить";
  stopPlaybackTimer();
  const nextIndex = Math.max(0, Math.min(state.currentPlayback.frames.length - 1, state.playbackIndex + direction));
  state.playbackIndex = nextIndex;
  renderPlaybackFrame();
}

function resetPlayback(options = {}) {
  state.playbackRunning = false;
  document.getElementById("pause-playback-btn").textContent = "Пауза";
  stopPlaybackTimer();
  if (state.currentPlayback && !options.keepFrame) {
    state.playbackIndex = 0;
    renderPlaybackFrame();
  }
  if (!options.keepPlaybackData) {
    state.currentPlayback = null;
    state.playbackSource = null;
    document.getElementById("playback-scrubber").max = "0";
    document.getElementById("playback-scrubber").value = "0";
    renderPlaybackMeta(null);
    if (state.operationsMapLayers?.playback) {
      state.operationsMapLayers.playback.clearLayers();
    }
    if (state.liveState) {
      renderOperationsMap(state.liveState);
    }
  }
}

function onPlaybackScrub(event) {
  if (!state.currentPlayback) {
    return;
  }
  state.playbackRunning = false;
  document.getElementById("pause-playback-btn").textContent = "Продолжить";
  stopPlaybackTimer();
  state.playbackIndex = Number(event.target.value || 0);
  renderPlaybackFrame();
}

function renderPlaybackMeta(frame) {
  const container = document.getElementById("playback-meta");
  if (!frame || !state.currentPlayback) {
    document.getElementById("playback-time-label").textContent = "нет проигрывания";
    container.innerHTML = `
      <div class="metric-box"><span>Состояние</span><strong>Ожидание</strong></div>
      <div class="metric-box"><span>Скорость</span><strong>${state.playbackSpeed.toFixed(1)}x</strong></div>
    `;
    return;
  }
  const driving = frame.vehicles.filter((item) => item.status === "driving").length;
  const servicing = frame.vehicles.filter((item) => item.status === "servicing").length;
  const waiting = frame.vehicles.filter((item) => item.status === "waiting" || item.status === "waiting_at_site").length;
  document.getElementById("playback-time-label").textContent = formatShortDate(frame.timestamp);
  container.innerHTML = `
    <div class="metric-box"><span>Кадр</span><strong>${state.playbackIndex + 1} / ${state.currentPlayback.frames.length}</strong></div>
    <div class="metric-box"><span>Скорость</span><strong>${state.playbackSpeed.toFixed(1)}x</strong></div>
    <div class="metric-box"><span>В пути</span><strong>${driving}</strong></div>
    <div class="metric-box"><span>На работе</span><strong>${servicing}</strong></div>
    <div class="metric-box"><span>Ожидают</span><strong>${waiting}</strong></div>
    <div class="metric-box"><span>Режим</span><strong>${state.playbackRunning ? "Автопроигрывание" : "Пауза"}</strong></div>
  `;
}

function renderVehiclePanel(vehicles, timestamp) {
  const container = document.getElementById("active-vehicles-panel");
  if (!vehicles?.length) {
    container.innerHTML = `<p class="muted">Нет данных по машинам.</p>`;
    return;
  }
  const sorted = [...vehicles].sort((left, right) => playbackStatusOrder(left.status) - playbackStatusOrder(right.status));
  container.innerHTML = sorted
    .map(
      (vehicle) => `
        <article class="vehicle-card">
          <div class="vehicle-card-title">
            <strong>${escapeHtml(vehicle.name)}</strong>
            <span class="status-badge status-${escapeHtml(vehicle.status)}">${escapeHtml(translateStatus(vehicle.status))}</span>
          </div>
          <div class="vehicle-card-meta">
            <div>ID: ${escapeHtml(String(vehicle.vehicle_id))}</div>
            <div>Заявка: ${escapeHtml(vehicle.task_id || "-")}</div>
            <div>Время кадра: ${escapeHtml(formatShortDate(timestamp))}</div>
          </div>
        </article>
      `,
    )
    .join("");
}

function createVehicleLeafletMarker(lat, lon, status, label, popupHtml) {
  return L.marker([lat, lon], {
    icon: L.divIcon({
      className: "vehicle-marker-wrapper",
      html: `<div class="vehicle-marker status-${escapeHtml(status)}"><span>${escapeHtml(label)}</span></div>`,
      iconSize: [42, 20],
      iconAnchor: [21, 10],
    }),
  }).bindPopup(popupHtml);
}

function shortVehicleLabel(name) {
  const match = /\d+/.exec(name || "");
  if (match) {
    return `ТС ${match[0]}`;
  }
  return "ТС";
}

function playbackStatusOrder(status) {
  return {
    driving: 0,
    servicing: 1,
    waiting_at_site: 2,
    waiting: 3,
    idle: 4,
    completed: 5,
  }[status] ?? 99;
}

async function runMultitask() {
  const taskIds = [...document.querySelectorAll("#multitask-checkboxes input:checked")].map((item) => item.value);
  if (!taskIds.length) {
    setStatus("multitask-status", "Нужно выбрать хотя бы одну заявку.", "error");
    return;
  }

  setStatus("multitask-status", "Оцениваю группировку заявок...", "success");
  const payload = {
    task_ids: taskIds,
    constraints: {
      max_total_time_minutes: Number(document.getElementById("multitask-max-time").value || 480),
      max_detour_ratio: Number(document.getElementById("multitask-detour").value || 1.3),
    },
  };

  try {
    const result = await requestJson("/api/multitask", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    renderMultitaskResult(result);
    if (!state.wsConnected) {
      await refreshAuditTrail();
    }
    setStatus("multitask-status", "Группировка рассчитана.", "success");
  } catch (error) {
    setStatus("multitask-status", error.message, "error");
  }
}

function renderMultitaskResult(result) {
  const container = document.getElementById("multitask-result");
  const metrics = `
    <div class="result-grid">
      <div class="metric-box"><span>Стратегия</span><strong>${translateStrategySummary(result.strategy_summary)}</strong></div>
      <div class="metric-box"><span>Выбрано заявок</span><strong>${result.selected_task_ids?.length ?? result.groups.length}</strong></div>
      <div class="metric-box"><span>Общий пробег</span><strong>${result.total_distance_km} km</strong></div>
      <div class="metric-box"><span>Baseline пробег</span><strong>${result.baseline_distance_km} km</strong></div>
      <div class="metric-box"><span>Экономия</span><strong>${result.savings_percent}%</strong></div>
    </div>
  `;
  const groups = result.groups
    .map(
      (group, index) => `
        <article class="group-card">
          <h3>Группа ${index + 1}</h3>
          <p>${group.join(", ")}</p>
        </article>
      `,
    )
    .join("");

  container.innerHTML = `
    ${metrics}
    <div class="muted" style="margin-top: 1rem;">
      ${result.reason}<br />
      ограничения: время <= ${result.constraints?.max_total_time_minutes ?? "-"} мин, коэффициент крюка <= ${result.constraints?.max_detour_ratio ?? "-"}x<br />
      базовое время расчета: ${formatShortDate(result.reference_time ?? "-")}
    </div>
    ${groups}
  `;
}

async function runPlan() {
  setStatus("plan-status", "Строю общий план назначений...", "success");
  const payload = {
    strategy: document.getElementById("plan-strategy").value,
  };

  try {
    const result = await requestJson("/api/plan", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    renderPlanResult(result);
    if (!state.wsConnected) {
      await refreshLiveState({ silent: true });
      await refreshAuditTrail();
    }
    setStatus("plan-status", "План назначений готов.", "success");
  } catch (error) {
    setStatus("plan-status", error.message, "error");
  }
}

function renderPlanResult(result) {
  const container = document.getElementById("plan-result");
  const metrics = result.metrics || {};
  const comparison = result.comparison_vs_baseline || {};
  const comparisonCards = Object.entries(comparison)
    .filter(([metric]) =>
      ["total_distance_km", "weighted_lateness", "assignment_rate", "high_priority_on_time_rate"].includes(metric),
    )
    .map(
      ([metric, value]) => `
        <div class="metric-box">
          <span>${humanizeMetric(metric)}</span>
          <strong>${value.delta > 0 ? "+" : ""}${value.delta}</strong>
          <small>${value.improved ? "лучше" : "хуже"} относительно baseline</small>
        </div>
      `,
    )
    .join("");

  const assignments = result.assignments
    .map(
      (assignment) => `
        <article class="assignment-card">
          <h3>${assignment.vehicle_name} (${assignment.vehicle_id})</h3>
          <p><strong>Заявки:</strong> ${assignment.task_ids.join(", ")}</p>
          <p><strong>Пробег:</strong> ${assignment.total_distance_km} km</p>
          <p><strong>Время в пути:</strong> ${assignment.total_travel_minutes} мин</p>
          <p><strong>Окно:</strong> ${formatShortDate(assignment.started_at)} -> ${formatShortDate(assignment.finished_at)}</p>
          ${assignment.explanation ? `<p><strong>Пояснение:</strong> ${assignment.explanation}</p>` : ""}
        </article>
      `,
    )
    .join("");

  container.innerHTML = `
    <div class="result-grid">
      <div class="metric-box"><span>Алгоритм</span><strong>${translateStrategy(result.strategy)}</strong></div>
      <div class="metric-box"><span>Назначений</span><strong>${result.assignments.length}</strong></div>
      <div class="metric-box"><span>Покрытие заявок</span><strong>${formatPercent(metrics.assignment_rate)}</strong></div>
      <div class="metric-box"><span>Пробег</span><strong>${metrics.total_distance_km ?? "-"} km</strong></div>
      <div class="metric-box"><span>Взвешенная просрочка</span><strong>${metrics.weighted_lateness ?? "-"}</strong></div>
      <div class="metric-box"><span>Без назначения</span><strong>${result.unassigned_task_ids.length}</strong></div>
    </div>
    <div class="muted" style="margin-top: 1rem;">${result.summary}</div>
    ${comparisonCards ? `<div class="result-grid" style="margin-top: 1rem;">${comparisonCards}</div>` : ""}
    ${assignments || `<p class="muted" style="margin-top: 1rem;">Система не вернула назначений.</p>`}
  `;
}

async function runBenchmark() {
  setStatus("benchmark-status", "Запускаю бенчмарк и проигрывание сценария...", "success");
  const payload = {
    scenarios: Number(document.getElementById("benchmark-scenarios").value || 250),
    min_tasks: Number(document.getElementById("benchmark-min-tasks").value || 6),
    max_tasks: Number(document.getElementById("benchmark-max-tasks").value || 12),
    min_vehicles: Number(document.getElementById("benchmark-min-vehicles").value || 4),
    max_vehicles: Number(document.getElementById("benchmark-max-vehicles").value || 7),
    seed: Number(document.getElementById("benchmark-seed").value || 42),
  };
  const playbackStrategy = document.getElementById("map-playback-strategy").value;

  runReplay(playbackStrategy, { silentStatus: false }).catch((_error) => {
    // Benchmark should continue even if replay request fails.
  });

  try {
    const result = await requestJson("/api/benchmark/run", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    renderBenchmarkResult(result);
    if (!state.wsConnected) {
      await refreshAuditTrail();
    }
    setStatus("benchmark-status", "Бенчмарк завершен.", "success");
  } catch (error) {
    setStatus("benchmark-status", error.message, "error");
  }
}

async function runReplayOnly() {
  const playbackStrategy = document.getElementById("map-playback-strategy").value;
  await runReplay(playbackStrategy);
}

async function runReplay(strategy, options = {}) {
  if (!options.silentStatus) {
    setStatus("benchmark-status", `Готовлю анимацию движения: ${translateStrategy(strategy)}...`, "success");
  }
  const payload = {
    strategy,
    frame_interval_minutes: 5,
  };
  const result = await requestJson("/api/replay/run", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  if (!state.wsConnected) {
    preparePlayback(result.playback, { autoplay: true, source: "local" });
    await refreshAuditTrail();
  }
  if (!options.silentStatus) {
    setStatus("benchmark-status", `Анимация движения запущена: ${translateStrategy(strategy)}.`, "success");
  }
  return result;
}

async function runTuning() {
  setStatus("tuning-status", "Подбираю веса скоринга...", "success");
  const payload = {
    candidate_limit: Number(document.getElementById("tuning-candidate-limit").value || 12),
  };
  try {
    const result = await requestJson("/api/tuning/run", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    renderTuningResult(result);
    if (!state.wsConnected) {
      await refreshAuditTrail();
    }
    setStatus("tuning-status", "Подбор весов завершен.", "success");
  } catch (error) {
    setStatus("tuning-status", error.message, "error");
  }
}

function renderBenchmarkResult(result) {
  const container = document.getElementById("benchmark-result");
  const latestCsvUrl = "/api/benchmark/reports/latest.csv";
  const rows = Object.entries(result.strategies)
    .map(([strategy, metrics]) => {
      const distanceDelta = metrics.comparison_vs_baseline?.total_distance_km?.delta_percent;
      const latenessDelta = metrics.comparison_vs_baseline?.weighted_lateness?.delta_percent;
      return `
        <tr>
          <td>${translateStrategy(strategy)}</td>
          <td>${metrics.total_distance_km}</td>
          <td>${metrics.weighted_lateness}</td>
          <td>${metrics.high_priority_on_time_rate}</td>
          <td>${metrics.runtime_ms}</td>
          <td>${distanceDelta == null ? "-" : `${distanceDelta}%`}</td>
          <td>${latenessDelta == null ? "-" : `${latenessDelta}%`}</td>
        </tr>
      `;
    })
    .join("");

  container.innerHTML = `
    <div class="result-grid">
      <div class="metric-box"><span>Сценариев</span><strong>${result.scenarios}</strong></div>
      <div class="metric-box"><span>Лучший по пробегу</span><strong>${translateStrategy(result.best_by_metric.distance)}</strong></div>
      <div class="metric-box"><span>Лучший по просрочке</span><strong>${translateStrategy(result.best_by_metric.weighted_lateness)}</strong></div>
      <div class="metric-box"><span>Самый быстрый</span><strong>${translateStrategy(result.best_by_metric.runtime)}</strong></div>
      <div class="metric-box"><span>ID отчета</span><strong>${result.report_id ?? "-"}</strong></div>
    </div>
    <div class="actions" style="margin-top: 1rem;">
      <a class="secondary-button small-button" href="${latestCsvUrl}" target="_blank" rel="noreferrer">Скачать последний CSV</a>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Алгоритм</th>
            <th>Средний пробег, km</th>
            <th>Взвешенная просрочка</th>
            <th>High-priority вовремя</th>
            <th>Время расчета, ms</th>
            <th>Пробег vs baseline</th>
            <th>Просрочка vs baseline</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <pre class="json-box">${escapeHtml(JSON.stringify(result, null, 2))}</pre>
  `;
}

function renderTuningResult(result) {
  const container = document.getElementById("tuning-result");
  const best = result.best_candidate;
  const rows = result.leaderboard
    .slice(0, 5)
    .map(
      (candidate) => `
        <tr>
          <td>${candidate.candidate_id}</td>
          <td>${candidate.objective}</td>
          <td>${candidate.metrics.total_distance_km}</td>
          <td>${candidate.metrics.weighted_lateness}</td>
          <td>${candidate.metrics.assignment_rate}</td>
        </tr>
      `,
    )
    .join("");
  container.innerHTML = `
    <div class="result-grid">
      <div class="metric-box"><span>ID отчета</span><strong>${result.report_id ?? "-"}</strong></div>
      <div class="metric-box"><span>Лучший кандидат</span><strong>${best.candidate_id}</strong></div>
      <div class="metric-box"><span>Целевая функция</span><strong>${best.objective}</strong></div>
    </div>
    <pre class="json-box">${escapeHtml(JSON.stringify(best.weights, null, 2))}</pre>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Кандидат</th>
            <th>Цель</th>
            <th>Пробег</th>
            <th>Просрочка</th>
            <th>Покрытие</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
  `;
}

function renderAuditTrail(result) {
  const container = document.getElementById("audit-result");
  if (!result.events?.length) {
    container.innerHTML = `<p class="muted">Журнал пока пуст.</p>`;
    return;
  }

  const cards = result.events
    .map(
      (event) => `
        <article class="audit-card">
          <h3>${escapeHtml(translateAction(event.action))}${event.strategy ? ` / ${escapeHtml(translateStrategy(event.strategy))}` : ""}</h3>
          <p><strong>${escapeHtml(event.summary)}</strong></p>
          <p class="muted">время: ${escapeHtml(formatShortDate(event.timestamp))}</p>
          <details>
            <summary>Запрос</summary>
            <pre>${escapeHtml(JSON.stringify(event.request, null, 2))}</pre>
          </details>
          <details>
            <summary>Ответ</summary>
            <pre>${escapeHtml(JSON.stringify(event.response, null, 2))}</pre>
          </details>
        </article>
      `,
    )
    .join("");

  container.innerHTML = cards;
}

function renderPolylineMap(coords, containerId) {
  const container = document.getElementById(containerId);
  if (!coords?.length) {
    container.textContent = "Координаты для отображения не получены.";
    container.classList.add("empty-state");
    return;
  }

  const xs = coords.map((point) => point[0]);
  const ys = coords.map((point) => point[1]);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const width = Math.max(maxX - minX, 0.000001);
  const height = Math.max(maxY - minY, 0.000001);
  const padding = 10;

  const normalized = coords.map(([x, y]) => {
    const px = padding + ((x - minX) / width) * (100 - padding * 2);
    const py = 100 - padding - ((y - minY) / height) * (100 - padding * 2);
    return [px, py];
  });

  const polyline = normalized.map(([x, y]) => `${x},${y}`).join(" ");
  const pointsMarkup = normalized
    .map(
      ([x, y], index) => `
        <circle cx="${x}" cy="${y}" r="${index === 0 || index === normalized.length - 1 ? 2.5 : 1.7}" fill="${index === 0 ? "#1d8a52" : index === normalized.length - 1 ? "#c0392b" : "#2257d9"}" />
      `,
    )
    .join("");

  container.classList.remove("empty-state");
  container.innerHTML = `
    <svg viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-label="Route preview">
      <rect x="0" y="0" width="100" height="100" rx="6" fill="#f8fbff"></rect>
      <polyline fill="none" stroke="#2257d9" stroke-width="1.8" points="${polyline}"></polyline>
      ${pointsMarkup}
    </svg>
  `;
}

function setStatus(elementId, message, tone) {
  const node = document.getElementById(elementId);
  node.textContent = message || "";
  node.classList.remove("error", "success");
  if (tone) {
    node.classList.add(tone);
  }
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
    ...options,
  });

  if (!response.ok) {
    if (response.status === 401) {
      window.location.href = `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
      throw new Error("Требуется вход.");
    }
    let message = `Request failed with status ${response.status}`;
    try {
      const error = await response.json();
      message = error.detail || message;
    } catch (_error) {
      message = await response.text();
    }
    throw new Error(message);
  }

  return response.json();
}

function findTaskById(taskId) {
  return state.catalog?.tasks.find((task) => task.task_id === taskId) || null;
}

function formatShortDate(value) {
  if (!value || value === "-") {
    return "-";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString();
}

function formatPercent(value) {
  if (typeof value !== "number") {
    return "-";
  }
  return `${(value * 100).toFixed(1)}%`;
}

function humanizeMetric(metric) {
  return {
    assignment_rate: "Покрытие заявок",
    unassigned_tasks: "Без назначения",
    total_distance_km: "Дельта пробега, km",
    total_travel_minutes: "Дельта времени, мин",
    weighted_lateness: "Дельта просрочки",
    high_priority_on_time_rate: "High-priority вовремя",
    runtime_ms: "Дельта runtime, ms",
  }[metric] || metric;
}

function translateStrategy(strategy) {
  return STRATEGY_LABELS[strategy] || strategy;
}

function translatePriority(priority) {
  return PRIORITY_LABELS[priority] || priority;
}

function translateShift(shift) {
  return SHIFT_LABELS[shift] || shift;
}

function translateTaskType(taskType) {
  return TASK_TYPE_LABELS[taskType] || taskType;
}

function translateStatus(status) {
  return STATUS_LABELS[status] || status;
}

function translateDatasetMode(mode) {
  return {
    sample: "demo / sample",
    directory: "CSV / файлы",
    postgres: "PostgreSQL",
  }[mode] || mode;
}

function translateStrategySummary(summary) {
  return {
    separate: "Раздельное обслуживание",
    mixed: "Смешанная группировка",
    single_unit: "Один выезд одной машины",
  }[summary] || summary;
}

function translateAction(action) {
  return {
    recommendation: "Рекомендация",
    route: "Маршрут",
    multitask: "Группировка",
    plan: "План",
    benchmark: "Бенчмарк",
    replay: "Проигрывание",
    tuning: "Подбор весов",
    dataset_reload: "Перезагрузка данных",
  }[action] || action;
}

function localDatetimeToIso(value) {
  if (!value) {
    return value;
  }
  return value.length === 16 ? `${value}:00` : value;
}

function escapeHtml(value) {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}
