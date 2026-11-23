let stream = null;
let timer = null;
let sending = false;
let crop = null;

const video = document.getElementById("capture");
const canvas = document.getElementById("frame");

async function stopCapture() {
  if (timer !== null) clearInterval(timer);
  timer = null;
  if (stream) stream.getTracks().forEach(track => track.stop());
  stream = null;
  video.srcObject = null;
  sending = false;
  await chrome.storage.local.set({ running: false });
}

async function sendFrame(endpoint, token) {
  if (sending || !stream || video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) return;
  sending = true;
  try {
    const width = video.videoWidth;
    const height = video.videoHeight;
    if (!width || !height) return;
    const sx = Math.max(0, Math.floor(crop.x * width));
    const sy = Math.max(0, Math.floor(crop.y * height));
    const sw = Math.max(1, Math.min(width - sx, Math.ceil(crop.width * width)));
    const sh = Math.max(1, Math.min(height - sy, Math.ceil(crop.height * height)));
    canvas.width = sw;
    canvas.height = sh;
    const context = canvas.getContext("2d", { alpha: false });
    context.drawImage(video, sx, sy, sw, sh, 0, 0, sw, sh);
    const blob = await new Promise(resolve => canvas.toBlob(resolve, "image/jpeg", 0.86));
    if (!blob) return;
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "image/jpeg", "X-OroCopy-Token": token },
      body: blob,
      cache: "no-store"
    });
    if (!response.ok) throw new Error(`OroCopy respondió ${response.status}`);
    await chrome.storage.local.set({ lastError: "", lastFrameAt: Date.now() });
  } catch (error) {
    await chrome.storage.local.set({ lastError: String(error), running: false });
    await stopCapture();
  } finally {
    sending = false;
  }
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.target !== "offscreen") return;
  if (message.type === "START_CAPTURE") {
    stopCapture().then(async () => {
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          audio: false,
          video: {
            mandatory: {
              chromeMediaSource: "tab",
              chromeMediaSourceId: message.streamId,
              maxWidth: 1920,
              maxHeight: 1080
            }
          }
        });
        crop = message.crop;
        if (!crop || crop.x < 0 || crop.y < 0 || crop.width <= 0 || crop.height <= 0 ||
            crop.x + crop.width > 1 || crop.y + crop.height > 1) {
          throw new Error("La zona de captura no es válida.");
        }
        const track = stream.getVideoTracks()[0];
        track.addEventListener("ended", () => stopCapture());
        video.srcObject = stream;
        await video.play();
        timer = setInterval(() => sendFrame(message.endpoint, message.token), 650);
        await chrome.storage.local.set({ running: true, lastError: "" });
        sendResponse({ ok: true });
      } catch (error) {
        await stopCapture();
        sendResponse({ ok: false, error: String(error) });
      }
    });
    return true;
  }
  if (message.type === "STOP_CAPTURE") {
    stopCapture().then(() => sendResponse({ ok: true }));
    return true;
  }
});
