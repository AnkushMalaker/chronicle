export const WEARABLE_SERVICE_UUID = '19b10000-e8f2-537e-4f6c-d104768a1214';
export const NEO_ACTIVE_CONTROL_UUID = '19b10003-e8f2-537e-4f6c-d104768a1214';
export const NEO_ACTIVE_VALUE_BASE64 = 'AQ==';

interface WearableCharacteristic {
  uuid: string;
  isWritableWithResponse: boolean;
}

export interface WearableControlTransport {
  characteristicsForDevice(
    deviceId: string,
    serviceUuid: string,
  ): Promise<WearableCharacteristic[]>;
  writeCharacteristicWithResponseForDevice(
    deviceId: string,
    serviceUuid: string,
    characteristicUuid: string,
    valueBase64: string,
  ): Promise<unknown>;
}

export type WearableActivationResult = 'neo_activated' | 'not_required';

export async function activateWearableAfterConnect(
  transport: WearableControlTransport,
  deviceId: string,
): Promise<WearableActivationResult> {
  const characteristics = await transport.characteristicsForDevice(
    deviceId,
    WEARABLE_SERVICE_UUID,
  );
  const neoControl = characteristics.find(
    (characteristic) => characteristic.uuid.toLowerCase() === NEO_ACTIVE_CONTROL_UUID,
  );

  if (!neoControl) {
    return 'not_required';
  }
  if (!neoControl.isWritableWithResponse) {
    throw new Error('Neo Active control does not support writes with response');
  }

  await transport.writeCharacteristicWithResponseForDevice(
    deviceId,
    WEARABLE_SERVICE_UUID,
    NEO_ACTIVE_CONTROL_UUID,
    NEO_ACTIVE_VALUE_BASE64,
  );
  return 'neo_activated';
}
