let stream = null;
let timer = null;
let sending = false;
let regions = null;

const video = document.getElementById("capture");
const canvas = document.getElementById("frame");

function reportState(patch) {
  chrome.runtime.sendMessage({ type: "CAPTURE_STATE", patch }).catch(() => {});
}

async function stopCapture() {
  if (timer !== null) clearInterval(timer);
  timer = null;
  if (stream) stream.getTracks().forEach(track => track.stop());
  stream = null;
  video.srcObject = null;
  sending = false;
  reportState({ running: false });
}

async function sendFrame(endpoint, token) {
  if (sending || !stream || video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) return;
  sending = true;
  try {
    const width = video.videoWidth;
    const height = video.videoHeight;
    if (!width || !height) return;
    const pixelRect = crop => {
      if (!crop || crop.x < 0 || crop.y < 0 || crop.width <= 0 || crop.height <= 0 ||
          crop.x + crop.width > 1 || crop.y + crop.height > 1) {
        throw new Error("Las zonas de captura no son válidas.");
      }
      const x = Math.max(0, Math.floor(crop.x * width));
      const y = Math.max(0, Math.floor(crop.y * height));
      return {
        x, y,
        width: Math.max(1, Math.min(width - x, Math.ceil(crop.width * width))),
        height: Math.max(1, Math.min(height - y, Math.ceil(crop.height * height)))
      };
    };
    const buy = pixelRect(regions?.buy);
    const sell = pixelRect(regions?.sell);
    canvas.width = buy.width + sell.width;
    canvas.height = Math.max(buy.height, sell.height);
    const context = canvas.getContext("2d", { alpha: false });
    context.fillStyle = "#000";
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(video, buy.x, buy.y, buy.width, buy.height,
      0, 0, buy.width, buy.height);
    context.drawImage(video, sell.x, sell.y, sell.width, sell.height,
      buy.width, 0, sell.width, sell.height);
    const blob = await new Promise(resolve => canvas.toBlob(resolve, "image/jpeg", 0.86));
    if (!blob) return;
    const response = await fetch(endpoint, {
      method: "POST",
      headers: {
        "Content-Type": "image/jpeg",
        "X-OroCopy-Token": token,
        "X-OroCopy-Buy-Width": String(buy.width),
        "X-OroCopy-Buy-Height": String(buy.height),
        "X-OroCopy-Sell-Width": String(sell.width),
        "X-OroCopy-Sell-Height": String(sell.height)
      },
      body: blob,
      cache: "no-store"
    });
    if (!response.ok) throw new Error(`OroCopy respondió ${response.status}`);
    reportState({ lastError: "", lastFrameAt: Date.now() });
  } catch (error) {
    reportState({ lastError: String(error), running: false });
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
        regions = message.regions;
        if (!regions?.buy || !regions?.sell) throw new Error("Selecciona las zonas COMPRAS y VENTAS.");
        const track = stream.getVideoTracks()[0];
        track.addEventListener("ended", () => stopCapture());
        video.srcObject = stream;
        await video.play();
        timer = setInterval(() => sendFrame(message.endpoint, message.token), 650);
        reportState({ running: true, lastError: "" });
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
