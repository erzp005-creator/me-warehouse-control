import React, { useEffect, useMemo, useState } from 'react';
import {
  ActivityIndicator,
  Image,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  TouchableOpacity,
  View,
} from 'react-native';
import * as ImagePicker from 'expo-image-picker';
import { useAuth } from '../auth/AuthContext';
import client from '../api/client';
import ScanInput from '../components/ScanInput';
import { buttonStyles, colors, fonts, listStyles, radii, screenStyles, spacing } from '../theme/styles';

const FUNCTION_TASK_TYPES = {
  pick: 'PICKING',
  pack: 'PACKING',
  receive: 'RECEIVING',
  putaway: 'PUTAWAY',
  count: 'STOCK_CHECK',
};

function message(error) {
  return error?.response?.data?.error || error?.message || 'Something went wrong';
}

function numberOrNull(value) {
  if (String(value).trim() === '') return null;
  const number = Number(value);
  return Number.isInteger(number) && number >= 0 ? number : NaN;
}

export default function ReceivingCountScreen({ navigation, route }) {
  const { warehouseId } = useAuth();
  const task = route.params?.task;
  const [taskTypes, setTaskTypes] = useState([]);
  const [reference, setReference] = useState(task?.source_ref || '');
  const [supplier, setSupplier] = useState('');
  const [entry, setEntry] = useState({ sku: '', expected: '', received: '', damaged: '0' });
  const [lines, setLines] = useState([]);
  const [photo, setPhoto] = useState(null);
  const [draftId, setDraftId] = useState(null);
  const [photoUploaded, setPhotoUploaded] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const [me, drafts] = await Promise.all([
          client.get('/api/auth/me'),
          client.get(`/api/work-control/receiving-drafts?warehouse_id=${warehouseId}`),
        ]);
        if (!active) return;
        const allowed = me.data.allowed_functions || [];
        setTaskTypes(allowed.map((key) => FUNCTION_TASK_TYPES[key]).filter(Boolean));
        const existing = (drafts.data.receiving_drafts || []).find(
          (item) => Number(item.task_id) === Number(task?.task_id) && item.status === 'DRAFT',
        );
        if (existing) {
          setDraftId(existing.receiving_id);
          setReference(existing.po_number || '');
          setSupplier(existing.supplier_ref || '');
          setLines((existing.lines || []).map((line) => ({
            sku: line.sku,
            expected_quantity: line.expected_quantity,
            received_quantity: line.received_quantity,
            good_quantity: line.good_quantity,
            damaged_quantity: line.damaged_quantity,
            notes: line.notes || null,
          })));
        }
      } catch (loadError) {
        if (active) setError(message(loadError));
      } finally {
        if (active) setLoading(false);
      }
    })();
    return () => { active = false; };
  }, [task?.task_id, warehouseId]);

  const totalUnits = useMemo(
    () => lines.reduce((sum, line) => sum + Number(line.received_quantity || 0), 0),
    [lines],
  );

  function addLine() {
    setError('');
    if (draftId) return setError('This draft is already saved; submit it or ask a supervisor to reject it.');
    const expected = numberOrNull(entry.expected);
    const received = numberOrNull(entry.received);
    const damaged = numberOrNull(entry.damaged || '0');
    if (!entry.sku.trim()) return setError('SKU is required');
    if (Number.isNaN(expected) || received === null || Number.isNaN(received) || Number.isNaN(damaged)) return setError('Quantities must be whole numbers of zero or more');
    if (damaged > received) return setError('Damaged quantity cannot exceed received quantity');
    const normalizedSku = entry.sku.trim().toUpperCase();
    if (lines.some((line) => line.sku === normalizedSku)) return setError('This SKU is already in the count');
    setLines((current) => [...current, {
      sku: normalizedSku,
      expected_quantity: expected,
      received_quantity: received,
      good_quantity: received - damaged,
      damaged_quantity: damaged,
      notes: null,
    }]);
    setEntry({ sku: '', expected: '', received: '', damaged: '0' });
  }

  async function capturePhoto() {
    setError('');
    try {
      const permission = await ImagePicker.requestCameraPermissionsAsync();
      if (!permission.granted) throw new Error('Camera permission is required');
      const result = await ImagePicker.launchCameraAsync({ mediaTypes: ['images'], quality: 0.72 });
      if (!result.canceled && result.assets?.[0]) {
        setPhoto(result.assets[0]);
        setPhotoUploaded(false);
      }
    } catch (photoError) {
      setError(message(photoError));
    }
  }

  async function saveAndSubmit() {
    if (!task) return setError('Receiving task is missing');
    if (!lines.length) return setError('Add at least one SKU');
    if (!photo) return setError('Take an arrival photo before submission');
    setBusy(true);
    setError('');
    let receivingId = draftId;
    try {
      if (!receivingId) {
        const created = await client.post('/api/work-control/receiving-drafts', {
          warehouse_id: task.warehouse_id || warehouseId,
          task_id: task.task_id,
          source_system: 'sitegiant',
          po_number: reference.trim() || null,
          supplier_ref: supplier.trim() || null,
          notes: null,
          lines,
        }, { timeout: 30000 });
        receivingId = created.data.receiving.receiving_id;
        setDraftId(receivingId);
      }
      if (!photoUploaded) {
        const form = new FormData();
        form.append('receiving_id', String(receivingId));
        form.append('note', 'Arrival count evidence');
        form.append('photo', {
          uri: photo.uri,
          name: photo.fileName || `receiving-${receivingId}.jpg`,
          type: photo.mimeType || 'image/jpeg',
        });
        await client.upload('/api/work-control/evidence', form, { timeout: 30000 });
        setPhotoUploaded(true);
      }
      await client.post(`/api/work-control/receiving-drafts/${receivingId}/submit`, {
        claim_next: true,
        next_task_types: taskTypes,
      }, { timeout: 30000 });
      navigation.goBack();
    } catch (submitError) {
      setError(message(submitError));
    } finally {
      setBusy(false);
    }
  }

  if (loading) return <View style={styles.center}><ActivityIndicator color={colors.accentRed} /></View>;

  return (
    <View style={screenStyles.screen}>
      <View style={screenStyles.header}>
        <TouchableOpacity style={screenStyles.backBtn} onPress={() => navigation.goBack()}><Text style={screenStyles.backText}>‹</Text></TouchableOpacity>
        <Text style={screenStyles.headerTitle}>ARRIVAL COUNT</Text>
        <View style={screenStyles.menuBtn} />
      </View>
      <ScrollView contentContainerStyle={styles.content} keyboardShouldPersistTaps="handled">
        {error ? <View style={styles.errorBox}><Text style={styles.errorText}>{error}</Text></View> : null}
        <View style={styles.summaryCard}>
          <Text style={styles.summaryLabel}>RECEIVING TASK</Text>
          <Text style={styles.summaryValue}>{task?.source_ref || task?.pack_note_ref || `#${task?.task_id}`}</Text>
          <Text style={styles.summaryMeta}>{lines.length} SKU lines · {totalUnits} units counted</Text>
          {draftId ? <Text style={styles.saved}>DRAFT SAVED · GRN-DRAFT-{draftId}</Text> : null}
        </View>

        <View style={styles.formCard}>
          <Text style={styles.sectionTitle}>ARRIVAL REFERENCE</Text>
          <TextInput style={styles.input} placeholder="PO / delivery note (optional)" placeholderTextColor={colors.textPlaceholder} value={reference} onChangeText={setReference} editable={!draftId} />
          <TextInput style={styles.input} placeholder="Supplier reference (optional)" placeholderTextColor={colors.textPlaceholder} value={supplier} onChangeText={setSupplier} editable={!draftId} />
        </View>

        {!draftId && (
          <View style={styles.formCard}>
            <Text style={styles.sectionTitle}>ADD SKU</Text>
            <ScanInput placeholder="SCAN OR ENTER SKU" onScan={(sku) => setEntry({ ...entry, sku })} suppressRefocus />
            <TextInput style={styles.input} placeholder="SKU" placeholderTextColor={colors.textPlaceholder} value={entry.sku} onChangeText={(value) => setEntry({ ...entry, sku: value })} autoCapitalize="characters" />
            <View style={styles.quantityRow}>
              <View style={styles.quantityField}><Text style={styles.fieldLabel}>EXPECTED</Text><TextInput style={styles.quantityInput} keyboardType="number-pad" value={entry.expected} onChangeText={(value) => setEntry({ ...entry, expected: value.replace(/\D/g, '') })} placeholder="—" placeholderTextColor={colors.textPlaceholder} /></View>
              <View style={styles.quantityField}><Text style={styles.fieldLabel}>RECEIVED</Text><TextInput style={styles.quantityInput} keyboardType="number-pad" value={entry.received} onChangeText={(value) => setEntry({ ...entry, received: value.replace(/\D/g, '') })} placeholder="0" placeholderTextColor={colors.textPlaceholder} /></View>
              <View style={styles.quantityField}><Text style={styles.fieldLabel}>DAMAGED</Text><TextInput style={styles.quantityInput} keyboardType="number-pad" value={entry.damaged} onChangeText={(value) => setEntry({ ...entry, damaged: value.replace(/\D/g, '') })} placeholder="0" placeholderTextColor={colors.textPlaceholder} /></View>
            </View>
            <TouchableOpacity style={buttonStyles.buttonSecondary} onPress={addLine}><Text style={buttonStyles.buttonSecondaryText}>ADD SKU LINE</Text></TouchableOpacity>
          </View>
        )}

        <Text style={styles.sectionTitle}>COUNTED SKU</Text>
        {lines.map((line, index) => (
          <View key={line.sku} style={listStyles.row}>
            <View style={{ flex: 1 }}><Text style={listStyles.sku}>{line.sku}</Text><Text style={listStyles.itemName}>Expected {line.expected_quantity ?? '—'} · Good {line.good_quantity} · Damaged {line.damaged_quantity}</Text></View>
            <Text style={styles.lineQty}>{line.received_quantity}</Text>
            {!draftId && <TouchableOpacity style={listStyles.removeBtn} onPress={() => setLines((current) => current.filter((_, itemIndex) => itemIndex !== index))}><Text style={listStyles.removeText}>×</Text></TouchableOpacity>}
          </View>
        ))}

        <View style={styles.photoCard}>
          <Text style={styles.sectionTitle}>ARRIVAL PHOTO · REQUIRED</Text>
          <Text style={styles.photoHelp}>Photograph the counted goods and visible labels. The stock clerk receives this with the Draft GRN.</Text>
          {photo ? <Image source={{ uri: photo.uri }} style={styles.photo} /> : <View style={styles.photoEmpty}><Text style={styles.photoEmptyText}>NO PHOTO YET</Text></View>}
          <TouchableOpacity style={buttonStyles.buttonSecondary} onPress={capturePhoto} disabled={busy}><Text style={buttonStyles.buttonSecondaryText}>{photo ? 'RETAKE PHOTO' : 'TAKE PHOTO'}</Text></TouchableOpacity>
        </View>

        <TouchableOpacity style={[buttonStyles.buttonPrimary, busy && buttonStyles.buttonDisabled]} onPress={saveAndSubmit} disabled={busy}>
          <Text style={buttonStyles.buttonPrimaryText}>{busy ? 'SENDING…' : 'SUBMIT TO STOCK CLERK · NEXT TASK'}</Text>
        </TouchableOpacity>
        <Text style={styles.footerNote}>Submission creates a Draft GRN only. It does not post inventory; you can continue immediately.</Text>
      </ScrollView>
    </View>
  );
}

