async function ensureOffscreenDocument() {
  const exists = await chrome.offscreen.hasDocument();
  if (exists) return;
  await chrome.offscreen.createDocument({
    url: "offscreen.html",
    reasons: ["USER_MEDIA"],
    justification: "Mantener la captura de la pestaña autorizada y enviar fotogramas a OroCopy."
  });
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "REGION_SELECTED") {
    chrome.storage.local.set({ crop: message.crop, cropTabId: _sender.tab?.id,
                               cropTabTitle: message.title || "Pestaña capturada" });
    return;
  }
  if (message?.type === "REGION_CANCELLED") {
    chrome.storage.local.set({ crop: null, cropTabId: null });
    return;
  }
  if (message?.type === "PREPARE_CAPTURE") {
    ensureOffscreenDocument().then(() => sendResponse({ ok: true }))
      .catch(error => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  if (message?.type === "START_CAPTURE") {
    ensureOffscreenDocument().then(() => chrome.runtime.sendMessage({
      target: "offscreen",
      type: "START_CAPTURE",
      streamId: message.streamId,
      endpoint: message.endpoint,
      token: message.token,
      crop: message.crop
    })).then(result => sendResponse(result || { ok: false, error: "El lector no respondió." }))
      .catch(error => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  if (message?.type === "STOP_CAPTURE") {
    chrome.runtime.sendMessage({ target: "offscreen", type: "STOP_CAPTURE" })
      .then(result => sendResponse(result || { ok: true }))
      .catch(() => sendResponse({ ok: true }));
    return true;
  }
});
