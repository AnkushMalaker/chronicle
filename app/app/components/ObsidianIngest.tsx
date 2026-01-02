
import React, { useState } from 'react';
import { View, Text, TextInput, TouchableOpacity, StyleSheet, Alert, ActivityIndicator } from 'react-native';

interface ObsidianIngestProps {
  backendUrl: string;
  jwtToken: string | null;
}

export const ObsidianIngest: React.FC<ObsidianIngestProps> = ({
  backendUrl,
  jwtToken,
}) => {
  const [vaultPath, setVaultPath] = useState('/app/data/obsidian_vault');
  const [loading, setLoading] = useState(false);

  const handleIngest = async () => {
    if (!backendUrl) {
        Alert.alert("Error", "Backend URL not set");
        return;
    }

    if (!jwtToken) {
        Alert.alert("Authentication Required", "Please login to ingest Obsidian vault.");
        return;
    }

    setLoading(true);
    try {
        let baseUrl = backendUrl.trim();
        // Handle different URL formats
        if (baseUrl.startsWith('ws://')) {
            baseUrl = baseUrl.replace('ws://', 'http://');
        } else if (baseUrl.startsWith('wss://')) {
            baseUrl = baseUrl.replace('wss://', 'https://');
        }
        baseUrl = baseUrl.split('/ws')[0];

        const response = await fetch(`${baseUrl}/api/obsidian/ingest`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${jwtToken}`
            },
            body: JSON.stringify({ vault_path: vaultPath })
        });

        if (response.ok) {
            Alert.alert("Success", "Ingestion started in background.");
        } else {
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
    <View style={styles.section}>
      <Text style={styles.sectionTitle}>Obsidian Ingestion</Text>
      
      <Text style={styles.inputLabel}>Vault Path (Backend Container):</Text>
      <TextInput
        style={styles.textInput}
        value={vaultPath}
        onChangeText={setVaultPath}
        placeholder="/app/data/obsidian_vault"
        autoCapitalize="none"
        autoCorrect={false}
      />
      
      <TouchableOpacity
        style={[styles.button, loading ? styles.buttonDisabled : null]}
        onPress={handleIngest}
        disabled={loading}
      >
        <Text style={styles.buttonText}>
          {loading ? 'Starting Ingestion...' : 'Ingest to Neo4j'}
        </Text>
      </TouchableOpacity>

      <Text style={styles.helpText}>
        Enter the absolute path to the Obsidian vault INSIDE the backend container.
        Ensure the folder is mounted to the container.
      </Text>
    </View>
  );
};

const styles = StyleSheet.create({
  section: {
    marginBottom: 25,
    padding: 15,
    backgroundColor: 'white',
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
    color: '#333',
  },
  inputLabel: {
    fontSize: 14,
    color: '#333',
    marginBottom: 5,
    fontWeight: '500',
  },
  textInput: {
    backgroundColor: '#f0f0f0',
    borderWidth: 1,
    borderColor: '#ddd',
    borderRadius: 6,
    padding: 10,
    fontSize: 14,
    width: '100%',
    marginBottom: 15,
    color: '#333',
  },
  button: {
    backgroundColor: '#9b59b6', // Purple for Obsidian
    paddingVertical: 12,
    paddingHorizontal: 20,
    borderRadius: 8,
    alignItems: 'center',
    marginBottom: 10,
    elevation: 2,
  },
  buttonDisabled: {
    backgroundColor: '#A0A0A0',
    opacity: 0.7,
  },
  buttonText: {
    color: 'white',
    fontSize: 16,
    fontWeight: '600',
  },
  helpText: {
    fontSize: 12,
    color: '#666',
    textAlign: 'center',
    fontStyle: 'italic',
  },
});

export default ObsidianIngest;
