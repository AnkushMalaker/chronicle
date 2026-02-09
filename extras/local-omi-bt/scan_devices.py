"""Quick BLE scanner to find neo1/neosapien devices."""
import asyncio
from bleak import BleakScanner, BleakClient

async def scan_all_devices():
    """Scan for all BLE devices."""
    print("Scanning for BLE devices (10 seconds)...")
    devices = await BleakScanner.discover(timeout=10.0)

    print(f"\nFound {len(devices)} devices:\n")

    neo_devices = []
    for d in sorted(devices, key=lambda x: x.name or ""):
        name = d.name or "(no name)"
        print(f"  {name:<30} | {d.address}")

        # Look for neo/neosapien devices
        if d.name and any(x in d.name.lower() for x in ["neo", "sapien"]):
            neo_devices.append(d)

    return neo_devices

async def explore_device(address: str):
    """Connect to a device and list its services/characteristics."""
    print(f"\nConnecting to {address}...")
    try:
        async with BleakClient(address, timeout=20.0) as client:
            print(f"Connected: {client.is_connected}")
            print("\nServices and Characteristics:")

            for service in client.services:
                print(f"\n  Service: {service.uuid}")
                print(f"    Description: {service.description}")

                for char in service.characteristics:
                    props = ", ".join(char.properties)
                    print(f"      Char: {char.uuid}")
                    print(f"        Properties: {props}")
                    print(f"        Handle: {char.handle}")
    except Exception as e:
        print(f"Error connecting: {e}")

async def main():
    neo_devices = await scan_all_devices()

    if neo_devices:
        print(f"\n{'='*60}")
        print(f"Found {len(neo_devices)} neo/neosapien device(s)!")
        for d in neo_devices:
            print(f"  - {d.name}: {d.address}")

        # Explore the first neo device
        print(f"\n{'='*60}")
        await explore_device(neo_devices[0].address)
    else:
        print("\nNo neo/neosapien devices found.")
        print("Make sure the device is powered on and in pairing mode.")

if __name__ == "__main__":
    asyncio.run(main())
