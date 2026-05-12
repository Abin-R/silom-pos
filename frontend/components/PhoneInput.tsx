/**
 * PhoneInput — country picker + number entry with inline validation.
 *
 * Stored value is E.164 (+<country><digits>).  When the parent passes
 * `value`, it's parsed back into country + local digits on mount.
 *
 *   <PhoneInput
 *     value={phone}                       // e.g. "+66912345678"
 *     onChange={(e164, valid) => ...}     // both updated as user types
 *   />
 */
import React, { useEffect, useMemo, useState } from 'react';
import {
  FlatList,
  Modal,
  StyleSheet,
  Text,
  TextInput,
  TouchableOpacity,
  View,
} from 'react-native';

import {
  COUNTRIES,
  Country,
  DEFAULT_COUNTRY,
  normalizeLocal,
  parseE164,
  validatePhone,
} from '../lib/phone';

type Props = {
  value: string;
  onChange: (e164: string, valid: boolean) => void;
  placeholder?: string;
  /** ISO code (e.g. "TH") to default to when value is empty */
  defaultCountryCode?: string;
  testID?: string;
};

export default function PhoneInput({
  value,
  onChange,
  placeholder = 'Phone number',
  defaultCountryCode,
  testID,
}: Props) {
  const initial = useMemo(() => {
    if (value) return parseE164(value);
    const c =
      COUNTRIES.find((x) => x.code === defaultCountryCode) || DEFAULT_COUNTRY;
    return { country: c, local: '' };
  }, []); // intentionally not re-deriving from `value` after mount

  const [country, setCountry] = useState<Country>(initial.country);
  const [local, setLocal] = useState<string>(initial.local);
  const [showPicker, setShowPicker] = useState(false);

  const validation = validatePhone(country, local);

  // Propagate every change upward so the parent always sees the latest E.164.
  useEffect(() => {
    onChange(validation.valid ? validation.e164 : '', validation.valid);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [country.code, local]);

  return (
    <View>
      <View style={styles.row}>
        <TouchableOpacity
          style={styles.countryBtn}
          onPress={() => setShowPicker(true)}
          testID={testID ? `${testID}-country` : undefined}
        >
          <Text style={styles.flag}>{country.flag}</Text>
          <Text style={styles.dial}>+{country.dial}</Text>
          <Text style={styles.chev}>▾</Text>
        </TouchableOpacity>
        <TextInput
          style={styles.input}
          value={local}
          onChangeText={(v) => setLocal(normalizeLocal(country, v))}
          placeholder={placeholder}
          placeholderTextColor="#94A3B8"
          keyboardType="phone-pad"
          testID={testID}
          autoCorrect={false}
        />
      </View>

      {/* Inline feedback — only shown when the user has typed something */}
      {local !== '' && (
        <Text
          style={[
            styles.feedback,
            validation.valid ? styles.feedbackOk : styles.feedbackErr,
          ]}
        >
          {validation.valid
            ? `✓ ${validation.display}`
            : `⚠ ${(validation as any).reason}`}
        </Text>
      )}

      {/* Country picker modal */}
      <Modal visible={showPicker} transparent animationType="fade" onRequestClose={() => setShowPicker(false)}>
        <TouchableOpacity
          style={styles.modalOverlay}
          activeOpacity={1}
          onPress={() => setShowPicker(false)}
        >
          <View style={styles.modalSheet}>
            <Text style={styles.modalTitle}>Choose Country</Text>
            <FlatList
              data={COUNTRIES}
              keyExtractor={(c) => c.code}
              renderItem={({ item }) => (
                <TouchableOpacity
                  style={[
                    styles.countryRow,
                    item.code === country.code && styles.countryRowActive,
                  ]}
                  onPress={() => {
                    setCountry(item);
                    setShowPicker(false);
                  }}
                  testID={`country-${item.code}`}
                >
                  <Text style={styles.flag}>{item.flag}</Text>
                  <Text style={styles.rowName}>{item.name}</Text>
                  <Text style={styles.rowDial}>+{item.dial}</Text>
                </TouchableOpacity>
              )}
            />
          </View>
        </TouchableOpacity>
      </Modal>
    </View>
  );
}

const styles = StyleSheet.create({
  row: {
    flexDirection: 'row',
    gap: 8,
    alignItems: 'center',
  },
  countryBtn: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
    paddingHorizontal: 10,
    paddingVertical: 12,
    borderRadius: 8,
    borderWidth: 1,
    borderColor: '#E2E8F0',
    backgroundColor: '#FFFFFF',
    minWidth: 96,
  },
  flag: { fontSize: 18 },
  dial: { fontSize: 14, fontWeight: '600', color: '#0F172A' },
  chev: { fontSize: 12, color: '#94A3B8', marginLeft: 2 },
  input: {
    flex: 1,
    borderWidth: 1,
    borderColor: '#E2E8F0',
    borderRadius: 8,
    paddingHorizontal: 12,
    paddingVertical: 12,
    fontSize: 14,
    color: '#0F172A',
    backgroundColor: '#FFFFFF',
  },
  feedback: { fontSize: 12, marginTop: 4, marginLeft: 4 },
  feedbackOk: { color: '#10B981' },
  feedbackErr: { color: '#EF4444' },

  modalOverlay: {
    flex: 1,
    backgroundColor: 'rgba(15,23,42,0.45)',
    justifyContent: 'center',
    padding: 20,
  },
  modalSheet: {
    backgroundColor: '#FFFFFF',
    borderRadius: 16,
    maxHeight: '70%',
    overflow: 'hidden',
  },
  modalTitle: {
    fontSize: 16,
    fontWeight: '700',
    color: '#0F172A',
    padding: 16,
    borderBottomWidth: 1,
    borderBottomColor: '#F1F5F9',
  },
  countryRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 12,
    paddingHorizontal: 16,
    paddingVertical: 14,
    borderBottomWidth: 1,
    borderBottomColor: '#F8FAFC',
  },
  countryRowActive: { backgroundColor: '#F0FDF4' },
  rowName: { flex: 1, fontSize: 14, color: '#0F172A', fontWeight: '500' },
  rowDial: { fontSize: 13, color: '#64748B', fontWeight: '600' },
});
