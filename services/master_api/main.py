from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel
from typing import Optional, List ,Dict
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime  # Add this import at the top
from passlib.context import CryptContext
from fastapi.middleware.cors import CORSMiddleware  # Add this import at the top
# Initialize FastAPI
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)
# PostgreSQL Configuration
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

# Create roles table if not exists
cur.execute("""
CREATE TABLE IF NOT EXISTS roles (
    id SERIAL PRIMARY KEY,
    name VARCHAR(50) UNIQUE NOT NULL,
    description TEXT
)
""")
cur.execute("""
    CREATE TABLE IF NOT EXISTS operator_sessions (
        id SERIAL PRIMARY KEY,
        user_id INTEGER REFERENCES users(id),
        username TEXT NOT NULL,
        role_id INTEGER REFERENCES roles(id),
        login_ts TIMESTAMP DEFAULT NOW(),
        shift_id INTEGER REFERENCES shift_master(id),
        logout_ts TIMESTAMP NULL,
        session_token TEXT
    )
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    email VARCHAR(100) UNIQUE NOT NULL,
    hashed_password TEXT NOT NULL,
    full_name VARCHAR(100),
    is_active BOOLEAN DEFAULT TRUE,
    role_id INTEGER REFERENCES roles(id),
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
)
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS shift_master (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    start_time TIMESTAMP NOT NULL,
    end_time TIMESTAMP NOT NULL,
    description TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TIMESTAMP DEFAULT NOW()
)
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS modules (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL,
    description TEXT,
    created_at TIMESTAMP DEFAULT NOW()
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS permission_policies (
    id SERIAL PRIMARY KEY,
    role_id INTEGER NOT NULL REFERENCES roles(id),
    module_id INTEGER NOT NULL REFERENCES modules(id),
    permission BOOLEAN DEFAULT FALSE,
    UNIQUE(role_id, module_id)
)
""")
# Pydantic Models
class RoleCreate(BaseModel):
    name: str
    description: Optional[str] = None

class RoleResponse(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
class UserBase(BaseModel):
    username: str
    email: str
    full_name: Optional[str] = None
    is_active: Optional[bool] = True
    role_id: Optional[int] = None

class UserCreate(UserBase):
    password: str  # Will be hashed before storage

class UserResponse(UserBase):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True

pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto",
    bcrypt__rounds=12  # Adjust based on your security needs
)
# Password hashing (simplified - use proper hashing in production)
def hash_password(password: str) -> str:
    """Properly hash a password using bcrypt"""
    return pwd_context.hash(password)
# Helper functions
def row_to_user(row):
    return {
        "id": row[0],
        "username": row[1],
        "email": row[2],
        "full_name": row[3],
        "is_active": row[4],
        "role_id": row[5],
        "created_at": row[6],
        "updated_at": row[7]
    }

class ShiftBase(BaseModel):
    name: str
    start_time: datetime
    end_time: datetime
    description: Optional[str] = None
    created_by: Optional[int] = None

class ShiftCreate(ShiftBase):
    pass

class ShiftResponse(ShiftBase):
    id: int
    created_at: datetime

    class Config:
        orm_mode = True
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }

# Helper function to convert DB row to response model
def row_to_shift(row):
    return {
        "id": row[0],
        "name": row[1],
        "start_time": row[2],
        "end_time": row[3],
        "description": row[4],
        "created_by": row[5],
        "created_at": row[6]
    }

# Pydantic Models
class ModuleBase(BaseModel):
    name: str
    description: Optional[str] = None

class ModuleCreate(ModuleBase):
    pass

class ModuleResponse(ModuleBase):
    id: int
    created_at: datetime  # Changed to string type

    class Config:
            orm_mode = True
            json_encoders = {
                datetime: lambda v: v.isoformat()
            }

class PermissionPolicyBase(BaseModel):
    role_id: int
    module_id: int
    permission: bool = False

class PermissionPolicyResponse(PermissionPolicyBase):
    module_name: str

# Helper functions
def row_to_module(row):
    return {
        "id": row[0],
        "name": row[1],
        "description": row[2],
        "created_at": row[3]
    }

def row_to_permission(row):
    return {
        "id": row[0],
        "role_id": row[1],
        "module_id": row[2],
        "permission": row[3],
        "module_name": row[4] if len(row) > 4 else None
    }
class BulkPermissionRequest(BaseModel):
    role_id: int
    permissions: Dict[int, bool]  # {module_id: permission_value}


# User CRUD Endpoints
@app.post("/api/users/", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def create_user(user: UserCreate):
    try:
        hashed_pw = hash_password(user.password)
        cur.execute(
            """INSERT INTO users 
            (username, email, hashed_password, full_name, is_active, role_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id, username, email, full_name, is_active, role_id, created_at, updated_at""",
            (user.username, user.email, hashed_pw, user.full_name, user.is_active, user.role_id)
        )
        new_user = cur.fetchone()
        return row_to_user(new_user)
    except psycopg2.IntegrityError as e:
        if "duplicate key value violates unique constraint" in str(e):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Username or email already exists"
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid role_id" if "violates foreign key constraint" in str(e) else "Database error"
        )

