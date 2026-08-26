import React, { useEffect, useMemo, useRef, useState } from 'react';
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
import client, { getAuthenticatedAssetSource } from '../api/client';
import ScanInput from '../components/ScanInput';
import { buttonStyles, colors, fonts, listStyles, radii, screenStyles, spacing } from '../theme/styles';

const FUNCTION_TASK_TYPES = {
  pick: 'PICKING',
  pack: 'PACKING',
  receive: 'RECEIVING',
  putaway: 'PUTAWAY',
  count: 'STOCK_CHECK',
};

const EMPTY_ENTRY = { sku: '', item_name: '', expected: '', received: '', damaged: '0', photo: null };

function message(error) {
  return error?.response?.data?.error || error?.message || 'Something went wrong';
}

function numberOrNull(value) {
  if (String(value).trim() === '') return null;
  const number = Number(value);
  return Number.isInteger(number) && number >= 0 ? number : NaN;
}

function normalizeSku(value) {
  return String(value || '').trim().toUpperCase();
}

async function takePhoto() {
  const permission = await ImagePicker.requestCameraPermissionsAsync();
  if (!permission.granted) throw new Error('Camera permission is required');
  const result = await ImagePicker.launchCameraAsync({ mediaTypes: ['images'], quality: 0.72 });
  return result.canceled ? null : result.assets?.[0] || null;
}

