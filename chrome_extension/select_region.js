(() => {
  if (window.__orocopySelecting) return;
  window.__orocopySelecting = true;
  const overlay = document.createElement("div");
  overlay.style.cssText = "position:fixed;inset:0;z-index:2147483647;background:rgba(3,10,16,.22);cursor:crosshair;touch-action:none";
  const banner = document.createElement("div");
  banner.style.cssText = "position:absolute;top:12px;left:50%;transform:translateX(-50%);padding:9px 14px;border-radius:6px;background:#10151b;color:#fff;font:600 14px Segoe UI,sans-serif;box-shadow:0 2px 12px #0008;white-space:nowrap";
  const box = document.createElement("div");
  box.style.cssText = "position:absolute;border:2px solid #57d99a;background:rgba(87,217,154,.16);display:none;pointer-events:none";
  overlay.append(banner, box);
  document.documentElement.appendChild(overlay);

  const viewportWidth = window.innerWidth;
  const viewportHeight = window.innerHeight;
  const crops = {};
  let side = "buy";
  let start = null;
  const updateBanner = () => {
    banner.textContent = side === "buy"
      ? "OroCopy 1/2: arrastra la zona COMPRAS (incluye POSICIONES y su número) · Esc cancela"
      : "OroCopy 2/2: arrastra la zona VENTAS (incluye POSICIONES y su número) · Esc cancela";
  };
  updateBanner();

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
    start = null;
    if (width < 30 || height < 16) {
      banner.textContent = "Zona muy pequeña; vuelve a arrastrar e incluye POSICIONES con su número";
      box.style.display = "none";
      setTimeout(updateBanner, 1600);
      return;
    }
    crops[side] = {
      x: left / viewportWidth, y: top / viewportHeight,
      width: width / viewportWidth, height: height / viewportHeight
    };
    if (side === "buy") {
      side = "sell";
      updateBanner();
      box.style.display = "none";
      return;
    }
    cleanup();
    chrome.runtime.sendMessage({
      type: "REGIONS_SELECTED",
      title: document.title,
      regions: crops
    });
  });
})();