@app.get("/api/users/{username}", response_model=UserResponse)
def get_user(username: str):
    cur.execute(
        """SELECT id, username, email, full_name, is_active, role_id, created_at, updated_at 
        FROM users WHERE username = %s""",
        (username,)
    )
    user = cur.fetchone()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User {username} not found"
        )
    return row_to_user(user)

@app.get("/api/users/", response_model=List[UserResponse])
def get_all_users(skip: int = 0, limit: int = 100):
    cur.execute(
        """SELECT id, username, email, full_name, is_active, role_id, created_at, updated_at 
        FROM users ORDER BY id OFFSET %s LIMIT %s""",
        (skip, limit)
    )
    return [row_to_user(row) for row in cur.fetchall()]

@app.put("/api/users/{username}", response_model=UserResponse)
def update_user(username: str, user: UserBase):
    try:
        cur.execute(
            """UPDATE users SET 
            email = %s, 
            full_name = %s, 
            is_active = %s, 
            role_id = %s,
            updated_at = NOW()
            WHERE username = %s
            RETURNING id, username, email, full_name, is_active, role_id, created_at, updated_at""",
            (user.email, user.full_name, user.is_active, user.role_id, username)
        )
        updated_user = cur.fetchone()
        if not updated_user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User {username} not found"
            )
        return row_to_user(updated_user)
    except psycopg2.IntegrityError as e:
        if "duplicate key value violates unique constraint" in str(e):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already exists"
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid role_id" if "violates foreign key constraint" in str(e) else "Database error"
        )

@app.delete("/api/users/{username}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(username: str):
    cur.execute("DELETE FROM users WHERE username = %s", (username,))
    if cur.rowcount == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User {username} not found"
        )

# Health check
@app.get("/api/health")
def health_check():
    try:
        cur.execute("SELECT 1")
        return {"status": "ok", "database": "connected"}
    except:
        return {"status": "unhealthy", "database": "disconnected"}

# CRUD Endpoints
@app.post("/api/roles/", response_model=RoleResponse, status_code=status.HTTP_201_CREATED)
def create_role(role: RoleCreate):
    try:
        cur.execute(
            "INSERT INTO roles (name, description) VALUES (%s, %s) RETURNING id",
            (role.name, role.description)
        )
        role_id = cur.fetchone()[0]
        return {
            "id": role_id,
            "name": role.name,
            "description": role.description
        }
    except psycopg2.IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Role with this name already exists"
        )

@app.get("/api/roles/{role_id}", response_model=RoleResponse)
def get_role(role_id: int):
    cur.execute("SELECT * FROM roles WHERE id = %s", (role_id,))
    role = cur.fetchone()
    if not role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Role not found"
        )
    return {
        "id": role[0],
        "name": role[1],
        "description": role[2]
    }

@app.get("/api/roles/", response_model=List[RoleResponse])
def get_roles(skip: int = 0, limit: int = 100):
    cur.execute("SELECT * FROM roles OFFSET %s LIMIT %s", (skip, limit))
    roles = cur.fetchall()
    return [
        {
            "id": r[0],
            "name": r[1],
            "description": r[2]
        }
        for r in roles
    ]

@app.put("/api/roles/{role_id}", response_model=RoleResponse)
def update_role(role_id: int, role: RoleCreate):
    cur.execute(
        "UPDATE roles SET name = %s, description = %s WHERE id = %s RETURNING *",
        (role.name, role.description, role_id)
    )
    updated_role = cur.fetchone()
    if not updated_role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Role not found"
        )
    return {
        "id": updated_role[0],
        "name": updated_role[1],
        "description": updated_role[2]
    }

@app.delete("/api/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_role(role_id: int):
    cur.execute("DELETE FROM roles WHERE id = %s", (role_id,))
    if cur.rowcount == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Role not found"
        )
    return None

# CRUD Endpoints
@app.post("/api/shifts/", response_model=ShiftResponse, status_code=status.HTTP_201_CREATED)
def create_shift(shift: ShiftCreate):
    try:
        cur.execute(
            """INSERT INTO shift_master 
            (name, start_time, end_time, description, created_by)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id, name, start_time, end_time, description, created_by, created_at""",
            (shift.name, shift.start_time, shift.end_time, shift.description, shift.created_by)
        )
        new_shift = cur.fetchone()
        return row_to_shift(new_shift)
    except psycopg2.IntegrityError as e:
        if "duplicate key value violates unique constraint" in str(e):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Shift name already exists"
            )
        if "violates foreign key constraint" in str(e):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid created_by user ID"
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error"
        )

@app.get("/api/shifts/{shift_id}", response_model=ShiftResponse)
def get_shift(shift_id: int):
    cur.execute(
        """SELECT id, name, start_time, end_time, description, created_by, created_at 
        FROM shift_master WHERE id = %s""",
        (shift_id,)
    )
    shift = cur.fetchone()
    if not shift:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Shift with ID {shift_id} not found"
        )
    return row_to_shift(shift)

