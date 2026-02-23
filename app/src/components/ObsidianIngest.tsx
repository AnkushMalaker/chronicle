import React, { useState } from 'react';
import { View, Text, TextInput, TouchableOpacity, StyleSheet, Alert } from 'react-native';
import { useTheme, ThemeColors } from '../theme';

interface ObsidianIngestProps {
  backendUrl: string;
  jwtToken: string | null;
}

export const ObsidianIngest: React.FC<ObsidianIngestProps> = ({
  backendUrl,
  jwtToken,
}) => {
  const { colors } = useTheme();
  const s = createStyles(colors);
  const [vaultPath, setVaultPath] = useState('/app/data/obsidian_vault');
  const [loading, setLoading] = useState(false);

  const handleIngest = async () => {
    if (!backendUrl) { Alert.alert("Error", "Backend URL not set"); return; }
    if (!jwtToken) { Alert.alert("Authentication Required", "Please login to ingest Obsidian vault."); return; }

    setLoading(true);
    try {
      let baseUrl = backendUrl.trim();
      if (baseUrl.startsWith('ws://')) baseUrl = baseUrl.replace('ws://', 'http://');
      else if (baseUrl.startsWith('wss://')) baseUrl = baseUrl.replace('wss://', 'https://');
      baseUrl = baseUrl.split('/ws')[0];

      const response = await fetch(`${baseUrl}/api/obsidian/ingest`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${jwtToken}` },
        body: JSON.stringify({ vault_path: vaultPath })
      });

      if (response.ok) Alert.alert("Success", "Ingestion started in background.");
      else {
        const errorText = await response.text();
        Alert.alert("Error", `Ingestion failed: ${response.status} - ${errorText}`);
      }
    } catch (e) {
      Alert.alert("Error", `Network request failed: ${e}`);
    } finally {
      setLoading(false);
    }
  };

  return (
    <View style={s.section}>
      <Text style={s.sectionTitle}>Obsidian Ingestion</Text>

      <Text style={s.inputLabel}>Vault Path (Backend Container):</Text>
      <TextInput
        style={s.textInput}
        value={vaultPath}
        onChangeText={setVaultPath}
        placeholder="/app/data/obsidian_vault"
        placeholderTextColor={colors.textTertiary}
        autoCapitalize="none"
        autoCorrect={false}
      />

      <TouchableOpacity
        style={[s.button, loading ? s.buttonDisabled : null]}
        onPress={handleIngest}
        disabled={loading}
      >
        <Text style={s.buttonText}>{loading ? 'Starting Ingestion...' : 'Ingest to Neo4j'}</Text>
      </TouchableOpacity>

      <Text style={s.helpText}>
        Enter the absolute path to the Obsidian vault INSIDE the backend container.
        Ensure the folder is mounted to the container.
      </Text>
    </View>
  );
};

const createStyles = (colors: ThemeColors) => StyleSheet.create({
  section: {
    marginBottom: 25,
    padding: 15,
    backgroundColor: colors.card,
    borderRadius: 10,
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 1 },
    shadowOpacity: 0.1,
    shadowRadius: 3,
    elevation: 2,
  },
  sectionTitle: {
    fontSize: 18,
    fontWeight: '600',
    marginBottom: 15,
    color: colors.text,
  },
  inputLabel: {
    fontSize: 14,
    color: colors.text,
    marginBottom: 5,
    fontWeight: '500',
  },
  textInput: {
    backgroundColor: colors.inputBackground,
    borderWidth: 1,
    borderColor: colors.inputBorder,
    borderRadius: 6,
    padding: 10,
    fontSize: 14,
    width: '100%',
    marginBottom: 15,
    color: colors.text,
  },
  button: {
    backgroundColor: '#9b59b6',
    paddingVertical: 12,
    paddingHorizontal: 20,
    borderRadius: 8,
    alignItems: 'center',
    marginBottom: 10,
    elevation: 2,
  },
  buttonDisabled: {
    backgroundColor: colors.disabled,
    opacity: 0.7,
  },
  buttonText: {
    color: 'white',
    fontSize: 16,
    fontWeight: '600',
  },
  helpText: {
    fontSize: 12,
    color: colors.textTertiary,
    textAlign: 'center',
    fontStyle: 'italic',
  },
});

export default ObsidianIngest;
