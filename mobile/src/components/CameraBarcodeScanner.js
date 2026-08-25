import React, { useEffect, useRef, useState } from 'react';
import {
  Modal,
  StyleSheet,
  Text,
  TouchableOpacity,
  View,
} from 'react-native';
import { CameraView, useCameraPermissions } from 'expo-camera';
import { SafeAreaView } from 'react-native-safe-area-context';
import { colors, fonts, radii } from '../theme/styles';

const COURIER_BARCODE_TYPES = [
  'code128',
  'code39',
  'code93',
  'codabar',
  'ean13',
  'ean8',
  'itf14',
  'upc_a',
  'upc_e',
  'qr',
  'pdf417',
  'datamatrix',
  'aztec',
];

function cleanBarcode(value) {
  return String(value || '').replace(/[\r\n\t\s]+/g, '').trim();
}

export default function CameraBarcodeScanner({ visible, onClose, onScan }) {
  const [permission, requestPermission] = useCameraPermissions();
  const [torchEnabled, setTorchEnabled] = useState(false);
  const [cameraError, setCameraError] = useState('');
  const scanLockedRef = useRef(false);

  useEffect(() => {
    if (!visible) return;
    scanLockedRef.current = false;
    setCameraError('');
    setTorchEnabled(false);
  }, [visible]);

  function handleBarcode({ data }) {
    const barcode = cleanBarcode(data);
    if (!barcode || scanLockedRef.current) return;
    scanLockedRef.current = true;
    onClose();
    Promise.resolve(onScan(barcode)).catch(() => {
      // WorkQueueScreen owns the user-facing API error state.
    });
  }

  return (
    <Modal visible={visible} animationType="slide" onRequestClose={onClose}>
      <SafeAreaView style={styles.screen}>
        {permission?.granted ? (
          <>
            <CameraView
              active={visible}
              facing="back"
              enableTorch={torchEnabled}
              style={StyleSheet.absoluteFill}
              barcodeScannerSettings={{ barcodeTypes: COURIER_BARCODE_TYPES }}
              onBarcodeScanned={scanLockedRef.current ? undefined : handleBarcode}
              onMountError={(event) => setCameraError(event?.message || 'Camera could not start.')}
            />
            <View style={styles.scrim} pointerEvents="none" />
            <View style={styles.content}>
              <View style={styles.header}>
                <Text style={styles.title}>Scan courier barcode</Text>
                <Text style={styles.subtitle}>Aim at any one order label inside this Pack Note.</Text>
              </View>

              <View style={styles.guide} accessibilityLabel="Barcode scanning area">
                <View style={[styles.corner, styles.topLeft]} />
                <View style={[styles.corner, styles.topRight]} />
                <View style={[styles.corner, styles.bottomLeft]} />
                <View style={[styles.corner, styles.bottomRight]} />
                <View style={styles.scanLine} />
              </View>

              {cameraError ? <Text style={styles.error}>{cameraError}</Text> : null}

              <View style={styles.actions}>
                <TouchableOpacity
                  accessibilityRole="button"
                  style={[styles.actionButton, torchEnabled && styles.actionButtonActive]}
                  onPress={() => setTorchEnabled((value) => !value)}
                >
                  <Text style={[styles.actionText, torchEnabled && styles.actionTextActive]}>
                    {torchEnabled ? 'FLASH ON' : 'FLASH OFF'}
                  </Text>
                </TouchableOpacity>
                <TouchableOpacity accessibilityRole="button" style={styles.actionButton} onPress={onClose}>
                  <Text style={styles.actionText}>CLOSE</Text>
                </TouchableOpacity>
              </View>
            </View>
          </>
        ) : (
          <View style={styles.permissionPanel}>
            <Text style={styles.permissionTitle}>Camera access is needed</Text>
            <Text style={styles.permissionText}>
              The camera is used only while you scan a courier barcode. No barcode image is saved.
            </Text>
            <TouchableOpacity
              accessibilityRole="button"
              style={styles.permissionPrimary}
              onPress={requestPermission}
            >
              <Text style={styles.permissionPrimaryText}>ALLOW CAMERA</Text>
            </TouchableOpacity>
            <TouchableOpacity accessibilityRole="button" style={styles.permissionCancel} onPress={onClose}>
              <Text style={styles.permissionCancelText}>NOT NOW</Text>
            </TouchableOpacity>
          </View>
        )}
      </SafeAreaView>
    </Modal>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: '#090909' },
  scrim: { ...StyleSheet.absoluteFillObject, backgroundColor: 'rgba(0, 0, 0, 0.24)' },
  content: { flex: 1, justifyContent: 'space-between', paddingHorizontal: 20, paddingVertical: 24 },
  header: { backgroundColor: 'rgba(9, 9, 9, 0.78)', borderRadius: radii.card, padding: 16 },
  title: { color: '#ffffff', fontSize: 22, fontWeight: '700' },
  subtitle: { color: '#f3e8d7', fontSize: 14, lineHeight: 20, marginTop: 6 },
  guide: { alignSelf: 'center', width: '92%', aspectRatio: 1.55, justifyContent: 'center' },
  corner: { position: 'absolute', width: 34, height: 34, borderColor: colors.cream },
  topLeft: { top: 0, left: 0, borderTopWidth: 4, borderLeftWidth: 4 },
  topRight: { top: 0, right: 0, borderTopWidth: 4, borderRightWidth: 4 },
  bottomLeft: { bottom: 0, left: 0, borderBottomWidth: 4, borderLeftWidth: 4 },
  bottomRight: { bottom: 0, right: 0, borderBottomWidth: 4, borderRightWidth: 4 },
  scanLine: { height: 2, backgroundColor: colors.accentRed, marginHorizontal: 18 },
  error: {
    alignSelf: 'center',
    color: '#ffffff',
    backgroundColor: 'rgba(142, 39, 22, 0.92)',
    borderRadius: radii.small,
    paddingHorizontal: 14,
    paddingVertical: 10,
  },
  actions: { flexDirection: 'row', gap: 10 },
  actionButton: {
    flex: 1,
    minHeight: 52,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: 'rgba(9, 9, 9, 0.82)',
    borderWidth: 1,
    borderColor: 'rgba(255, 255, 255, 0.38)',
    borderRadius: radii.button,
  },
  actionButtonActive: { backgroundColor: colors.cream, borderColor: colors.cream },
  actionText: { color: '#ffffff', fontFamily: fonts.mono, fontSize: 12, fontWeight: '700' },
  actionTextActive: { color: colors.accentRed },
  permissionPanel: { flex: 1, justifyContent: 'center', paddingHorizontal: 28 },
  permissionTitle: { color: '#ffffff', fontSize: 26, fontWeight: '700', marginBottom: 10 },
  permissionText: { color: '#d5cabd', fontSize: 15, lineHeight: 22, marginBottom: 24 },
  permissionPrimary: {
    minHeight: 52,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: colors.accentRed,
    borderRadius: radii.button,
  },
  permissionPrimaryText: { color: colors.cream, fontFamily: fonts.mono, fontSize: 13, fontWeight: '700' },
  permissionCancel: { minHeight: 52, alignItems: 'center', justifyContent: 'center', marginTop: 8 },
  permissionCancelText: { color: '#d5cabd', fontFamily: fonts.mono, fontSize: 12, fontWeight: '700' },
});

