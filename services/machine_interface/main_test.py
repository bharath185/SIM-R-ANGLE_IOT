import asyncio
from pymodbus.client.tcp import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException
from pymodbus.pdu import ExceptionResponse

PLC_HOST = "localhost"  # or your PLC IP
PLC_PORT = 502

async def main():
    client = AsyncModbusTcpClient(host=PLC_HOST, port=PLC_PORT)

    # Establish connection
    await client.connect()
    if not client.connected:
        print("❌ Could not connect to PLC.")
        return

    print(f"✅ Connected to PLC at {PLC_HOST}:{PLC_PORT}")

    try:
        # Example register address and values to write
        register_address = 20000
        values_to_write = [1234, 5678]  # Two 16-bit values
        
        # Write holding registers
        print(f"✏️ Writing values {values_to_write} to register {register_address}")
        write_response = await client.write_registers(
            address=register_address,
            values=values_to_write
        )
        
        if isinstance(write_response, ExceptionResponse):
            print("⚠️ Write error:", write_response)
        else:
            print("✅ Write successful")
            
            # Small delay to allow the PLC to process the write
            await asyncio.sleep(0.1)
            
            # Read back the same registers to verify
            print(f"📖 Reading back registers starting at {register_address}")
            read_response = await client.read_holding_registers(
                address=register_address, 
                count=1
            )
            
            if isinstance(read_response, ExceptionResponse):
                print("⚠️ Read error:", read_response)
            else:
                print(f"📦 Read registers: {read_response.registers}")
                if read_response.registers == values_to_write:
                    print("✅ Write verification successful!")
                else:
                    print("❌ Write verification failed - values don't match")

    except ModbusException as e:
        print("❌ Modbus Exception:", e)
    except Exception as e:
        print("❌ General Exception:", e)
    finally:
        client.close()
        print("🔌 Connection closed.")

if __name__ == "__main__":
    asyncio.run(main())