const styles = StyleSheet.create({
  center: { flex: 1, alignItems: 'center', justifyContent: 'center', backgroundColor: colors.background },
  content: { padding: spacing.screenPadding, paddingBottom: 42 },
  errorBox: { backgroundColor: '#fbe9e6', borderColor: colors.danger, borderWidth: 1, borderRadius: radii.small, padding: 11, marginBottom: 12 },
  errorText: { color: colors.danger, fontSize: 13 },
  summaryCard: { borderWidth: 1.5, borderColor: colors.copper, borderRadius: radii.card, backgroundColor: colors.cardBg, padding: 15, marginBottom: 12 },
  summaryLabel: { fontFamily: fonts.mono, fontSize: 10, color: colors.copper, fontWeight: '700' },
  summaryValue: { fontFamily: fonts.mono, fontSize: 21, color: colors.textPrimary, fontWeight: '700', marginTop: 7 },
  summaryMeta: { color: colors.textMuted, fontSize: 12, marginTop: 5 },
  saved: { fontFamily: fonts.mono, color: colors.success, fontSize: 10, fontWeight: '700', marginTop: 9 },
  formCard: { borderWidth: 1, borderColor: colors.cardBorder, borderRadius: radii.card, padding: 13, marginBottom: 12 },
  sectionTitle: { fontFamily: fonts.mono, fontSize: 11, fontWeight: '700', color: colors.textSecondary, letterSpacing: .7, marginBottom: 9 },
  input: { minHeight: 47, borderWidth: 1, borderColor: colors.inputBorder, borderRadius: radii.input, backgroundColor: colors.inputBg, paddingHorizontal: 11, color: colors.textPrimary, marginBottom: 8 },
  quantityRow: { flexDirection: 'row', gap: 7, marginBottom: 10 },
  quantityField: { flex: 1 },
  fieldLabel: { fontFamily: fonts.mono, fontSize: 8, color: colors.textMuted, textAlign: 'center', marginBottom: 3 },
  quantityInput: { minHeight: 48, borderWidth: 1, borderColor: colors.inputBorder, borderRadius: radii.input, backgroundColor: colors.inputBg, fontFamily: fonts.mono, textAlign: 'center', fontSize: 18, color: colors.textPrimary },
  lineQty: { fontFamily: fonts.mono, fontSize: 22, fontWeight: '700', color: colors.accentRed, minWidth: 40, textAlign: 'center' },
  photoCard: { borderWidth: 1, borderColor: colors.cardBorder, borderRadius: radii.card, padding: 13, marginTop: 10, marginBottom: 12 },
  photoHelp: { color: colors.textMuted, fontSize: 12, lineHeight: 18, marginBottom: 9 },
  photo: { width: '100%', height: 210, borderRadius: radii.small, marginBottom: 10, backgroundColor: colors.cardBg },
  photoEmpty: { height: 120, alignItems: 'center', justifyContent: 'center', borderWidth: 1, borderStyle: 'dashed', borderColor: colors.inputBorder, borderRadius: radii.small, marginBottom: 10 },
  photoEmptyText: { fontFamily: fonts.mono, color: colors.textPlaceholder, fontSize: 10 },
  footerNote: { color: colors.textMuted, fontSize: 11, textAlign: 'center', lineHeight: 16, marginTop: 9, paddingHorizontal: 12 },
});
