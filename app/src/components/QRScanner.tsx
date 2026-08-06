import React, { useState, useEffect } from 'react';
import {
  View,
  Text,
  StyleSheet,
  Modal,
  Alert,
} from 'react-native';
import { CameraView, useCameraPermissions, scanFromURLAsync } from 'expo-camera';
import * as ImagePicker from 'expo-image-picker';

import { Body, Button, Caption, Heading } from '@/components/ui';
import { darkTheme, useTheme, type Theme } from '@/theme';
import { parseScannedConfig, ScannedBackendConfig } from '@/utils/urlConversion';

interface QRScannerProps {
  visible: boolean;
  onScanned: (config: ScannedBackendConfig) => void;
  onClose: () => void;
}

export const QRScanner: React.FC<QRScannerProps> = ({ visible, onScanned, onClose }) => {
  const t = useTheme();
  const s = createStyles(t);
  const [permission, requestPermission] = useCameraPermissions();
  const [scanned, setScanned] = useState(false);

  useEffect(() => {
    if (visible) {
      setScanned(false);
    }
  }, [visible]);

  const handleBarCodeScanned = ({ data }: { data: string }) => {
    if (scanned) return;
    setScanned(true);

    const config = parseScannedConfig(data);
    if (config) {
      onScanned(config);
      onClose();
    } else {
      Alert.alert(
        'Invalid QR Code',
        'The scanned QR code does not contain a valid backend URL. Please scan the QR code from the Chronicle dashboard.',
        [{ text: 'Try Again', onPress: () => setScanned(false) }]
      );
    }
  };

  const handlePickFromGallery = async () => {
    try {
      const result = await ImagePicker.launchImageLibraryAsync({
        mediaTypes: ['images'],
        quality: 1,
      });

      if (result.canceled || !result.assets?.[0]?.uri) return;

      const scanResult = await scanFromURLAsync(result.assets[0].uri, ['qr']);

      if (scanResult.length > 0 && scanResult[0].data) {
        handleBarCodeScanned({ data: scanResult[0].data });
      } else {
        Alert.alert('No QR Code Found', 'Could not find a QR code in the selected image.');
      }
    } catch (error) {
      console.log('[QRScanner] Gallery scan error:', error);
      Alert.alert('Error', 'Failed to scan QR code from image.');
    }
  };

  const renderContent = () => {
    if (!permission) {
      return <Body style={s.messageText}>Requesting camera permission...</Body>;
    }

    if (!permission.granted) {
      return (
        <View style={s.permissionContainer}>
          <Body style={s.messageText}>Camera access is needed to scan QR codes.</Body>
          <Button variant="primary" size="lg" onPress={requestPermission}>
            Grant Camera Access
          </Button>
          <Caption style={s.orText}>or</Caption>
          <Button variant="outline" size="lg" onPress={handlePickFromGallery} style={s.galleryButton}>
            Pick from Gallery
          </Button>
        </View>
      );
    }

    return (
      <View style={s.cameraContainer}>
        <CameraView
          style={s.camera}
          facing="back"
          barcodeScannerSettings={{ barcodeTypes: ['qr'] }}
          onBarcodeScanned={scanned ? undefined : handleBarCodeScanned}
        />
        <View style={s.overlay}>
          <Text style={s.overlayText}>Point at QR code on Chronicle dashboard</Text>
        </View>
        <Button variant="outline" size="lg" onPress={handlePickFromGallery} style={s.galleryButton}>
          Pick from Gallery
        </Button>
      </View>
    );
  };

  return (
    <Modal visible={visible} animationType="slide" presentationStyle="pageSheet">
      <View style={s.container}>
        <View style={s.header}>
          <Heading>Scan QR Code</Heading>
          <Button variant="link" size="sm" onPress={onClose}>
            Close
          </Button>
        </View>
        {renderContent()}
      </View>
    </Modal>
  );
};

const createStyles = (t: Theme) =>
  StyleSheet.create({
    container: {
      flex: 1,
      backgroundColor: t.color.surface.page,
    },
    header: {
      flexDirection: 'row',
      justifyContent: 'space-between',
      alignItems: 'center',
      paddingHorizontal: t.space[5],
      paddingTop: t.space[12] + t.space[3],
      paddingBottom: t.space[4],
      borderBottomWidth: t.borderWidth,
      borderBottomColor: t.color.border.subtle,
      backgroundColor: t.color.surface.raised,
    },
    cameraContainer: {
      flex: 1,
      alignItems: 'center',
    },
    camera: {
      flex: 1,
      width: '100%',
    },
    overlay: {
      position: 'absolute',
      top: t.space[10],
      left: t.space[5],
      right: t.space[5],
      alignItems: 'center',
    },
    overlayText: {
      // This label sits on a scrim over the live camera feed, which is dark in
      // both themes — so it always needs the dark theme's light ink, not the
      // active theme's text colour (which is near-black under the light theme).
      color: darkTheme.color.text.primary,
      backgroundColor: t.color.overlay,
      fontFamily: t.font.sans,
      ...t.type.base,
      fontWeight: t.weight.medium,
      textAlign: 'center',
      paddingHorizontal: t.space[4],
      paddingVertical: t.space[2],
      borderRadius: t.radius.lg,
      overflow: 'hidden',
    },
    permissionContainer: {
      flex: 1,
      justifyContent: 'center',
      alignItems: 'center',
      padding: t.space[8],
    },
    messageText: {
      ...t.type.base,
      textAlign: 'center',
      marginBottom: t.space[5],
    },
    orText: {
      marginVertical: t.space[3],
    },
    galleryButton: {
      marginTop: t.space[3],
      marginBottom: t.space[5],
    },
  });

export default QRScanner;
