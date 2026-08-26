import React, { useCallback, useState } from 'react';
import {
  ActivityIndicator,
  Image,
  Modal,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  TouchableOpacity,
  View,
} from 'react-native';
import { useFocusEffect } from '@react-navigation/native';
import * as ImagePicker from 'expo-image-picker';
import { useAuth } from '../auth/AuthContext';
import client from '../api/client';
import CameraBarcodeScanner from '../components/CameraBarcodeScanner';
import ScanInput from '../components/ScanInput';
import { buttonStyles, colors, fonts, modalStyles, radii, screenStyles, spacing } from '../theme/styles';

const FUNCTION_TASK_TYPES = {
  pick: 'PICKING',
  pack: 'PACKING',
  receive: 'RECEIVING',
  putaway: 'PUTAWAY',
  count: 'STOCK_CHECK',
};

const TYPE_LABELS = {
  PICKING: 'PICK',
  PACKING: 'PACK',
  RECEIVING: 'COUNT ARRIVAL',
  PUTAWAY: 'PUT AWAY',
  STOCK_CHECK: 'STOCK CHECK',
  OTHER: 'WAREHOUSE TASK',
};

function errorMessage(error) {
  return error?.response?.data?.error || error?.message || 'Something went wrong';
}

function elapsed(seconds) {
  const value = Number(seconds || 0);
  const minutes = Math.floor(value / 60);
  return `${minutes}m ${value % 60}s`;
}

async function takePhoto() {
  const permission = await ImagePicker.requestCameraPermissionsAsync();
  if (!permission.granted) throw new Error('Camera permission is required');
  const result = await ImagePicker.launchCameraAsync({
    mediaTypes: ['images'],
    quality: 0.72,
  });
  return result.canceled ? null : result.assets?.[0] || null;
}

