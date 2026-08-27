import { useEffect, useState } from 'react';

function standaloneMode() {
  return window.matchMedia?.('(display-mode: standalone)').matches || window.navigator.standalone === true;
}

function iosDevice() {
  return /iphone|ipad|ipod/i.test(window.navigator.userAgent);
}

export default function PwaStatus() {
  const [online, setOnline] = useState(() => navigator.onLine);
  const [installPrompt, setInstallPrompt] = useState(null);
  const [installed, setInstalled] = useState(standaloneMode);
  const [dismissed, setDismissed] = useState(false);

  useEffect(() => {
    const wentOnline = () => setOnline(true);
    const wentOffline = () => setOnline(false);
    const canInstall = (event) => {
      event.preventDefault();
      setInstallPrompt(event);
      setDismissed(false);
    };
    const didInstall = () => {
      setInstalled(true);
      setInstallPrompt(null);
    };

    window.addEventListener('online', wentOnline);
    window.addEventListener('offline', wentOffline);
    window.addEventListener('beforeinstallprompt', canInstall);
    window.addEventListener('appinstalled', didInstall);
    return () => {
      window.removeEventListener('online', wentOnline);
      window.removeEventListener('offline', wentOffline);
      window.removeEventListener('beforeinstallprompt', canInstall);
      window.removeEventListener('appinstalled', didInstall);
    };
  }, []);

  async function install() {
    if (!installPrompt) return;
    await installPrompt.prompt();
    const choice = await installPrompt.userChoice;
    if (choice?.outcome === 'accepted') setInstalled(true);
    setInstallPrompt(null);
  }

  if (!online) {
    return (
      <aside className="wc-connectivity wc-connectivity--offline" role="alert">
        <strong>Offline</strong>
        <span>Saved screens remain visible, but scans, timers and photos need a connection before they can be submitted.</span>
      </aside>
    );
  }

  if (installed || dismissed || (!installPrompt && !iosDevice())) return null;

  return (
    <aside className="wc-connectivity" aria-label="Install warehouse app">
      <div>
        <strong>Keep Work Control on this phone</strong>
        <span>{installPrompt ? 'Install it for one-tap access and a full-screen workspace.' : 'On iPhone Safari, tap Share, then Add to Home Screen.'}</span>
      </div>
      <div className="wc-connectivity__actions">
        {installPrompt && <button type="button" className="btn btn-primary" onClick={install}>Install app</button>}
        <button type="button" className="btn" onClick={() => setDismissed(true)}>Not now</button>
      </div>
    </aside>
  );
}
