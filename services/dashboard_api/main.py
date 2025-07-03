import os
import json
import time
import threading
import select
import asyncio
import requests
import psycopg2
import psycopg2.extensions
import uuid
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, status, Depends, WebSocket, WebSocketDisconnect, Query
from pydantic import BaseModel
from typing import Optional,Union
from influxdb_client import InfluxDBClient, WritePrecision, WriteOptions
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer # Import AIOKafkaProducer
from aiokafka.errors import KafkaConnectionError, NoBrokersAvailable as AIOKafkaNoBrokersAvailable
from passlib.context import CryptContext
from fastapi.middleware.cors import CORSMiddleware  # Add this import at the top
# ─── Configuration ──────────────────────────────────────────────
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092") # Using KAFKA_BOOTSTRAP_SERVERS env var name
PLC_WRITE_COMMANDS_TOPIC = os.getenv("PLC_WRITE_COMMANDS_TOPIC", "plc_write_commands")
PLC_WRITE_RESPONSES_TOPIC = os.getenv("PLC_WRITE_RESPONSES_TOPIC", "plc_write_responses") # For optional write acknowledgements from PLC Gateway
MACHINE_STATUS  = os.getenv("MACHINE_STATUS", "machine_status")
STARTUP_STATUS = os.getenv("STARTUP_STATUS", "startup_status")
MANUAL_STATUS = os.getenv("MANUAL_STATUS", "manual_status")
AUTO_STATUS = os.getenv("AUTO_STATUS", "auto_status")
ROBO_STATUS = os.getenv("ROBO_STATUS", "robo_status")
IO_STATUS = os.getenv("IO_STATUS", "io_status")
OEE_STATUS = os.getenv("OEE_STATUS", "oee_status") # New topic for OEE data

# ─── kafka init ──────────────────────────────────────────────
async def kafka_to_ws(name, topic):
    for attempt in range(10):  # Try for up to ~50 seconds
        try:
            consumer = AIOKafkaConsumer(
                topic,
                bootstrap_servers=KAFKA_BOOTSTRAP,
                auto_offset_reset="latest"
            )
            await consumer.start()
            print(f"[{name}] Kafka consumer for topic '{topic}' started.")
            break
        except AIOKafkaNoBrokersAvailable:
            print(f"[{name}] Kafka not ready for consumer (topic: {topic}). Retrying ({attempt + 1}/10)...")
            await asyncio.sleep(5)
        except Exception as e:
            print(f"[{name}] Error starting Kafka consumer for topic '{topic}': {e}. Retrying ({attempt + 1}/10)...")
            await asyncio.sleep(5)
    else:
        raise RuntimeError(f"Kafka not available for consumer topic {topic} after multiple attempts.")

    try:
        async for msg in consumer:
            # print(f"[{name}] Received Kafka message for broadcast: {msg.value.decode()}") # Debug
            await mgr.broadcast(name, msg.value.decode())
    except Exception as e:
        print(f"[{name}] Error during Kafka consumption for topic '{topic}': {e}")
    finally:
        if consumer:
            await consumer.stop()
            print(f"[{name}] Kafka consumer for topic '{topic}' stopped.")

# ─── Kafka Producer (for sending write commands) ──────────────────────────────────────────────
kafka_producer: Optional[AIOKafkaProducer] = None

async def init_kafka_producer():
    global kafka_producer
    for attempt in range(10):
        try:
            producer = AIOKafkaProducer(
                bootstrap_servers=KAFKA_BOOTSTRAP,
                value_serializer=lambda v: json.dumps(v).encode("utf-8"),
                max_request_size=2048576, # Default is 1MB, increase if large messages expected
                request_timeout_ms=30000 # Default is 30s
            )
            await producer.start()
            kafka_producer = producer
            print("[Kafka Producer] AIOKafkaProducer started successfully.")
            return
        except AIOKafkaNoBrokersAvailable:
            print(f"[Kafka Producer] Kafka not ready. Retrying ({attempt + 1}/10)...")
            await asyncio.sleep(5)
        except Exception as e:
            print(f"[Kafka Producer] Error starting AIOKafkaProducer: {e}. Retrying ({attempt + 1}/10)...")
            await asyncio.sleep(5)
    raise RuntimeError("AIOKafkaProducer not available after multiple attempts.")


