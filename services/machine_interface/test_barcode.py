import asyncio
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException
import logging
import time

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Constants
BARCODE_1_BLOCK = (2000, 16)  # Start address, length
BARCODE_2_BLOCK = (2020, 16)
PORT = 502
HOST = "localhost"

def encode_string(value):
    """Encode string to Modbus registers"""
    value = value.ljust(32, '\x00')  # Pad to 16 registers (32 bytes)
    registers = []
    for i in range(0, len(value), 2):
        if i+1 < len(value):
            registers.append((ord(value[i]) << 8) | ord(value[i+1]))
        else:
            registers.append(ord(value[i]) << 8)
    return registers

def decode_string(registers):
    """Decode Modbus registers to ASCII string"""
    if not registers:
        return ""
    return ''.join(chr((reg >> 8) & 0xFF) + chr(reg & 0xFF) for reg in registers).replace('\x00', '')

async def write_and_verify_barcode(client, address, test_value):
    """Write a test barcode and verify the read value matches"""
    try:
        # Encode and write the test value
        encoded = encode_string(test_value)
        write_response = await client.write_registers(address=address, values=encoded)
        
        if write_response.isError():
            logger.error(f"Write failed for address {address}")
            return False

        # Read back the value
        read_response = await client.read_holding_registers(address=address, count=len(encoded))
        
        if read_response.isError():
            logger.error(f"Read failed for address {address}")
            return False

        # Compare values
        read_value = decode_string(read_response.registers)
        match = read_value == test_value
        logger.info(f"Write/Verify {'SUCCESS' if match else 'FAILED'}: "
                   f"Wrote '{test_value}', Read '{read_value}'")
        return match

    except ModbusException as e:
        logger.error(f"Modbus error during write/verify: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return False

async def run_barcode_test(client):
    """Run complete barcode write/verify test"""
    test_cases = [
        (BARCODE_1_BLOCK[0], "TEST1234567890"),
        (BARCODE_2_BLOCK[0], "SCANME987654321"),
        (BARCODE_1_BLOCK[0], ""),  # Test empty string
        (BARCODE_1_BLOCK[0], "A"),  # Test single character
        (BARCODE_2_BLOCK[0], "LONGSTRINGTEST1234567890ABCDEFGHIJ")  # Test long string
    ]

    results = []
    for address, test_value in test_cases:
        success = await write_and_verify_barcode(client, address, test_value)
        results.append((test_value, success))
        await asyncio.sleep(1)  # Delay between tests

    # Print summary
    print("\n=== Test Summary ===")
    for test_value, success in results:
        print(f"{'✓' if success else '✗'} Test value: '{test_value}'")
    
    return all(success for _, success in results)

async def run_test():
    """Main test runner"""
    client = AsyncModbusTcpClient(
        host=HOST,
        port=PORT,
        timeout=5,
        retries=3
    )

    try:
        print(f"Connecting to Modbus server at {HOST}:{PORT}...")
        await client.connect()
        
        if not client.connected:
            print("❌ Connection failed")
            return False

        print("✅ Connected successfully\n")
        return await run_barcode_test(client)

    except Exception as e:
        logger.error(f"Test failed: {e}")
        return False
    finally:
        print("\nDisconnecting...")
        client.close()
        await asyncio.sleep(1)

if __name__ == "__main__":
    print("=== Modbus Barcode Read/Write Verification Test ===")
    print(f"Target Server: {HOST}:{PORT}")
    print("This script will write test barcodes and verify the read values match")
    print("="*50 + "\n")
    
    try:
        test_result = asyncio.run(run_test())
        print("\n=== Final Result ===")
        print("✅ All tests passed" if test_result else "❌ Some tests failed")
    except KeyboardInterrupt:
        print("\nTest stopped by user")