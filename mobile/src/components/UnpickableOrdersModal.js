import React from 'react';
import { Modal, View, Text, ScrollView, TouchableOpacity, StyleSheet } from 'react-native';
import { colors, fonts, radii } from '../theme/styles';

export default function UnpickableOrdersModal({ visible, unpickable, onCancel, onContinue }) {
  const count = unpickable?.length || 0;
  return (
    <Modal visible={visible} transparent animationType="fade">
      <View style={styles.overlay}>
        <View style={styles.card}>
          <Text style={styles.title}>INSUFFICIENT STOCK</Text>
          <Text style={styles.body}>
            {count === 1 ? '1 order does' : `${count} orders do`} not have pickable
            stock in this warehouse. Continue to pick the orders that are ready to
            be fulfilled, or cancel to adjust the batch.
          </Text>

          <ScrollView style={styles.list} contentContainerStyle={{ paddingBottom: 8 }}>
            {(unpickable || []).map((so) => (
              <View key={so.so_id} style={styles.soBlock}>
                <Text style={styles.soNumber}>{so.so_number}</Text>
                {(so.lines || []).map((ln, idx) => (
                  <Text key={`${so.so_id}-${idx}`} style={styles.lineRow}>
                    {ln.sku} · ordered {ln.ordered} · available {ln.available}
                  </Text>
                ))}
              </View>
            ))}
          </ScrollView>

          <View style={styles.buttonRow}>
            <TouchableOpacity style={[styles.button, styles.buttonCancel]} onPress={onCancel}>
              <Text style={styles.buttonCancelText}>CANCEL</Text>
            </TouchableOpacity>
            <TouchableOpacity style={[styles.button, styles.buttonContinue]} onPress={onContinue}>
              <Text style={styles.buttonContinueText}>CONTINUE</Text>
            </TouchableOpacity>
          </View>
        </View>
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  overlay: {
    flex: 1,
    backgroundColor: colors.overlay,
    justifyContent: 'center',
    alignItems: 'center',
    padding: 24,
  },
  card: {
    backgroundColor: colors.background,
    borderRadius: radii.card,
    borderWidth: 1.5,
    borderColor: colors.accentRed,
    padding: 20,
    width: '100%',
    maxWidth: 420,
    maxHeight: '85%',
  },
  title: {
    fontFamily: fonts.mono,
    fontSize: 14,
    fontWeight: '700',
    letterSpacing: 0.5,
    color: colors.accentRed,
    textAlign: 'center',
    marginBottom: 8,
  },
  body: {
    fontSize: 13,
    color: colors.textPrimary,
    textAlign: 'center',
    lineHeight: 19,
    marginBottom: 12,
  },
  list: {
    maxHeight: 320,
    marginBottom: 12,
  },
  soBlock: {
    paddingVertical: 8,
    paddingHorizontal: 4,
    borderBottomWidth: 1,
    borderBottomColor: colors.border || '#333',
  },
  soNumber: {
    fontFamily: fonts.mono,
    fontSize: 13,
    fontWeight: '700',
    color: colors.textPrimary,
    marginBottom: 4,
  },
  lineRow: {
    fontFamily: fonts.mono,
    fontSize: 12,
    color: colors.textMuted,
    paddingLeft: 8,
    lineHeight: 17,
  },
  buttonRow: {
    flexDirection: 'row',
    gap: 12,
  },
  button: {
    flex: 1,
    borderRadius: radii.button,
    paddingVertical: 12,
    alignItems: 'center',
    justifyContent: 'center',
    minHeight: 44,
  },
  buttonCancel: {
    backgroundColor: 'transparent',
    borderWidth: 1.5,
    borderColor: colors.textMuted,
  },
  buttonCancelText: {
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: 13,
    fontWeight: '700',
    letterSpacing: 0.5,
  },
  buttonContinue: {
    backgroundColor: colors.accentRed,
  },
  buttonContinueText: {
    color: colors.cream,
    fontFamily: fonts.mono,
    fontSize: 13,
    fontWeight: '700',
    letterSpacing: 0.5,
  },
});