# ─── FastAPI App Initialization ──────────────────────────────────────────────
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)
# ─── PostgreSQL Setup ──────────────────────────────────────────────
DB_URL = os.getenv("DB_URL")
for _ in range(10):
    try:
        conn = psycopg2.connect(DB_URL)
        break
    except psycopg2.OperationalError:
        print("PostgreSQL not ready, retrying...")
        time.sleep(5)
else:
    raise RuntimeError("PostgreSQL not available")
conn.autocommit = True
cur = conn.cursor()

# ─── UUID Generator ──────────────────────────────────────────────
def generate_request_id() -> str:
    return str(uuid.uuid4())

# ─── Hot Reloadable Config ─────────────────────────────────────────
_config_cache: dict[str, str] = {}

def load_config():
    cur.execute("SELECT key, value FROM config")
    return {k: v for k, v in cur.fetchall()}

def listen_for_config_updates():
    listen_conn = psycopg2.connect(DB_URL)
    listen_conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    lc = listen_conn.cursor()
    lc.execute("LISTEN config_update;")
    while True:
        if select.select([listen_conn], [], [], 5) == ([], [], []):
            continue
        listen_conn.poll()
        while listen_conn.notifies:
            note = listen_conn.notifies.pop(0)
            key = note.payload
            cur.execute("SELECT value FROM config WHERE key = %s", (key,))
            row = cur.fetchone()
            if row:
                _config_cache[key] = row[0]

_config_cache = load_config()
threading.Thread(target=listen_for_config_updates, daemon=True).start()

def get_cfg(key: str, default=None):
    return _config_cache.get(key, default)

# ─── InfluxDB Setup ────────────────────────────────────────────────
influx_client = InfluxDBClient(
    url=get_cfg("INFLUXDB_URL", "http://influxdb:8086"),
    token=get_cfg("INFLUXDB_TOKEN", "edgetoken"),
    org=get_cfg("INFLUXDB_ORG", "EdgeOrg")
)
write_api = influx_client.write_api(write_options=WriteOptions(batch_size=1, flush_interval=1000))

query_api = influx_client.query_api()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
# ─── Models & Auth Guard ───────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str
    password: str
    role: str
    
class ScanRequest(BaseModel):
    serial: str
    result: str  # "pass" | "fail" | "scrap"

class PlcWriteCommand(BaseModel):
    section: str
    tag_name: str
    value: int | float | str
    request_id: Optional[str] = None # For tracking responses


_current_operator: Optional[str] = None
def require_login():
    if not _current_operator:
        raise HTTPException(401, "Operator not logged in")

# ─── Config API ─────────────────────────────────────────────────────
@app.post("/api/config")
def update_config(item: dict, _=Depends(require_login)):
    cur.execute("UPDATE config SET value = %s WHERE key = %s", (item["value"], item["key"]))
    cur.execute("NOTIFY config_update, %s", (item["key"],))
    return {"message": f"Config {item['key']} updated"}

@app.get("/api/config")
def get_config():
    return _config_cache


