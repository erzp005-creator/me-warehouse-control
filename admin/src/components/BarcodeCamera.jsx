import { useEffect, useRef, useState } from 'react';

function cameraErrorMessage(error) {
  if (!window.isSecureContext) return 'Camera scanning needs the secure HTTPS address.';
  if (error?.name === 'NotAllowedError') return 'Camera access was blocked. Allow Camera for this site, then try again.';
  if (error?.name === 'NotFoundError') return 'No camera was found on this device.';
  if (error?.name === 'NotReadableError') return 'The camera is being used by another app. Close it and try again.';
  return 'Could not start the camera. You can still type or use a Bluetooth scanner.';
}

export default function BarcodeCamera({ onDetected, onClose, label = 'barcode' }) {
  const videoRef = useRef(null);
  const controlsRef = useRef(null);
  const detectedRef = useRef(false);
  const onDetectedRef = useRef(onDetected);
  const [error, setError] = useState('');
  const [starting, setStarting] = useState(true);

  useEffect(() => {
    onDetectedRef.current = onDetected;
  }, [onDetected]);

  useEffect(() => {
    let disposed = false;
    const videoElement = videoRef.current;

    async function start() {
      try {
        if (!navigator.mediaDevices?.getUserMedia) {
          throw Object.assign(new Error('Camera unavailable'), { name: 'NotFoundError' });
        }
        const { BrowserMultiFormatReader } = await import('@zxing/browser');
        if (disposed) return;
        const reader = new BrowserMultiFormatReader(undefined, {
          delayBetweenScanAttempts: 180,
          delayBetweenScanSuccess: 900,
        });
        const controls = await reader.decodeFromVideoDevice(undefined, videoElement, (result) => {
          if (!result || detectedRef.current) return;
          detectedRef.current = true;
          controlsRef.current?.stop();
          onDetectedRef.current(result.getText());
        });
        if (disposed) controls.stop();
        else {
          controlsRef.current = controls;
          setStarting(false);
        }
      } catch (startError) {
        if (!disposed) {
          setStarting(false);
          setError(cameraErrorMessage(startError));
        }
      }
    }

    start();
    return () => {
      disposed = true;
      controlsRef.current?.stop();
      const stream = videoElement?.srcObject;
      if (stream?.getTracks) stream.getTracks().forEach((track) => track.stop());
    };
  }, []);

  return (
    <section className="wc-camera" aria-label={`Scan ${label} with camera`}>
      <div className="wc-camera__viewport">
        <video ref={videoRef} muted playsInline aria-label="Rear camera preview" />
        {!error && <div className="wc-camera__guide" aria-hidden="true" />}
        {starting && <div className="wc-camera__status" role="status">Starting camera…</div>}
      </div>
      {error ? (
        <div className="wc-camera__error" role="alert">{error}</div>
      ) : (
        <p>Hold one barcode inside the frame. Nothing starts until you confirm.</p>
      )}
      <button type="button" className="btn" onClick={onClose}>Close camera</button>
    </section>
  );
}