export default function WorkQueueScreen({ navigation }) {
  const { warehouseId } = useAuth();
  const [task, setTask] = useState(null);
  const [taskTypes, setTaskTypes] = useState([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [pauseOpen, setPauseOpen] = useState(false);
  const [reportOpen, setReportOpen] = useState(false);
  const [cameraScannerOpen, setCameraScannerOpen] = useState(false);
  const [report, setReport] = useState({
    error_type: 'WRONG_QUANTITY',
    severity: 'MEDIUM',
    order_number: '',
    sku: '',
    description: '',
    photo: null,
  });

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [current, me] = await Promise.all([
        client.get('/api/work-control/tasks/current'),
        client.get('/api/auth/me'),
      ]);
      const allowed = me.data.allowed_functions || [];
      setTaskTypes(allowed.map((key) => FUNCTION_TASK_TYPES[key]).filter(Boolean));
      setTask(current.data.task || null);
    } catch (loadError) {
      setError(errorMessage(loadError));
    } finally {
      setLoading(false);
    }
  }, []);

  useFocusEffect(useCallback(() => { load(); }, [load]));

  async function claimNext() {
    if (!warehouseId) return setError('Select a warehouse first');
    if (!taskTypes.length) return setError('No work types are granted to this account');
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const response = await client.post('/api/work-control/tasks/claim-next', {
        warehouse_id: warehouseId,
        task_types: taskTypes,
      });
      setTask(response.data.task || null);
      if (!response.data.task) setNotice('No suitable task is waiting right now.');
    } catch (claimError) {
      setError(errorMessage(claimError));
    } finally {
      setBusy(false);
    }
  }

  async function transition(action, extra = {}) {
    if (!task) return;
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const response = await client.post(`/api/work-control/tasks/${task.task_id}/transition`, {
        action,
        ...extra,
        claim_next: action === 'COMPLETE',
        next_task_types: action === 'COMPLETE' ? taskTypes : null,
      });
      setTask(response.data.next_task || response.data.task || null);
      if (action === 'COMPLETE' && !response.data.next_task) {
        setTask(null);
        setNotice('Task completed. No suitable task is waiting.');
      }
      setPauseOpen(false);
    } catch (transitionError) {
      setError(errorMessage(transitionError));
    } finally {
      setBusy(false);
    }
  }

  async function scanToStart(barcode) {
    if (!task) return;
    setBusy(true);
    setError('');
    try {
      await client.post(`/api/work-control/tasks/${task.task_id}/verify-scan`, { barcode });
      await transition('START', {
        reason_code: 'BATCH_BARCODE_SCANNED',
        notes: `Scanned ${barcode}`,
      });
    } catch (scanError) {
      setError(errorMessage(scanError));
      setBusy(false);
    }
  }

  async function captureReportPhoto() {
    try {
      const photo = await takePhoto();
      if (photo) setReport((value) => ({ ...value, photo }));
    } catch (photoError) {
      setError(errorMessage(photoError));
    }
  }

  async function submitReport() {
    if (!task) return;
    setBusy(true);
    setError('');
    try {
      const created = await client.post('/api/work-control/errors', {
        warehouse_id: task.warehouse_id,
        task_id: task.task_id,
        batch_id: task.batch_id || null,
        error_type: report.error_type,
        severity: report.severity,
        discovered_stage: task.task_type,
        order_number: report.order_number || null,
        sku: report.sku || null,
        description: report.description || null,
      });
      if (report.photo) {
        const form = new FormData();
        form.append('error_id', String(created.data.error_id));
        form.append('photo', {
          uri: report.photo.uri,
          name: report.photo.fileName || `mistake-${created.data.error_id}.jpg`,
          type: report.photo.mimeType || 'image/jpeg',
        });
        await client.upload('/api/work-control/evidence', form, { timeout: 30000 });
      }
      setReportOpen(false);
      setReport({ error_type: 'WRONG_QUANTITY', severity: 'MEDIUM', order_number: '', sku: '', description: '', photo: null });
      setNotice(`Mistake case #${created.data.error_id} sent for review.`);
    } catch (reportError) {
      setError(errorMessage(reportError));
    } finally {
      setBusy(false);
    }
  }

  if (loading) return <View style={styles.center}><ActivityIndicator color={colors.accentRed} /></View>;

  return (
    <View style={screenStyles.screen}>
      <View style={screenStyles.header}>
        <TouchableOpacity style={screenStyles.backBtn} onPress={() => navigation.goBack()}><Text style={screenStyles.backText}>‹</Text></TouchableOpacity>
        <Text style={screenStyles.headerTitle}>WORK QUEUE</Text>
        <TouchableOpacity style={screenStyles.menuBtn} onPress={load}><Text style={styles.refresh}>↻</Text></TouchableOpacity>
      </View>

      <ScrollView contentContainerStyle={styles.content} keyboardShouldPersistTaps="handled">
        {error ? <View style={styles.errorBox}><Text style={styles.errorText}>{error}</Text></View> : null}
        {notice ? <View style={styles.noticeBox}><Text style={styles.noticeText}>{notice}</Text></View> : null}

        {!task ? (
          <View style={styles.emptyCard}>
            <Text style={styles.emptyEyebrow}>READY</Text>
            <Text style={styles.emptyTitle}>Get your next task</Text>
            <Text style={styles.emptyText}>The queue uses priority and your granted work types. One task is assigned at a time.</Text>
            <TouchableOpacity style={[buttonStyles.buttonPrimary, busy && buttonStyles.buttonDisabled]} onPress={claimNext} disabled={busy}>
              <Text style={buttonStyles.buttonPrimaryText}>{busy ? 'CHECKING…' : 'GET NEXT TASK'}</Text>
            </TouchableOpacity>
          </View>
        ) : (
          <>
            <View style={styles.taskCard}>
              <View style={styles.taskTopRow}>
                <Text style={styles.taskType}>{TYPE_LABELS[task.task_type] || task.task_type}</Text>
                <Text style={styles.status}>{task.status}</Text>
              </View>
              <Text style={styles.packNote}>{task.pack_note_ref || task.source_ref || `TASK #${task.task_id}`}</Text>
              <View style={styles.facts}>
                <View><Text style={styles.factValue}>{task.order_count || 0}</Text><Text style={styles.factLabel}>ORDERS</Text></View>
                <View><Text style={styles.factValue}>{task.sku_count || 0}</Text><Text style={styles.factLabel}>SKUS</Text></View>
                <View><Text style={styles.factValue}>{task.unit_count || 0}</Text><Text style={styles.factLabel}>UNITS</Text></View>
              </View>
              {task.complexity_note ? <Text style={styles.complexity}>{task.complexity_note}</Text> : null}
              <Text style={styles.timeText}>Recorded: {elapsed(task.active_seconds)} active · {elapsed(task.paused_seconds)} excluded</Text>
            </View>

            {task.status === 'CLAIMED' && ['PICKING', 'PACKING'].includes(task.task_type) && (
              <View style={styles.actionCard}>
                <Text style={styles.actionTitle}>SCAN PACK NOTE OR ONE ORDER</Text>
                <Text style={styles.actionHelp}>The Pack Note number or any listed order/courier barcode confirms the whole batch.</Text>
                <ScanInput placeholder="SCAN PACK NOTE / ORDER BARCODE" onScan={scanToStart} disabled={busy} />
                <TouchableOpacity
                  accessibilityRole="button"
                  style={buttonStyles.buttonSecondary}
                  onPress={() => setCameraScannerOpen(true)}
                  disabled={busy}
                >
                  <Text style={buttonStyles.buttonSecondaryText}>USE PHONE CAMERA</Text>
                </TouchableOpacity>
              </View>
            )}

            {task.status === 'CLAIMED' && !['PICKING', 'PACKING'].includes(task.task_type) && (
              <TouchableOpacity style={[buttonStyles.buttonPrimary, busy && buttonStyles.buttonDisabled]} onPress={() => transition('START')} disabled={busy}>
                <Text style={buttonStyles.buttonPrimaryText}>START TASK</Text>
              </TouchableOpacity>
            )}

            {task.status === 'IN_PROGRESS' && task.task_type === 'RECEIVING' && (
              <TouchableOpacity style={buttonStyles.buttonPrimary} onPress={() => navigation.navigate('ReceivingCount', { task })} disabled={busy}>
                <Text style={buttonStyles.buttonPrimaryText}>COUNT SKU & TAKE PHOTOS</Text>
              </TouchableOpacity>
            )}

            {task.status === 'IN_PROGRESS' && task.task_type !== 'RECEIVING' && (
              <TouchableOpacity style={[buttonStyles.buttonPrimary, busy && buttonStyles.buttonDisabled]} onPress={() => transition('COMPLETE')} disabled={busy}>
                <Text style={buttonStyles.buttonPrimaryText}>100% COMPLETE · NEXT TASK</Text>
              </TouchableOpacity>
            )}

            {task.status === 'IN_PROGRESS' && (
              <TouchableOpacity style={[buttonStyles.buttonSecondary, { marginTop: 8 }]} onPress={() => setPauseOpen(true)} disabled={busy}>
                <Text style={buttonStyles.buttonSecondaryText}>PAUSE / WAITING REASON</Text>
              </TouchableOpacity>
            )}
            {task.status === 'PAUSED' && (
              <TouchableOpacity style={buttonStyles.buttonPrimary} onPress={() => transition('RESUME')} disabled={busy}>
                <Text style={buttonStyles.buttonPrimaryText}>RESUME TASK</Text>
              </TouchableOpacity>
            )}
            {['CLAIMED', 'IN_PROGRESS', 'PAUSED'].includes(task.status) && (
              <TouchableOpacity style={styles.reportButton} onPress={() => setReportOpen(true)} disabled={busy}>
                <Text style={styles.reportText}>REPORT MISTAKE / EXCEPTION</Text>
              </TouchableOpacity>
            )}
          </>
        )}
      </ScrollView>

      <Modal visible={pauseOpen} transparent animationType="fade">
        <Pressable style={modalStyles.overlay} onPress={() => setPauseOpen(false)}>
          <Pressable style={modalStyles.card} onPress={() => {}}>
            <Text style={modalStyles.title}>Why is work paused?</Text>
            <Text style={modalStyles.subtitle}>Paused time is excluded from active work time.</Text>
            {[
              ['WAITING_STOCK', 'Waiting for stock'],
              ['SYSTEM_DELAY', 'System / printer delay'],
              ['SUPERVISOR_REQUEST', 'Supervisor request'],
              ['BREAK', 'Break'],
              ['OTHER', 'Other'],
            ].map(([code, label]) => (
              <TouchableOpacity key={code} style={styles.reasonButton} onPress={() => transition('PAUSE', { reason_code: code, notes: label })}>
                <Text style={styles.reasonText}>{label}</Text>
              </TouchableOpacity>
            ))}
          </Pressable>
        </Pressable>
      </Modal>

      <Modal visible={reportOpen} transparent animationType="slide">
        <View style={modalStyles.overlay}>
          <View style={[modalStyles.card, styles.reportModal]}>
            <ScrollView keyboardShouldPersistTaps="handled">
              <Text style={modalStyles.title}>Report mistake</Text>
              <Text style={styles.inputLabel}>TYPE</Text>
              <View style={styles.choiceRow}>{['WRONG_QUANTITY', 'WRONG_SKU', 'WRONG_LABEL', 'DAMAGE', 'OTHER'].map((value) => <TouchableOpacity key={value} style={[styles.choice, report.error_type === value && styles.choiceActive]} onPress={() => setReport({ ...report, error_type: value })}><Text style={[styles.choiceText, report.error_type === value && styles.choiceTextActive]}>{value.replaceAll('_', ' ')}</Text></TouchableOpacity>)}</View>
              <Text style={styles.inputLabel}>ORDER / BARCODE (OPTIONAL)</Text>
              <TextInput style={styles.input} value={report.order_number} onChangeText={(value) => setReport({ ...report, order_number: value })} autoCapitalize="characters" />
              <Text style={styles.inputLabel}>SKU (OPTIONAL)</Text>
              <TextInput style={styles.input} value={report.sku} onChangeText={(value) => setReport({ ...report, sku: value })} autoCapitalize="characters" />
              <Text style={styles.inputLabel}>DESCRIPTION</Text>
              <TextInput style={[styles.input, styles.multiline]} multiline value={report.description} onChangeText={(value) => setReport({ ...report, description: value })} />
              {report.photo ? <Image source={{ uri: report.photo.uri }} style={styles.photo} /> : null}
              <TouchableOpacity style={buttonStyles.buttonSecondary} onPress={captureReportPhoto}><Text style={buttonStyles.buttonSecondaryText}>{report.photo ? 'RETAKE PHOTO' : 'TAKE EVIDENCE PHOTO'}</Text></TouchableOpacity>
              <TouchableOpacity style={[buttonStyles.buttonPrimary, { marginTop: 8 }]} onPress={submitReport} disabled={busy}><Text style={buttonStyles.buttonPrimaryText}>SEND FOR REVIEW</Text></TouchableOpacity>
              <TouchableOpacity style={{ padding: 14, alignItems: 'center' }} onPress={() => setReportOpen(false)}><Text style={styles.cancelText}>CANCEL</Text></TouchableOpacity>
            </ScrollView>
          </View>
        </View>
      </Modal>

      <CameraBarcodeScanner
        visible={cameraScannerOpen && task?.status === 'CLAIMED'}
        onClose={() => setCameraScannerOpen(false)}
        onScan={scanToStart}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  center: { flex: 1, alignItems: 'center', justifyContent: 'center', backgroundColor: colors.background },
  content: { padding: spacing.screenPadding, paddingBottom: 40 },
  refresh: { fontSize: 23, color: colors.textPrimary },
  errorBox: { backgroundColor: '#fbe9e6', borderColor: colors.danger, borderWidth: 1, borderRadius: radii.small, padding: 11, marginBottom: 12 },
  errorText: { color: colors.danger, fontSize: 13 },
  noticeBox: { backgroundColor: '#eaf5ed', borderColor: colors.success, borderWidth: 1, borderRadius: radii.small, padding: 11, marginBottom: 12 },
  noticeText: { color: '#276337', fontSize: 13 },
  emptyCard: { borderWidth: 1, borderColor: colors.cardBorder, backgroundColor: colors.cardBg, borderRadius: radii.card, padding: 22, marginTop: 18 },
  emptyEyebrow: { fontFamily: fonts.mono, fontSize: 11, color: colors.copper, fontWeight: '700', letterSpacing: 1 },
  emptyTitle: { fontSize: 24, color: colors.textPrimary, fontWeight: '700', marginTop: 8 },
  emptyText: { fontSize: 14, color: colors.textMuted, lineHeight: 20, marginVertical: 12 },
  taskCard: { borderWidth: 1.5, borderColor: colors.accentRed, borderRadius: radii.card, padding: 16, backgroundColor: colors.cardBg, marginBottom: 12 },
  taskTopRow: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' },
  taskType: { fontFamily: fonts.mono, fontSize: 13, color: colors.accentRed, fontWeight: '700', letterSpacing: 1 },
  status: { fontFamily: fonts.mono, fontSize: 10, color: colors.textSecondary, borderWidth: 1, borderColor: colors.cardBorder, borderRadius: 8, paddingHorizontal: 8, paddingVertical: 4 },
  packNote: { fontFamily: fonts.mono, fontSize: 26, fontWeight: '700', color: colors.textPrimary, marginVertical: 18 },
  facts: { flexDirection: 'row', justifyContent: 'space-between', borderTopWidth: 1, borderTopColor: colors.cardBorder, paddingTop: 12 },
  factValue: { fontFamily: fonts.mono, fontSize: 22, fontWeight: '700', textAlign: 'center' },
  factLabel: { fontFamily: fonts.mono, fontSize: 9, color: colors.textMuted, textAlign: 'center', marginTop: 2 },
  complexity: { color: colors.textSecondary, fontSize: 13, marginTop: 12 },
  timeText: { fontFamily: fonts.mono, fontSize: 10, color: colors.textMuted, marginTop: 12 },
  actionCard: { padding: 14, borderWidth: 1, borderColor: colors.cardBorder, borderRadius: radii.card, marginBottom: 12 },
  actionTitle: { fontFamily: fonts.mono, fontSize: 13, fontWeight: '700', color: colors.textPrimary },
  actionHelp: { fontSize: 12, color: colors.textMuted, lineHeight: 18, marginVertical: 6 },
  reportButton: { minHeight: 48, alignItems: 'center', justifyContent: 'center', marginTop: 8 },
  reportText: { fontFamily: fonts.mono, fontSize: 11, color: colors.danger, fontWeight: '700' },
  reasonButton: { borderTopWidth: 1, borderTopColor: colors.cardBorder, paddingVertical: 13 },
  reasonText: { color: colors.textPrimary, fontSize: 14 },
  reportModal: { maxWidth: 440, maxHeight: '92%' },
  inputLabel: { fontFamily: fonts.mono, fontSize: 10, fontWeight: '700', color: colors.textMuted, marginTop: 10, marginBottom: 4 },
  input: { minHeight: 46, borderWidth: 1, borderColor: colors.inputBorder, borderRadius: radii.input, backgroundColor: colors.inputBg, paddingHorizontal: 11, color: colors.textPrimary },
  multiline: { minHeight: 78, paddingTop: 10, textAlignVertical: 'top' },
  choiceRow: { flexDirection: 'row', flexWrap: 'wrap', gap: 5 },
  choice: { borderWidth: 1, borderColor: colors.cardBorder, borderRadius: 8, paddingHorizontal: 8, paddingVertical: 7 },
  choiceActive: { borderColor: colors.accentRed, backgroundColor: colors.accentRed },
  choiceText: { fontFamily: fonts.mono, fontSize: 8, color: colors.textSecondary },
  choiceTextActive: { color: colors.cream },
  photo: { width: '100%', height: 150, borderRadius: radii.small, marginVertical: 10, backgroundColor: colors.cardBg },
  cancelText: { fontFamily: fonts.mono, fontSize: 11, color: colors.textMuted },
});