# ─── Login & Logout ─────────────────────────────────────────────────────────
@app.post("/api/login")
def login(req: LoginRequest):
    try:
        # 1. Verify user exists and get credentials
        cur.execute("""
            SELECT u.id, u.username, u.full_name, u.hashed_password, 
                   r.id as role_id, r.name as role_name
            FROM users u
            JOIN roles r ON u.role_id = r.id
            WHERE u.username = %s AND u.is_active = TRUE
        """, (req.username,))
        
        user = cur.fetchone()
        if not user:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "Invalid username or password"
            )

        # 2. Verify password
        if not pwd_context.verify(req.password, user[3]):  # hashed_password is at index 3
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "Invalid username or password"
            )
        if req.role:
            try:
                requested_role_id = int(req.role)
                if user[4] != requested_role_id:  # role_id is at index 4
                    raise HTTPException(
                        status.HTTP_403_FORBIDDEN,
                        detail="User doesn't have the requested role"
                    )
            except ValueError:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    detail="Invalid role format"
                )
        # 3. Get Current Shift Information
        current_time = datetime.now().time()
        # Execute the query
        cur.execute("""
            SELECT id, name, start_time::time, end_time::time
            FROM shift_master
            WHERE 
                (
                    -- Case 1: Normal shift (start_time < end_time)
                    (start_time::time <= end_time::time AND %s BETWEEN start_time::time AND end_time::time)
                    OR
                    -- Case 2: Overnight shift (start_time > end_time)
                    (start_time::time > end_time::time AND (
                        %s >= start_time::time OR %s <= end_time::time
                    ))
                )
            ORDER BY start_time
            LIMIT 1
        """, (current_time, current_time, current_time))
        # Fetch the result
        shift = cur.fetchone()
        # Check the result
        if shift:
            print(f"Current Shift: {shift}")
        else:
            print("No active shift found.")

        shift_info = {
            "id": shift[0],
            "name": shift[1],
            "start_time": shift[2].isoformat(),
            "end_time": shift[3].isoformat()
        } if shift else None

        # 4. Get User Permissions
        cur.execute("""
            SELECT m.name, p.permission
            FROM permission_policies p
            JOIN modules m ON p.module_id = m.id
            WHERE p.role_id = %s
        """, (user[4],))  # role_id is at index 4
        permissions = {row[0]: row[1] for row in cur.fetchall()}

        # 5. Record Login Session
        cur.execute("""
            INSERT INTO operator_sessions (username, login_ts)
            VALUES (%s, NOW())
            RETURNING id
        """, (user[1],))

        # 6. Return Comprehensive Response
        return {
            "status": "success",
            "user": {
                "id": user[0],
                "username": user[1],
                "full_name": user[2],
                "role": {
                    "id": user[4],
                    "name": user[5]
                }
            },
            "shift": shift_info,
            "permissions": permissions,
            "session_id": cur.fetchone()[0]
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(e))

@app.post("/api/logout")
def logout():
    global _current_operator
    _current_operator = None
    return {"message": "Logged out"}

# ─── Health ─────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    return {"status": "ok"}

# ─── Scan Orchestration ─────────────────────────────────────────────────────
@app.post("/api/scan")
def scan_part(req: ScanRequest, _=Depends(require_login)):
    serial = req.serial
    result = {}

    try:
        # 1) MES PC-based Process Control
        pc = requests.post(
            get_cfg("MES_PROCESS_CONTROL_URL"),
            json={
                "UniqueId":      serial,
                "MachineId":     get_cfg("MACHINE_ID"),
                "OperatorId":    _current_operator,
                "Tools":         [],
                "RawMaterials":  [],
                "CbsStreamName": get_cfg("CBS_STREAM_NAME")
            }
        )
        if pc.status_code != 200 or not pc.json().get("IsSuccessful"):
            raise HTTPException(400, f"MES PC failed: {pc.text}")
        result["mes_pc"] = pc.json()
        cur.execute(
            "INSERT INTO mes_trace_history(serial, step, response_json, ts) VALUES(%s,%s,%s::jsonb,NOW())",
            (serial, "mes_pc", json.dumps(result["mes_pc"]))
        )

        # 2) Trace Process Control
        tp = requests.get(
            f"http://{get_cfg('TRACE_PROXY_HOST')}/v2/process_control",
            params={"serial": serial, "serial_type": "band"}
        ); tp.raise_for_status()
        tpj = tp.json()
        if not tpj.get("pass"):
            raise HTTPException(400, f"Trace PC failed: {tpj}")
        result["trace_pc"] = tpj
        cur.execute(
            "INSERT INTO mes_trace_history(serial, step, response_json, ts) VALUES(%s,%s,%s::jsonb,NOW())",
            (serial, "trace_pc", json.dumps(tpj))
        )

        # 3) Trace Interlock
        inter = requests.post(
            f"http://{get_cfg('TRACE_PROXY_HOST')}/interlock",
            params={"serial": serial, "serial_type": "band"}
        ); inter.raise_for_status()
        inj = inter.json()
        if not inj.get("pass"):
            raise HTTPException(400, f"Interlock failed: {inj}")
        result["interlock"] = inj
        cur.execute(
            "INSERT INTO mes_trace_history(serial, step, response_json, ts) VALUES(%s,%s,%s::jsonb,NOW())",
            (serial, "interlock", json.dumps(inj))
        )

        # 4) MES Upload
        payload = {
          "UniqueIds":     [serial],
          "MachineIds":    [get_cfg("MACHINE_ID")],
          "OperatorIds":   [_current_operator],
          "Tools":         [],
          "RawMaterials":  [],
          "DateTimeStamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
          "CbsStreamName": get_cfg("CBS_STREAM_NAME")
        }
        if req.result == "pass":
            payload.update({"IsPass": True, "IsFail": False, "IsScrap": False, "Defects": []})
        elif req.result == "fail":
            payload.update({
              "IsPass": False, "IsFail": True, "IsScrap": False,
              "FailureDefects": [{
                 "FailureReasonCodes": get_cfg("FAILURE_REASON_CODES").split(","),
                 "NcmReasonCodes":     get_cfg("NCM_REASON_CODES").split(",")
              }]
            })
        elif req.result == "scrap":
            payload.update({
              "IsPass": False, "IsFail": False, "IsScrap": True,
              "Defects": [{"DefectCode":"Trace Scrap","Quantity":1}]
            })
        else:
            raise HTTPException(400, "Invalid result type")

        mes = requests.post(get_cfg("MES_UPLOAD_URL"), json=payload)
        if mes.status_code != 200 or not mes.json().get("IsSuccessful"):
            raise HTTPException(400, f"MES upload failed: {mes.text}")
        result["mes_upload"] = mes.json()
        cur.execute(
            "INSERT INTO mes_trace_history(serial, step, response_json, ts) VALUES(%s,%s,%s::jsonb,NOW())",
            (serial, "mes_upload", json.dumps(result["mes_upload"]))
        )

        # 5) Trace Data Log
        td = requests.post(
            f"http://{get_cfg('TRACE_PROXY_HOST')}/v2/logs",
            json={"serials":{"part_id":serial}}
        )
        if td.status_code != 200:
            raise HTTPException(400, f"Trace log failed: {td.text}")
        result["trace_log"] = td.json()
        cur.execute(
            "INSERT INTO mes_trace_history(serial, step, response_json, ts) VALUES(%s,%s,%s::jsonb,NOW())",
            (serial, "trace_log", json.dumps(result["trace_log"]))
        )

        # Final audit
        cur.execute(
            "INSERT INTO scan_audit(serial, operator, result, ts) VALUES(%s,%s,%s,NOW())",
            (serial, _current_operator, req.result)
        )

        return result

    except HTTPException as he:
        cur.execute(
            "INSERT INTO error_logs(context,error_msg,details,ts) VALUES(%s,%s,%s::jsonb,NOW())",
            ("scan", str(he.detail), json.dumps({"serial":serial, "error":he.detail}))
        )
        raise

    except Exception as e:
        cur.execute(
            "INSERT INTO error_logs(context,error_msg,details,ts) VALUES(%s,%s,%s::jsonb,NOW())",
            ("scan", str(e), json.dumps({"serial": serial}))
        )
        raise HTTPException(500, "Internal server error")

