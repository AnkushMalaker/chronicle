import React, { useState } from 'react';
import { StyleSheet, Alert } from 'react-native';

import { Button, Caption, Card, TextField } from '@/components/ui';
import { useTheme, type Theme } from '@/theme';

interface ObsidianIngestProps {
  backendUrl: string;
  jwtToken: string | null;
}

export const ObsidianIngest: React.FC<ObsidianIngestProps> = ({
  backendUrl,
  jwtToken,
}) => {
  const t = useTheme();
  const s = createStyles(t);
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
    <Card title="Obsidian Ingestion">
      <TextField
        label="Vault Path (Backend Container):"
        value={vaultPath}
        onChangeText={setVaultPath}
        placeholder="/app/data/obsidian_vault"
        autoCapitalize="none"
        autoCorrect={false}
      />

      <Button
        variant="primary"
        size="lg"
        fullWidth
        loading={loading}
        onPress={handleIngest}
        style={s.button}
      >
        {loading ? 'Starting Ingestion...' : 'Ingest to FalkorDB'}
      </Button>

      <Caption style={s.helpText}>
        Enter the absolute path to the Obsidian vault INSIDE the backend container.
        Ensure the folder is mounted to the container.
      </Caption>
    </Card>
  );
};

const createStyles = (t: Theme) => StyleSheet.create({
  button: {
    marginBottom: t.space[3],
  },
  helpText: {
    textAlign: 'center',
    fontStyle: 'italic',
  },
});

export default ObsidianIngest;
