import asyncio
from pymodbus.client.tcp import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException
from pymodbus.pdu import ExceptionResponse

PLC_HOST = "localhost"  # or your PLC IP
PLC_PORT = 502
MAX_BARCODE_LENGTH = 16  # 16 characters = 8 registers

# Adjust these based on your PLC's memory map
WRITE_START_ADDRESS = 4000  # Typical holding registers start at 4xxxx
READ_START_ADDRESS = 4000   # Can be same or different
REGISTER_COUNT = 8           # Number of registers needed for max barcode

def string_to_registers(text):
    """Convert string to register values with diagnostic output"""
    text = text.ljust(MAX_BARCODE_LENGTH)[:MAX_BARCODE_LENGTH]
    print(f"Original string: {text} (len={len(text)})")
    registers = []
    for i in range(0, len(text), 2):
        chunk = text[i:i+2]
        # Big Endian encoding
        if len(chunk) == 2:
            be_val = (ord(chunk[0]) << 8) | ord(chunk[1])
        else:
            be_val = ord(chunk[0]) << 8  # Only one character
        print(f"Chars '{chunk}' → BE:0x{be_val:04X}")
        registers.append(be_val)
    return registers

def registers_to_string(registers):
    """Convert registers to string with thorough decoding"""
    print(f"Raw registers: {registers}")
    result = ''
    
    for reg in registers:
        # Big Endian decoding
        be_char1 = chr((reg >> 8) & 0xFF)
        be_char2 = chr(reg & 0xFF)
        result += be_char1 + be_char2
    
    print(f"Decoded string: '{result}' (len={len(result)})")
    return result.strip()



async def check_registers_accessible(client, address, count, is_write=False):
    """Verify if registers are accessible before operation"""
    try:
        if is_write:
            # Test write one register
            test_response = await client.write_register(address, 0)
        else:
            # Test read one register
            test_response = await client.read_holding_registers(address, 1)
            
        if isinstance(test_response, ExceptionResponse):
            print(f"❌ Register address {address} not accessible (Exception Code: {test_response.exception_code})")
            return False
        return True
    except Exception as e:
        print(f"❌ Error checking registers: {str(e)}")
        return False

async def write_barcode(client, barcode):
    """Write barcode to PLC"""
    if len(barcode) > MAX_BARCODE_LENGTH:
        print(f"❌ Barcode exceeds maximum length of {MAX_BARCODE_LENGTH} characters")
        return False

    if not await check_registers_accessible(client, WRITE_START_ADDRESS, REGISTER_COUNT, is_write=True):
        return False

    values = string_to_registers(barcode)
    try:
        print(f"✏️ Attempting to write barcode to registers {WRITE_START_ADDRESS}-{WRITE_START_ADDRESS+REGISTER_COUNT-1}")
        response = await client.write_registers(WRITE_START_ADDRESS, values)
        
        if isinstance(response, ExceptionResponse):
            print(f"⚠️ Write failed (Exception Code: {response.exception_code})")
            return False
            
        print(f"✅ Successfully wrote '{barcode}'")
        return True
    except Exception as e:
        print(f"❌ Write error: {str(e)}")
        return False

async def read_barcode(client, barcode):
    """Read barcode from PLC"""
    if not await check_registers_accessible(client, READ_START_ADDRESS, REGISTER_COUNT,1):
        return None

    try:
        # First write the barcode
        values = string_to_registers(barcode)
        print(f"✏️ Writing '{barcode}' to registers {READ_START_ADDRESS}-{READ_START_ADDRESS+7}")
        await client.write_registers(
            address=READ_START_ADDRESS,
            values=values,
            slave=1
        )
        
        # Then read back
        await asyncio.sleep(0.1)  # Small delay for PLC processing
        response = await client.read_holding_registers(
            address=READ_START_ADDRESS,
            count=REGISTER_COUNT,
            slave=1
        )
        
        if isinstance(response, ExceptionResponse):
            print(f"⚠️ Read failed (Exception Code: {response.exception_code})")
            return False
            
        read_barcode = registers_to_string(response.registers)
        print(f"📖 Read back: '{read_barcode}'")
        
        if read_barcode.startswith(barcode):
            print("✅ Verification successful!")
            return True
        else:
            print(f"❌ Mismatch! Written: '{barcode}' | Read: '{read_barcode}'")
            return False
    except Exception as e:
        print(f"❌ Read error: {str(e)}")
        return False
async def main():
    print(f"🔌 Connecting to {PLC_HOST}:{PLC_PORT}...")
    client = AsyncModbusTcpClient(host=PLC_HOST, port=PLC_PORT, timeout=5)

    try:
        await client.connect()
        if not client.connected:
            print("❌ Connection failed")
            return

        print(f"✅ Connected to PLC at {PLC_HOST}:{PLC_PORT}")

        # Example usage
        await write_barcode(client, "REFTD23423234")
        await read_barcode(client,"NEW123")
        
        # Test with shorter barcode
        await write_barcode(client, "NEW123")
        await read_barcode(client,"NEW123")

    except Exception as e:
        print(f"❌ Main error: {str(e)}")
    finally:
        client.close()
        print("🔌 Connection closed")

if __name__ == "__main__":
    asyncio.run(main())