# ─── Sensors Endpoints ─────────────────────────────────────────────────────
@app.get("/api/sensors/latest")
def sensors_latest(source: str):
    flux = f'''
      from(bucket:"{get_cfg("INFLUXDB_BUCKET")}")
        |> range(start:-1m)
        |> filter(fn:(r)=>r._measurement=="sensor_data" and r.source=="{source}")
        |> last()
    '''
    tables = query_api.query(flux)
    if not tables or not tables[0].records:
        raise HTTPException(404, "No data")
    rec = tables[0].records[-1]
    return {"time":rec.get_time().isoformat(), "value":rec.get_value(), "source":rec.values.get("source")}

@app.get("/api/sensors/history")
def sensors_history(source: str, hours: int = 1):
    flux = f'''
      from(bucket:"{get_cfg("INFLUXDB_BUCKET")}")
        |> range(start:-{hours}h)
        |> filter(fn:(r)=>r._measurement=="sensor_data" and r.source=="{source}")
        |> keep(columns:["_time","_value","source"])
    '''
    tables = query_api.query(flux)
    data = [
      {"time":rec.get_time().isoformat(), "value":rec.get_value(), "source":rec.values.get("source")}
      for table in tables for rec in table.records
    ]
    if not data:
        raise HTTPException(404, "No history")
    return data

# ─── OEE Endpoints ─────────────────────────────────────────────────────────
@app.get("/api/oee/current")
def oee_current():
    flux = f'''
      from(bucket:"{get_cfg("INFLUXDB_BUCKET")}")
        |> range(start:-5m)
        |> filter(fn:(r)=>r._measurement=="oee")
        |> last()
    '''
    tables = query_api.query(flux)
    if not tables or not tables[0].records:
        raise HTTPException(404, "No OEE data")
    rec = tables[0].records[-1]
    return {"time":rec.get_time().isoformat(),
            **{k:rec.values[k] for k in rec.values if not k.startswith("_")}}

