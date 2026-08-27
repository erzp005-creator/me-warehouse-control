export function registerWarehouseServiceWorker() {
  if (!import.meta.env.PROD || !('serviceWorker' in navigator)) return;
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => {
      // PWA support is progressive; a failed registration must never block
      // warehouse work or replace the normal online application.
    });
  }, { once: true });
}