@app.get("/api/shifts/", response_model=List[ShiftResponse])
def get_all_shifts(skip: int = 0, limit: int = 100):
    cur.execute(
        """SELECT id, name, start_time, end_time, description, created_by, created_at 
        FROM shift_master ORDER BY id OFFSET %s LIMIT %s""",
        (skip, limit)
    )
    return [row_to_shift(row) for row in cur.fetchall()]

@app.put("/api/shifts/{shift_id}", response_model=ShiftResponse)
def update_shift(shift_id: int, shift: ShiftBase):
    try:
        cur.execute(
            """UPDATE shift_master SET 
            name = %s, 
            start_time = %s, 
            end_time = %s, 
            description = %s,
            created_by = %s
            WHERE id = %s
            RETURNING id, name, start_time, end_time, description, created_by, created_at""",
            (shift.name, shift.start_time, shift.end_time, shift.description, shift.created_by, shift_id)
        )
        updated_shift = cur.fetchone()
        if not updated_shift:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Shift with ID {shift_id} not found"
            )
        return row_to_shift(updated_shift)
    except psycopg2.IntegrityError as e:
        if "duplicate key value violates unique constraint" in str(e):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Shift name already exists"
            )
        if "violates foreign key constraint" in str(e):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid created_by user ID"
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error"
        )

@app.delete("/api/shifts/{shift_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_shift(shift_id: int):
    cur.execute("DELETE FROM shift_master WHERE id = %s", (shift_id,))
    if cur.rowcount == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Shift with ID {shift_id} not found"
        )
@app.post("/api/modules/", response_model=ModuleResponse, status_code=status.HTTP_201_CREATED)
def create_module(module: ModuleCreate):
    try:
        cur.execute(
            """INSERT INTO modules (name, description)
            VALUES (%s, %s)
            RETURNING id, name, description, created_at""",
            (module.name, module.description)
        )
        return row_to_module(cur.fetchone())
    except psycopg2.IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Module name already exists"
        )

@app.get("/api/modules/", response_model=List[ModuleResponse])
def get_all_modules():
    cur.execute("SELECT id, name, description, created_at FROM modules")
    return [row_to_module(row) for row in cur.fetchall()]

# Permission Policy Endpoints
@app.post("/api/permissions/", response_model=PermissionPolicyBase, status_code=status.HTTP_201_CREATED)
def create_permission_policy(policy: PermissionPolicyBase):
    try:
        cur.execute(
            """INSERT INTO permission_policies 
            (role_id, module_id, permission)
            VALUES (%s, %s, %s)
            RETURNING id, role_id, module_id, permission""",
            (policy.role_id, policy.module_id, policy.permission)
        )
        return row_to_permission(cur.fetchone())
    except psycopg2.IntegrityError as e:
        if "violates foreign key constraint" in str(e):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid role_id or module_id"
            )
        if "duplicate key value violates unique constraint" in str(e):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Permission policy already exists for this role and module"
            )
        raise

@app.put("/api/permissions/{policy_id}", response_model=PermissionPolicyBase)
def update_permission_policy(policy_id: int, policy: PermissionPolicyBase):
    cur.execute(
        """UPDATE permission_policies SET
        permission = %s
        WHERE id = %s
        RETURNING id, role_id, module_id, permission""",
        (policy.permission, policy_id)
    )
    updated = cur.fetchone()
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Permission policy not found"
        )
    return row_to_permission(updated)

@app.get("/api/permissions/role/{role_id}", response_model=List[PermissionPolicyResponse])
def get_permissions_by_role(role_id: int):
    cur.execute(
        """SELECT p.id, p.role_id, p.module_id, p.permission, m.name as module_name
           FROM permission_policies p
           JOIN modules m ON p.module_id = m.id
           WHERE p.role_id = %s""",
        (role_id,)
    )
    return [row_to_permission(row) for row in cur.fetchall()]

@app.delete("/api/permissions/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_permission_policy(policy_id: int):
    cur.execute("DELETE FROM permission_policies WHERE id = %s", (policy_id,))
    if cur.rowcount == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Permission policy not found"
        )

@app.post("/api/permissions/bulk/", response_model=List[PermissionPolicyResponse])
def bulk_upsert_permissions(request: BulkPermissionRequest):
    results = []
    for module_id, permission in request.permissions.items():
        try:
            # Using PostgreSQL's ON CONFLICT for upsert
            cur.execute("""
                INSERT INTO permission_policies (role_id, module_id, permission)
                VALUES (%s, %s, %s)
                ON CONFLICT (role_id, module_id) 
                DO UPDATE SET permission = EXCLUDED.permission
                RETURNING id, role_id, module_id, permission
                """,
                (request.role_id, module_id, permission)
            )
            
            result = cur.fetchone()
            cur.execute("SELECT name FROM modules WHERE id = %s", (module_id,))
            module_name = cur.fetchone()[0]
            
            results.append({
                "id": result[0],
                "role_id": result[1],
                "module_id": result[2],
                "permission": result[3],
                "module_name": module_name
            })
            
        except psycopg2.Error as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Error processing module {module_id}: {str(e)}"
            )
    
    return results