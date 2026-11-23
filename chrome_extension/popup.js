const endpoint = document.getElementById("endpoint");
const token = document.getElementById("token");
const status = document.getElementById("status");
const cropStatus = document.getElementById("crop");
const startButton = document.getElementById("start");
const stopButton = document.getElementById("stop");
const selectButton = document.getElementById("select");

function setStatus(text, error = false) {
  status.textContent = text;
  status.style.color = error ? "#ff7777" : "#57d99a";
}

async function loadSettings() {
  const saved = await chrome.storage.local.get(["endpoint", "token", "running", "lastError", "crop", "cropTabTitle"]);
  endpoint.value = saved.endpoint || "http://127.0.0.1:39872/frame";
  token.value = saved.token || "";
  cropStatus.textContent = saved.crop
    ? `Zona guardada: ${saved.cropTabTitle || "pestaña"} · ${Math.round(saved.crop.width * 100)}% × ${Math.round(saved.crop.height * 100)}%`
    : "Zona: no seleccionada";
  if (saved.lastError) setStatus(saved.lastError, true);
  else if (saved.running) setStatus("Capturando la pestaña autorizada");
}

selectButton.addEventListener("click", async () => {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.id || !/^https?:/i.test(tab.url || "")) {
      throw new Error("Activa primero la pestaña del directo.");
    }
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["select_region.js"] });
    cropStatus.textContent = "Arrastra en la pestaña la zona de ambos contadores.";
    setStatus("Selecciona el recuadro en la página; el popup puede cerrarse.");
  } catch (error) {
    setStatus(String(error.message || error), true);
  }
});

async function saveSettings() {
  const url = new URL(endpoint.value.trim());
  if (url.origin !== "http://127.0.0.1:39872" || url.pathname !== "/frame") {
    throw new Error("La dirección debe ser http://127.0.0.1:39872/frame");
  }
  if (token.value.trim().length < 32) throw new Error("Pega el token que entrega OroCopy.");
  await chrome.storage.local.set({ endpoint: url.href, token: token.value.trim() });
  return { endpoint: url.href, token: token.value.trim() };
}

startButton.addEventListener("click", async () => {
  startButton.disabled = true;
  try {
    const settings = await saveSettings();
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.id || !/^https?:/i.test(tab.url || "")) {
      throw new Error("Activa primero la pestaña del directo.");
    }
    const saved = await chrome.storage.local.get(["crop", "cropTabId"]);
    if (!saved.crop || saved.cropTabId !== tab.id) {
      throw new Error("Selecciona primero la zona en esta pestaña.");
    }
    // Request the stream immediately from the popup click handler so Chrome
    // still recognizes the user gesture required by tabCapture.
    const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
    const prepared = await chrome.runtime.sendMessage({ type: "PREPARE_CAPTURE" });
    if (!prepared?.ok) throw new Error(prepared?.error || "No se pudo preparar la captura.");
    const result = await chrome.runtime.sendMessage({
      type: "START_CAPTURE", streamId, endpoint: settings.endpoint,
      token: settings.token, crop: saved.crop
    });
    if (!result?.ok) throw new Error(result?.error || "No se pudo iniciar el flujo de video.");
    await chrome.storage.local.set({ running: true, lastError: "" });
    setStatus("Capturando esta pestaña · envíos cada 0,65 s");
  } catch (error) {
    setStatus(String(error.message || error), true);
  } finally {
    startButton.disabled = false;
  }
});

stopButton.addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ type: "STOP_CAPTURE" });
  await chrome.storage.local.set({ running: false });
  setStatus("Captura detenida");
});

loadSettings();