@app.get("/api/oee/history")
def oee_history(
    # if start & end are provided, we'll use them; otherwise fall back to `hours`
    hours: int = Query(1, ge=0, description="Look back this many hours if start/end are omitted"),
    start: Optional[datetime] = Query(
        None,
        description="ISO8601 start time (e.g. 2025-06-20T00:00:00Z)",
    ),
    end: Optional[datetime] = Query(
        None,
        description="ISO8601 end time (e.g. 2025-06-21T00:00:00Z)",
    ),
):
    # 1) determine our window
    if start and end:
        t0, t1 = start, end
    else:
        t1 = datetime.utcnow()
        t0 = t1 - timedelta(hours=hours)

    # Flux wants un-quoted literals like 2025-06-20T00:00:00Z
    start_ts = t0.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_ts   = t1.strftime("%Y-%m-%dT%H:%M:%SZ")

    flux = f'''
      from(bucket: "{get_cfg("INFLUXDB_BUCKET")}")
        |> range(start: {start_ts}, stop: {end_ts})
        |> filter(fn: (r) => r._measurement == "oee")
        |> pivot(
             rowKey:   ["_time"],
             columnKey: ["_field"],
             valueColumn: "_value"
           )
    '''
    tables = query_api.query(flux)

    data = []
    for table in tables:
        for rec in table.records:
            entry = {"time": rec.get_time().isoformat()}
            # grab all fields except Flux's internal ones
            entry.update({k: rec.values[k] for k in rec.values if not k.startswith("_")})
            data.append(entry)

    if not data:
        raise HTTPException(404, f"No OEE data in window {start_ts} → {end_ts}")

    return data


# ─── WebSocket Setup ───────────────────────────────────────────────
KAFKA_BOOTSTRAP = get_cfg("KAFKA_BROKER", "kafka:9092")
WS_TOPICS = {
    # "sensors": "raw_server_data",
    # "triggers": "trigger_events",
    # "commands": "machine_commands",
    # "mes-logs": "mes_uploads",
    # "trace-logs": "trace_logs",
    #"scan-results": "scan_results",
    "machine-status":MACHINE_STATUS,
    #"station-status": "station_flags",
    "startup-status": STARTUP_STATUS,
    "manual-status": MANUAL_STATUS,
    "auto-status": AUTO_STATUS,
    "robo-status": ROBO_STATUS,
    "io-status": IO_STATUS,
    "oee-status": OEE_STATUS,
    "plc-write-responses": PLC_WRITE_RESPONSES_TOPIC
}
class ConnectionManager:
    def __init__(self):
        self.active_connections = {k: set() for k in WS_TOPICS.keys()} # Active WebSocket connections per stream
        # A mapping from request_id to specific WebSocket connection
        self.pending_write_responses = {} # request_id -> WebSocket

    async def connect(self, stream_name, ws: WebSocket):
        await ws.accept()
        if stream_name in self.active_connections:
            self.active_connections[stream_name].add(ws)
            print(f"[WS Manager] Client connected to '{stream_name}' from {ws.client}")
        else:
            print(f"[WS Manager] Warning: Client connected to unknown stream '{stream_name}'")


    def disconnect(self, stream_name, ws: WebSocket):
        if stream_name in self.active_connections:
            self.active_connections[stream_name].discard(ws)
            print(f"[WS Manager] Client disconnected from '{stream_name}' from {ws.client}")

    async def broadcast(self, stream_name, msg):
        living_connections = set()
        for ws in self.active_connections.get(stream_name, set()):
            try:
                await ws.send_text(msg)
                living_connections.add(ws)
            except Exception as e:
                print(f"[WS Manager] Error broadcasting to {ws.client} on '{stream_name}': {e}. Removing.")
        self.active_connections[stream_name] = living_connections
        # print(f"[WS Manager] Broadcasted to {len(living_connections)} clients on '{stream_name}'.") # Debug
    
    async def send_write_response_to_client(self, request_id: str, message: dict):
        ws = self.pending_write_responses.pop(request_id, None)
        if ws:
            try:
                await ws.send_json({"type": "plc_write_response", "data": message})
                print(f"[WS Manager] Sent specific write response for {request_id} to {ws.client}")
            except Exception as e:
                print(f"[WS Manager] Error sending specific write response to {ws.client} for {request_id}: {e}")
        else:
            print(f"[WS Manager] No pending client for request_id {request_id} for write response.")