export default function ReceivingCountScreen({ navigation, route }) {
  const { warehouseId } = useAuth();
  const task = route.params?.task;
  const selectionVersion = useRef(0);
  const [taskTypes, setTaskTypes] = useState([]);
  const [reference, setReference] = useState(task?.source_ref || '');
  const [supplier, setSupplier] = useState('');
  const [entry, setEntry] = useState(EMPTY_ENTRY);
  const [lines, setLines] = useState([]);
  const [draftId, setDraftId] = useState(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [searchResults, setSearchResults] = useState([]);
  const [searching, setSearching] = useState(false);
  const [catalogError, setCatalogError] = useState('');
  const [selectedSku, setSelectedSku] = useState(null);
  const [previousPhotoSource, setPreviousPhotoSource] = useState(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [newItemName, setNewItemName] = useState('');
  const [creatingSku, setCreatingSku] = useState(false);
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
          const evidenceLines = new Set(
            (existing.evidence || []).map((item) => Number(item.receiving_line_id)).filter(Boolean),
          );
          const headerPhotoExists = (existing.evidence || []).some((item) => item.receiving_id);
          setDraftId(existing.receiving_id);
          setReference(existing.po_number || '');
          setSupplier(existing.supplier_ref || '');
          setLines((existing.lines || []).map((line) => ({
            receiving_line_id: line.receiving_line_id,
            sku_catalog_id: line.sku_catalog_id,
            sku: line.sku,
            item_name: line.item_name || line.sku,
            expected_quantity: line.expected_quantity,
            received_quantity: line.received_quantity,
            good_quantity: line.good_quantity,
            damaged_quantity: line.damaged_quantity,
            notes: line.notes || null,
            photo: null,
            photo_uploaded: headerPhotoExists || evidenceLines.has(Number(line.receiving_line_id)),
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

  useEffect(() => {
    if (draftId) return undefined;
    const query = searchQuery.trim();
    if (!query) {
      setSearchResults([]);
      setCatalogError('');
      setSearching(false);
      return undefined;
    }
    if (selectedSku && normalizeSku(selectedSku.sku) === normalizeSku(query)) {
      setSearchResults([]);
      setSearching(false);
      return undefined;
    }
    let active = true;
    const timer = setTimeout(async () => {
      setSearching(true);
      setCatalogError('');
      try {
        const response = await client.get(
          `/api/work-control/skus?warehouse_id=${warehouseId}&q=${encodeURIComponent(query)}&limit=12`,
        );
        if (active) setSearchResults(response.data.skus || []);
      } catch (searchError) {
        if (active) {
          setSearchResults([]);
          setCatalogError(message(searchError));
        }
      } finally {
        if (active) setSearching(false);
      }
    }, 250);
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [draftId, searchQuery, selectedSku, warehouseId]);

  const totalUnits = useMemo(
    () => lines.reduce((sum, line) => sum + Number(line.received_quantity || 0), 0),
    [lines],
  );

  function updateSearch(value) {
    setSearchQuery(value);
    setCreateOpen(false);
    setCatalogError('');
    if (normalizeSku(value) !== normalizeSku(selectedSku?.sku)) {
      selectionVersion.current += 1;
      setSelectedSku(null);
      setPreviousPhotoSource(null);
      setEntry((current) => ({ ...current, sku: normalizeSku(value), item_name: '', photo: null }));
    }
  }

  async function selectCatalogSku(item) {
    const version = selectionVersion.current + 1;
    selectionVersion.current = version;
    setSelectedSku(item);
    setSearchQuery(item.sku);
    setSearchResults([]);
    setCreateOpen(false);
    setCatalogError('');
    setEntry({ ...EMPTY_ENTRY, sku: item.sku, item_name: item.item_name });
    setPreviousPhotoSource(null);
    if (item.last_evidence_id) {
      const source = await getAuthenticatedAssetSource(`/api/work-control/evidence/${item.last_evidence_id}`);
      if (selectionVersion.current === version) setPreviousPhotoSource(source);
    }
  }

  async function createSku() {
    const sku = normalizeSku(searchQuery);
    if (!sku) return setCatalogError('Enter or scan the new SKU first');
    if (!newItemName.trim()) return setCatalogError('Item name is required');
    setCreatingSku(true);
    setCatalogError('');
    try {
      const response = await client.post('/api/work-control/skus', {
        warehouse_id: warehouseId,
        sku,
        item_name: newItemName.trim(),
      });
      await selectCatalogSku(response.data.sku);
      setNewItemName('');
    } catch (createError) {
      const existing = createError?.response?.data?.sku;
      if (existing) await selectCatalogSku(existing);
      else setCatalogError(message(createError));
    } finally {
      setCreatingSku(false);
    }
  }

  async function captureSkuPhoto() {
    setError('');
    try {
      const photo = await takePhoto();
      if (photo) setEntry((current) => ({ ...current, photo }));
    } catch (photoError) {
      setError(message(photoError));
    }
  }

  async function captureSavedLinePhoto(index) {
    setError('');
    try {
      const photo = await takePhoto();
      if (photo) {
        setLines((current) => current.map((line, lineIndex) => (
          lineIndex === index ? { ...line, photo } : line
        )));
      }
    } catch (photoError) {
      setError(message(photoError));
    }
  }

  function addLine() {
    setError('');
    if (draftId) return setError('This draft is already saved; finish its missing photos and submit it.');
    if (!selectedSku || normalizeSku(selectedSku.sku) !== normalizeSku(entry.sku)) {
      return setError('Select a matching SKU from the catalog first');
    }
    const expected = numberOrNull(entry.expected);
    const received = numberOrNull(entry.received);
    const damaged = numberOrNull(entry.damaged || '0');
    if (Number.isNaN(expected) || received === null || Number.isNaN(received) || Number.isNaN(damaged)) {
      return setError('Quantities must be whole numbers of zero or more');
    }
    if (damaged > received) return setError('Damaged quantity cannot exceed received quantity');
    if (!entry.photo) return setError('Take a photo of this SKU before adding the line');
    const normalizedSku = normalizeSku(entry.sku);
    if (lines.some((line) => normalizeSku(line.sku) === normalizedSku)) return setError('This SKU is already in the count');
    setLines((current) => [...current, {
      sku_catalog_id: selectedSku.sku_catalog_id,
      sku: normalizedSku,
      item_name: selectedSku.item_name,
      product_image_url: selectedSku.image_url,
      expected_quantity: expected,
      received_quantity: received,
      good_quantity: received - damaged,
      damaged_quantity: damaged,
      notes: null,
      photo: entry.photo,
      photo_uploaded: false,
    }]);
    selectionVersion.current += 1;
    setEntry(EMPTY_ENTRY);
    setSelectedSku(null);
    setPreviousPhotoSource(null);
    setSearchQuery('');
    setSearchResults([]);
  }

  async function saveAndSubmit() {
    if (!task) return setError('Receiving task is missing');
    if (!lines.length) return setError('Add at least one SKU');
    if (lines.some((line) => !line.photo && !line.photo_uploaded)) {
      return setError('Every SKU needs its own arrival photo');
    }
    setBusy(true);
    setError('');
    let receivingId = draftId;
    let workingLines = lines;
    try {
      if (!receivingId) {
        const created = await client.post('/api/work-control/receiving-drafts', {
          warehouse_id: task.warehouse_id || warehouseId,
          task_id: task.task_id,
          source_system: 'sitegiant',
          po_number: reference.trim() || null,
          supplier_ref: supplier.trim() || null,
          notes: null,
          lines: lines.map((line) => ({
            sku: line.sku,
            item_name: line.item_name,
            expected_quantity: line.expected_quantity,
            received_quantity: line.received_quantity,
            good_quantity: line.good_quantity,
            damaged_quantity: line.damaged_quantity,
            notes: line.notes,
          })),
        }, { timeout: 30000 });
        receivingId = created.data.receiving.receiving_id;
        const serverLines = created.data.receiving.lines || [];
        workingLines = lines.map((line) => ({
          ...line,
          receiving_line_id: serverLines.find(
            (serverLine) => normalizeSku(serverLine.sku) === normalizeSku(line.sku),
          )?.receiving_line_id,
        }));
        setDraftId(receivingId);
        setLines(workingLines);
      }

      for (const line of workingLines) {
        if (line.photo_uploaded) continue;
        if (!line.receiving_line_id || !line.photo) throw new Error(`Photo is missing for ${line.sku}`);
        const form = new FormData();
        form.append('receiving_line_id', String(line.receiving_line_id));
        form.append('note', `${line.sku} arrival count`);
        form.append('photo', {
          uri: line.photo.uri,
          name: line.photo.fileName || `receiving-${receivingId}-${line.sku}.jpg`,
          type: line.photo.mimeType || 'image/jpeg',
        });
        await client.upload('/api/work-control/evidence', form, { timeout: 30000 });
        workingLines = workingLines.map((item) => (
          item.receiving_line_id === line.receiving_line_id
            ? { ...item, photo_uploaded: true }
            : item
        ));
        setLines(workingLines);
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
            <Text style={styles.sectionTitle}>FIND SKU</Text>
            <ScanInput placeholder="SCAN SKU" onScan={updateSearch} suppressRefocus />
            <TextInput
              style={styles.searchInput}
              placeholder="Search iSKU or item name"
              placeholderTextColor={colors.textPlaceholder}
              value={searchQuery}
              onChangeText={updateSearch}
              autoCapitalize="characters"
              autoCorrect={false}
            />
            {searching ? <View style={styles.searching}><ActivityIndicator size="small" color={colors.copper} /><Text style={styles.searchingText}>Searching Warehouse catalog…</Text></View> : null}
            {catalogError ? <Text style={styles.catalogError}>{catalogError}</Text> : null}
            {searchResults.map((item) => (
              <TouchableOpacity key={item.sku_catalog_id} style={styles.searchResult} onPress={() => selectCatalogSku(item)} accessibilityRole="button">
                {item.image_url ? <Image source={{ uri: item.image_url }} style={styles.resultImage} /> : <View style={styles.resultImageEmpty}><Text style={styles.imageEmptyText}>NO IMG</Text></View>}
                <View style={styles.resultCopy}>
                  <Text style={styles.resultSku}>{item.sku}</Text>
                  <Text style={styles.resultName} numberOfLines={2}>{item.item_name}</Text>
                </View>
                <Text style={styles.chevron}>›</Text>
              </TouchableOpacity>
            ))}
            {!searching && !catalogError && searchQuery.trim() && !searchResults.length && !selectedSku && !createOpen ? (
              <TouchableOpacity style={styles.createPrompt} onPress={() => { setCreateOpen(true); setNewItemName(''); }}>
                <Text style={styles.createPromptTitle}>SKU not found?</Text>
                <Text style={styles.createPromptText}>Add {normalizeSku(searchQuery)} to the Warehouse catalog</Text>
              </TouchableOpacity>
            ) : null}
            {createOpen ? (
              <View style={styles.createPanel}>
                <Text style={styles.createSku}>{normalizeSku(searchQuery)}</Text>
                <TextInput style={styles.input} placeholder="Item name" placeholderTextColor={colors.textPlaceholder} value={newItemName} onChangeText={setNewItemName} maxLength={500} />
                <Text style={styles.createHelp}>Local Warehouse entry · it will not create or change a SiteGiant product.</Text>
                <TouchableOpacity style={[buttonStyles.buttonSecondary, creatingSku && buttonStyles.buttonDisabled]} onPress={createSku} disabled={creatingSku}>
                  <Text style={buttonStyles.buttonSecondaryText}>{creatingSku ? 'ADDING…' : 'ADD & SELECT SKU'}</Text>
                </TouchableOpacity>
              </View>
            ) : null}

            {selectedSku ? (
              <View style={styles.selectedPanel}>
                <View style={styles.selectedHeader}>
                  <View style={styles.selectedCopy}><Text style={styles.selectedSku}>{selectedSku.sku}</Text><Text style={styles.selectedName}>{selectedSku.item_name}</Text></View>
                  <Text style={[styles.sourceTag, selectedSku.needs_review && styles.reviewTag]}>{selectedSku.needs_review ? 'LOCAL · REVIEW' : 'SITEGIANT'}</Text>
                </View>
                <View style={styles.identityPhotos}>
                  <View style={styles.identityPhotoCell}>
                    <Text style={styles.photoLabel}>SITEGIANT ITEM</Text>
                    {selectedSku.image_url ? <Image source={{ uri: selectedSku.image_url }} style={styles.identityPhoto} resizeMode="cover" /> : <View style={styles.identityEmpty}><Text style={styles.imageEmptyText}>NO PRODUCT IMAGE</Text></View>}
                  </View>
                  <View style={styles.identityPhotoCell}>
                    <Text style={styles.photoLabel}>LAST RECEIVING</Text>
                    {previousPhotoSource ? <Image source={previousPhotoSource} style={styles.identityPhoto} resizeMode="cover" /> : <View style={styles.identityEmpty}><Text style={styles.imageEmptyText}>FIRST RECEIPT</Text></View>}
                  </View>
                </View>
                <View style={styles.quantityRow}>
                  <View style={styles.quantityField}><Text style={styles.fieldLabel}>EXPECTED</Text><TextInput style={styles.quantityInput} keyboardType="number-pad" value={entry.expected} onChangeText={(value) => setEntry({ ...entry, expected: value.replace(/\D/g, '') })} placeholder="—" placeholderTextColor={colors.textPlaceholder} /></View>
                  <View style={styles.quantityField}><Text style={styles.fieldLabel}>RECEIVED</Text><TextInput style={styles.quantityInput} keyboardType="number-pad" value={entry.received} onChangeText={(value) => setEntry({ ...entry, received: value.replace(/\D/g, '') })} placeholder="0" placeholderTextColor={colors.textPlaceholder} /></View>
                  <View style={styles.quantityField}><Text style={styles.fieldLabel}>DAMAGED</Text><TextInput style={styles.quantityInput} keyboardType="number-pad" value={entry.damaged} onChangeText={(value) => setEntry({ ...entry, damaged: value.replace(/\D/g, '') })} placeholder="0" placeholderTextColor={colors.textPlaceholder} /></View>
                </View>
                {entry.photo ? <Image source={{ uri: entry.photo.uri }} style={styles.currentPhoto} /> : <View style={styles.photoRequired}><Text style={styles.photoRequiredText}>PHOTO REQUIRED FOR THIS SKU</Text></View>}
                <TouchableOpacity style={buttonStyles.buttonSecondary} onPress={captureSkuPhoto}><Text style={buttonStyles.buttonSecondaryText}>{entry.photo ? 'RETAKE SKU PHOTO' : 'TAKE SKU PHOTO'}</Text></TouchableOpacity>
                <TouchableOpacity style={[buttonStyles.buttonPrimary, styles.addLineButton]} onPress={addLine}><Text style={buttonStyles.buttonPrimaryText}>ADD COUNTED SKU</Text></TouchableOpacity>
              </View>
            ) : null}
          </View>
        )}

        <Text style={styles.sectionTitle}>COUNTED SKU</Text>
        {lines.map((line, index) => (
          <View key={line.sku} style={listStyles.row}>
            {line.photo ? <Image source={{ uri: line.photo.uri }} style={styles.linePhoto} /> : <View style={styles.linePhotoSaved}><Text style={styles.linePhotoSavedText}>{line.photo_uploaded ? 'PHOTO\nSAVED' : 'NO\nPHOTO'}</Text></View>}
            <View style={styles.lineCopy}><Text style={listStyles.sku}>{line.sku}</Text><Text style={styles.lineName} numberOfLines={2}>{line.item_name || line.sku}</Text><Text style={listStyles.itemName}>Expected {line.expected_quantity ?? '—'} · Good {line.good_quantity} · Damaged {line.damaged_quantity}</Text></View>
            <Text style={styles.lineQty}>{line.received_quantity}</Text>
            {draftId && !line.photo_uploaded ? <TouchableOpacity style={styles.linePhotoAction} onPress={() => captureSavedLinePhoto(index)}><Text style={styles.linePhotoActionText}>{line.photo ? 'RETAKE\nPHOTO' : 'TAKE\nPHOTO'}</Text></TouchableOpacity> : null}
            {!draftId && <TouchableOpacity style={listStyles.removeBtn} onPress={() => setLines((current) => current.filter((_, itemIndex) => itemIndex !== index))}><Text style={listStyles.removeText}>×</Text></TouchableOpacity>}
          </View>
        ))}

        <TouchableOpacity style={[buttonStyles.buttonPrimary, busy && buttonStyles.buttonDisabled]} onPress={saveAndSubmit} disabled={busy}>
          <Text style={buttonStyles.buttonPrimaryText}>{busy ? 'SENDING…' : 'SUBMIT TO STOCK CLERK · NEXT TASK'}</Text>
        </TouchableOpacity>
        <Text style={styles.footerNote}>Every SKU keeps its own count photo. Submission creates a Draft GRN only; stock posting remains with the stock clerk.</Text>
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
  sectionTitle: { fontFamily: fonts.mono, fontSize: 11, fontWeight: '700', color: colors.textSecondary, letterSpacing: 0.7, marginBottom: 9 },
  input: { minHeight: 48, borderWidth: 1, borderColor: colors.inputBorder, borderRadius: radii.input, backgroundColor: colors.inputBg, paddingHorizontal: 11, color: colors.textPrimary, fontSize: 16, marginBottom: 8 },
  searchInput: { minHeight: 50, borderWidth: 1.5, borderColor: colors.copper, borderRadius: radii.input, backgroundColor: colors.background, paddingHorizontal: 12, color: colors.textPrimary, fontSize: 16, marginBottom: 8 },
  searching: { flexDirection: 'row', alignItems: 'center', gap: 8, paddingVertical: 8 },
  searchingText: { color: colors.textMuted, fontSize: 12 },
  catalogError: { color: colors.danger, fontSize: 12, lineHeight: 18, marginBottom: 8 },
  searchResult: { flexDirection: 'row', alignItems: 'center', minHeight: 68, paddingVertical: 8, borderBottomWidth: 1, borderBottomColor: colors.cardBorder },
  resultImage: { width: 52, height: 52, borderRadius: radii.small, backgroundColor: colors.cardBg },
  resultImageEmpty: { width: 52, height: 52, borderRadius: radii.small, alignItems: 'center', justifyContent: 'center', backgroundColor: colors.cardBg },
  resultCopy: { flex: 1, minWidth: 0, marginHorizontal: 10 },
  resultSku: { fontFamily: fonts.mono, color: colors.accentRed, fontSize: 13, fontWeight: '700' },
  resultName: { color: colors.textPrimary, fontSize: 13, lineHeight: 18, marginTop: 3 },
  chevron: { fontSize: 24, color: colors.copper, paddingHorizontal: 4 },
  createPrompt: { paddingVertical: 13, borderTopWidth: 1, borderTopColor: colors.cardBorder },
  createPromptTitle: { color: colors.accentRed, fontSize: 14, fontWeight: '700' },
  createPromptText: { color: colors.textMuted, fontSize: 12, marginTop: 3 },
  createPanel: { marginTop: 8, padding: 12, borderRadius: radii.card, backgroundColor: colors.cardBg },
  createSku: { fontFamily: fonts.mono, fontSize: 18, fontWeight: '700', color: colors.textPrimary, marginBottom: 8 },
  createHelp: { color: colors.textMuted, fontSize: 11, lineHeight: 16, marginBottom: 10 },
  selectedPanel: { marginTop: 12, paddingTop: 13, borderTopWidth: 1, borderTopColor: colors.cardBorder },
  selectedHeader: { flexDirection: 'row', alignItems: 'flex-start', gap: 8 },
  selectedCopy: { flex: 1, minWidth: 0 },
  selectedSku: { fontFamily: fonts.mono, fontSize: 18, color: colors.accentRed, fontWeight: '700' },
  selectedName: { color: colors.textPrimary, fontSize: 14, lineHeight: 20, marginTop: 4 },
  sourceTag: { fontFamily: fonts.mono, fontSize: 8, color: colors.success, borderWidth: 1, borderColor: colors.success, borderRadius: radii.badge, paddingHorizontal: 6, paddingVertical: 4 },
  reviewTag: { color: colors.warning, borderColor: colors.warning },
  identityPhotos: { flexDirection: 'row', gap: 8, marginVertical: 12 },
  identityPhotoCell: { flex: 1, minWidth: 0 },
  photoLabel: { fontFamily: fonts.mono, color: colors.textMuted, fontSize: 8, marginBottom: 4 },
  identityPhoto: { width: '100%', aspectRatio: 1, borderRadius: radii.small, backgroundColor: colors.cardBg },
  identityEmpty: { width: '100%', aspectRatio: 1, borderRadius: radii.small, alignItems: 'center', justifyContent: 'center', backgroundColor: colors.cardBg, padding: 8 },
  imageEmptyText: { fontFamily: fonts.mono, textAlign: 'center', color: colors.textPlaceholder, fontSize: 8 },
  quantityRow: { flexDirection: 'row', gap: 7, marginBottom: 10 },
  quantityField: { flex: 1 },
  fieldLabel: { fontFamily: fonts.mono, fontSize: 8, color: colors.textMuted, textAlign: 'center', marginBottom: 3 },
  quantityInput: { minHeight: 50, borderWidth: 1, borderColor: colors.inputBorder, borderRadius: radii.input, backgroundColor: colors.inputBg, fontFamily: fonts.mono, textAlign: 'center', fontSize: 18, color: colors.textPrimary },
  currentPhoto: { width: '100%', height: 190, borderRadius: radii.small, marginBottom: 8, backgroundColor: colors.cardBg },
  photoRequired: { height: 74, alignItems: 'center', justifyContent: 'center', borderWidth: 1, borderStyle: 'dashed', borderColor: colors.inputBorder, borderRadius: radii.small, marginBottom: 8 },
  photoRequiredText: { fontFamily: fonts.mono, color: colors.textPlaceholder, fontSize: 9 },
  addLineButton: { marginTop: 8 },
  linePhoto: { width: 52, height: 52, borderRadius: radii.small, marginRight: 9, backgroundColor: colors.background },
  linePhotoSaved: { width: 52, height: 52, borderRadius: radii.small, marginRight: 9, backgroundColor: colors.background, alignItems: 'center', justifyContent: 'center' },
  linePhotoSavedText: { fontFamily: fonts.mono, color: colors.success, fontSize: 8, lineHeight: 11, textAlign: 'center' },
  lineCopy: { flex: 1, minWidth: 0 },
  lineName: { color: colors.textSecondary, fontSize: 12, lineHeight: 16, marginTop: 2 },
  lineQty: { fontFamily: fonts.mono, fontSize: 22, fontWeight: '700', color: colors.accentRed, minWidth: 40, textAlign: 'center' },
  linePhotoAction: { minWidth: 48, minHeight: 48, alignItems: 'center', justifyContent: 'center', marginLeft: 4 },
  linePhotoActionText: { fontFamily: fonts.mono, color: colors.accentRed, fontSize: 8, lineHeight: 11, textAlign: 'center', fontWeight: '700' },
  footerNote: { color: colors.textMuted, fontSize: 11, textAlign: 'center', lineHeight: 16, marginTop: 9, paddingHorizontal: 12 },
});
