(() => {
  if (window.__orocopySelecting) return;
  window.__orocopySelecting = true;
  const overlay = document.createElement("div");
  overlay.style.cssText = "position:fixed;inset:0;z-index:2147483647;background:rgba(3,10,16,.22);cursor:crosshair;touch-action:none";
  const banner = document.createElement("div");
  banner.textContent = "OroCopy: arrastra el área con COMPRAS, VENTAS y sus posiciones · Esc cancela";
  banner.style.cssText = "position:absolute;top:12px;left:50%;transform:translateX(-50%);padding:9px 14px;border-radius:6px;background:#10151b;color:#fff;font:600 14px Segoe UI,sans-serif;box-shadow:0 2px 12px #0008;white-space:nowrap";
  const box = document.createElement("div");
  box.style.cssText = "position:absolute;border:2px solid #57d99a;background:rgba(87,217,154,.16);display:none;pointer-events:none";
  overlay.append(banner, box);
  document.documentElement.appendChild(overlay);
  let start = null;
  const cleanup = () => {
    overlay.remove();
    window.removeEventListener("keydown", onKey, true);
    window.__orocopySelecting = false;
  };
  const onKey = event => {
    if (event.key === "Escape") {
      event.preventDefault(); event.stopPropagation(); cleanup();
      chrome.runtime.sendMessage({ type: "REGION_CANCELLED" });
    }
  };
  window.addEventListener("keydown", onKey, true);
  overlay.addEventListener("pointerdown", event => {
    if (event.target === banner) return;
    event.preventDefault();
    start = { x: event.clientX, y: event.clientY };
    overlay.setPointerCapture(event.pointerId);
    box.style.display = "block";
    box.style.left = `${start.x}px`; box.style.top = `${start.y}px`;
    box.style.width = "0"; box.style.height = "0";
  });
  overlay.addEventListener("pointermove", event => {
    if (!start) return;
    const x = Math.min(start.x, event.clientX), y = Math.min(start.y, event.clientY);
    box.style.left = `${x}px`; box.style.top = `${y}px`;
    box.style.width = `${Math.abs(event.clientX - start.x)}px`;
    box.style.height = `${Math.abs(event.clientY - start.y)}px`;
  });
  overlay.addEventListener("pointerup", event => {
    if (!start) return;
    const left = Math.min(start.x, event.clientX), top = Math.min(start.y, event.clientY);
    const width = Math.abs(event.clientX - start.x), height = Math.abs(event.clientY - start.y);
    const viewportWidth = window.innerWidth, viewportHeight = window.innerHeight;
    cleanup();
    if (width < 30 || height < 16) {
      chrome.runtime.sendMessage({ type: "REGION_CANCELLED" });
      return;
    }
    chrome.runtime.sendMessage({
      type: "REGION_SELECTED",
      title: document.title,
      crop: { x: left / viewportWidth, y: top / viewportHeight,
              width: width / viewportWidth, height: height / viewportHeight }
    });
  });
})();
