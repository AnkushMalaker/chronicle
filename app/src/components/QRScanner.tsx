import React, { useState, useEffect } from 'react';
import {
  View,
  Text,
  TouchableOpacity,
  StyleSheet,
  Modal,
  Alert,
} from 'react-native';
import { CameraView, useCameraPermissions, scanFromURLAsync } from 'expo-camera';
import * as ImagePicker from 'expo-image-picker';
import { isValidBackendUrl } from '../utils/urlConversion';
import { useTheme, ThemeColors } from '../theme';

interface QRScannerProps {
  visible: boolean;
  onScanned: (url: string) => void;
  onClose: () => void;
}

export const QRScanner: React.FC<QRScannerProps> = ({ visible, onScanned, onClose }) => {
  const { colors } = useTheme();
  const s = createStyles(colors);
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

    if (isValidBackendUrl(data)) {
      onScanned(data);
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
      return <Text style={s.messageText}>Requesting camera permission...</Text>;
    }

    if (!permission.granted) {
      return (
        <View style={s.permissionContainer}>
          <Text style={s.messageText}>Camera access is needed to scan QR codes.</Text>
          <TouchableOpacity style={s.permissionButton} onPress={requestPermission}>
            <Text style={s.permissionButtonText}>Grant Camera Access</Text>
          </TouchableOpacity>
          <Text style={s.orText}>or</Text>
          <TouchableOpacity style={s.galleryButton} onPress={handlePickFromGallery}>
            <Text style={s.galleryButtonText}>Pick from Gallery</Text>
          </TouchableOpacity>
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
        <TouchableOpacity style={s.galleryButton} onPress={handlePickFromGallery}>
          <Text style={s.galleryButtonText}>Pick from Gallery</Text>
        </TouchableOpacity>
      </View>
    );
  };

  return (
    <Modal visible={visible} animationType="slide" presentationStyle="pageSheet">
      <View style={s.container}>
        <View style={s.header}>
          <Text style={s.headerTitle}>Scan QR Code</Text>
          <TouchableOpacity onPress={onClose} style={s.closeButton}>
            <Text style={s.closeButtonText}>Close</Text>
          </TouchableOpacity>
        </View>
        {renderContent()}
      </View>
    </Modal>
  );
};

const createStyles = (colors: ThemeColors) =>
  StyleSheet.create({
    container: {
      flex: 1,
      backgroundColor: colors.background,
    },
    header: {
      flexDirection: 'row',
      justifyContent: 'space-between',
      alignItems: 'center',
      paddingHorizontal: 20,
      paddingTop: 60,
      paddingBottom: 15,
      borderBottomWidth: 1,
      borderBottomColor: colors.separator,
      backgroundColor: colors.card,
    },
    headerTitle: {
      fontSize: 18,
      fontWeight: '600',
      color: colors.text,
    },
    closeButton: {
      padding: 8,
    },
    closeButtonText: {
      fontSize: 16,
      color: colors.primary,
      fontWeight: '500',
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
      top: 40,
      left: 20,
      right: 20,
      alignItems: 'center',
    },
    overlayText: {
      color: '#ffffff',
      fontSize: 16,
      fontWeight: '500',
      textAlign: 'center',
      backgroundColor: 'rgba(0,0,0,0.5)',
      paddingHorizontal: 16,
      paddingVertical: 8,
      borderRadius: 8,
      overflow: 'hidden',
    },
    permissionContainer: {
      flex: 1,
      justifyContent: 'center',
      alignItems: 'center',
      padding: 30,
    },
    messageText: {
      fontSize: 16,
      color: colors.textSecondary,
      textAlign: 'center',
      marginBottom: 20,
    },
    permissionButton: {
      backgroundColor: colors.primary,
      paddingVertical: 12,
      paddingHorizontal: 24,
      borderRadius: 8,
    },
    permissionButtonText: {
      color: '#ffffff',
      fontSize: 16,
      fontWeight: '600',
    },
    orText: {
      fontSize: 14,
      color: colors.textTertiary,
      marginVertical: 12,
    },
    galleryButton: {
      paddingVertical: 12,
      paddingHorizontal: 24,
      borderRadius: 8,
      borderWidth: 1,
      borderColor: colors.primary,
      marginTop: 12,
      marginBottom: 20,
    },
    galleryButtonText: {
      color: colors.primary,
      fontSize: 16,
      fontWeight: '500',
      textAlign: 'center',
    },
  });

export default QRScanner;