mgr = ConnectionManager()


# ─── Startup and Shutdown Events ─────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup_event():
    await init_kafka_producer() # Initialize AIOKafkaProducer
    for name, topic in WS_TOPICS.items():
        asyncio.create_task(kafka_to_ws(name, topic))
    
    # NEW: Start consumer for PLC write responses from PLC Gateway
    asyncio.create_task(listen_for_plc_write_responses())

@app.on_event("shutdown")
async def shutdown_event():
    if kafka_producer:
        await kafka_producer.stop()
        print("[Kafka Producer] AIOKafkaProducer stopped.")
    # Close PostgreSQL connection
    if conn:
        conn.close()
        print("PostgreSQL connection closed.")
    # Close InfluxDB client
    if influx_client:
        influx_client.close()
        print("InfluxDB client closed.")
    # Note: AIOKafkaConsumer tasks should handle their own stopping in finally blocks

# ─── New Handler for Incoming PLC Write Commands via WebSocket ──────────────────────────
async def handle_plc_write_command_ws(websocket: WebSocket):
    """
    Handles incoming PLC write commands from the Dashboard UI via WebSocket.
    Publishes valid commands to the PLC_WRITE_COMMANDS_TOPIC Kafka topic.
    """
    async for message_str in websocket.iter_text(): # Use iter_text for cleaner loop
        try:
            # Parse the incoming WebSocket message as a JSON command
            command_data = json.loads(message_str)
            command = PlcWriteCommand(**command_data) # Validate with Pydantic model

            # Generate a request_id if not provided by the client
            if not command.request_id:
                command.request_id = str(uuid.uuid4())
            
            # Store the WebSocket client connection to send response back later
            mgr.pending_write_responses[command.request_id] = websocket
            
            # Convert Pydantic model back to dict for Kafka
            kafka_message = command.model_dump_json().encode('utf-8')
            
            if kafka_producer is None:
                raise RuntimeError("Kafka producer not initialized.")

            # Publish the command to Kafka
            await kafka_producer.send_and_wait(PLC_WRITE_COMMANDS_TOPIC, value=kafka_message)
            
            print(f"[WS Handler] Published PLC write command for request_id '{command.request_id}' to Kafka.")
            
            # Send an immediate acknowledgement back to the UI
            await websocket.send_json({"type": "ack", "status": "pending", "request_id": command.request_id, "message": "Command sent to PLC queue."})

        except json.JSONDecodeError:
            await websocket.send_json({"type": "error", "message": "Invalid JSON format for PLC write command."})
            print(f"[WS Handler] Invalid JSON received from {websocket.client}: {message_str}")
        except ValueError as ve: # Pydantic validation error
            await websocket.send_json({"type": "error", "message": f"Invalid command data: {ve}"})
            print(f"[WS Handler] Pydantic validation error from {websocket.client}: {ve}")
        except RuntimeError as re:
            await websocket.send_json({"type": "error", "message": str(re)})
            print(f"[WS Handler] Runtime error: {re}")
        except Exception as e:
            await websocket.send_json({"type": "error", "message": f"An internal server error occurred: {e}"})
            print(f"[WS Handler] Unexpected error in handle_plc_write_command_ws: {e}")

# ─── New Consumer for PLC Write Responses from PLC Gateway ──────────────────────────
async def listen_for_plc_write_responses():
    consumer = None
    for attempt in range(10):
        try:
            consumer = AIOKafkaConsumer(
                PLC_WRITE_RESPONSES_TOPIC,
                bootstrap_servers=KAFKA_BOOTSTRAP,
                auto_offset_reset="latest",
                group_id="dashboard-write-response-listener" # Unique consumer group for this service
            )
            await consumer.start()
            print(f"[Response Listener] Kafka consumer for topic '{PLC_WRITE_RESPONSES_TOPIC}' started.")
            break
        except AIOKafkaNoBrokersAvailable:
            print(f"[Response Listener] Kafka not ready for response consumer. Retrying ({attempt + 1}/10)...")
            await asyncio.sleep(5)
        except Exception as e:
            print(f"[Response Listener] Error starting Kafka response consumer: {e}. Retrying ({attempt + 1}/10)...")
            await asyncio.sleep(5)
    else:
        raise RuntimeError(f"Kafka not available for response topic {PLC_WRITE_RESPONSES_TOPIC} after multiple attempts.")

    try:
        async for msg in consumer:
            response_data = json.loads(msg.value.decode('utf-8'))
            print(f"[Response Listener] Received PLC write response: {response_data}")
            request_id = response_data.get("request_id")
            if request_id:
                await mgr.send_write_response_to_client(request_id, response_data)
            else:
                print(f"[Response Listener] Response missing request_id: {response_data}")
    except Exception as e:
        print(f"[Response Listener] Error during Kafka response consumption: {e}")
    finally:
        if consumer:
            await consumer.stop()
            print(f"[Response Listener] Kafka response consumer stopped.")

@app.websocket("/ws/report_data")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    
    # Initialize InfluxDB client with error handling
    try:

        
        while True:
            try:
                # Simplified query that definitely works
                query = '''
                from(bucket: "vision_data")
                  |> range(start: -1h)
                  |> filter(fn: (r) => r._measurement == "barcode_measurements")
                  |> filter(fn: (r) => r._field == "barcode_value" or 
                                       r._field == "overall_status" or
                                       r._field == "radius1" or
                                       r._field == "radius2" or
                                       r._field == "fai1" or
                                       r._field == "fai2" or
                                       r._field == "fai3" or
                                       r._field == "fai4")
                  |> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value")
                  |> group(columns: ["barcode_value"])
                  |> sort(columns: ["_time"], desc: true)
                  |> limit(n: 100)
                '''
                
                # Execute query with timeout
                tables = await asyncio.to_thread(
                    lambda: query_api.query(query)
                )
                
                # Process results safely
                results = []
                for table in tables:
                    for record in table.records:
                        try:
                            results.append({
                                "time": str(record.get_time()),
                                "barcode": record.values.get("barcode_value"),
                                "status": record.values.get("overall_status", 0),
                                "radius1": float(record.values.get("radius1", 0)),
                                "radius2": float(record.values.get("radius2", 0)),
                                "fai1": int(record.values.get("fai1", 0)),
                                "fai2": int(record.values.get("fai2", 0)),
                                "fai3": int(record.values.get("fai3", 0)),
                                "fai4": int(record.values.get("fai4", 0))
                            })
                        except Exception as e:
                            print(f"Error processing record: {e}")
                            continue
                
                await websocket.send_text(json.dumps({
                    "type": "data_update",
                    "data": results
                }))
                
            except Exception as query_error:
                print(f"Query error: {str(query_error)}")
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": "Failed to fetch data"
                }))
                await asyncio.sleep(5)  # Wait before retrying
            
            await asyncio.sleep(1)  # Throttle updates
            
    except WebSocketDisconnect:
        print("Client disconnected")
    except Exception as e:
        print(f"WebSocket error: {str(e)}")
    finally:
        try:
            influx_client.close()
        except:
            pass
        await websocket.close()

@app.websocket("/ws/plc-write")
async def plc_write_ws(ws: WebSocket):
    await mgr.connect("plc-write-responses", ws)  # CORRECT
    try:
        while True:
            data = await ws.receive_json()
            try:
                command = PlcWriteCommand(**data)
                request_id = command.request_id or str(uuid.uuid4())
                kafka_message = command.model_copy(update={"request_id": request_id}).model_dump()

                mgr.pending_write_responses[request_id] = ws
                await kafka_producer.send_and_wait(PLC_WRITE_COMMANDS_TOPIC, value=kafka_message)

                await ws.send_json({
                    "type": "ack",
                    "status": "pending",
                    "request_id": request_id,
                    "message": "PLC write command enqueued"
                })
            except Exception as e:
                await ws.send_json({"type": "error", "message": str(e)})
    except WebSocketDisconnect:
        mgr.disconnect("plc-write-responses", ws)


@app.websocket("/ws/{stream}")
async def ws_endpoint(stream: str, ws: WebSocket):
    if stream not in WS_TOPICS:
        await ws.close(code=1008)
        return
    await mgr.connect(stream, ws)
    try:
        while True:
            await ws.receive_text()  # Ignore input; server-push only
    except WebSocketDisconnect:
        mgr.disconnect(stream, ws)

