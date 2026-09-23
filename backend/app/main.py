import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from sqlalchemy import DateTime, Float, Integer, String, Text, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.rules import classify


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg2://app:app@localhost:54391/methane"
    jwt_secret: str = "mine-methane-dev-secret"
    urgent_default_minutes: int = 30


settings = Settings()
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer(auto_error=False)
USERS = {
    "gasman": {"role": "writer", "password_hash": pwd.hash("gas123456")},
    "viewer": {"role": "reader", "password_hash": pwd.hash("view123456")},
}

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


class Reading(Base):
    __tablename__ = "readings"
    id: Mapped[int] = mapped_column(primary_key=True)
    site: Mapped[str] = mapped_column(String(80))
    ch4_pct: Mapped[float] = mapped_column(Float)
    level: Mapped[str] = mapped_column(String(20))
    note: Mapped[str] = mapped_column(String(200))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # 催办确认：未确认为空
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    disposition_note: Mapped[str | None] = mapped_column(String(500), nullable=True)


class UrgentSetting(Base):
    """催办分钟门槛，单独存表，全系统一行。"""

    __tablename__ = "urgent_settings"
    id: Mapped[int] = mapped_column(primary_key=True)
    timeout_minutes: Mapped[int] = mapped_column(Integer)
    updated_by: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UrgentRecord(Base):
    """催办大事记：报警超时上榜写一条；确认后保留，状态改为确认完毕。"""

    __tablename__ = "urgent_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    site: Mapped[str] = mapped_column(String(80), index=True)
    reading_id: Mapped[int] = mapped_column(Integer, unique=True)
    ch4_pct: Mapped[float] = mapped_column(Float)
    kind: Mapped[str] = mapped_column(String(20))
    note: Mapped[str] = mapped_column(String(200))
    created_by: Mapped[str] = mapped_column(String(64))
    alarm_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="待确认")
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    disposition_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class LoginIn(BaseModel):
    username: str
    password: str


class ReadingIn(BaseModel):
    site: str = Field(min_length=1, max_length=80)
    ch4_pct: float


class ConfirmIn(BaseModel):
    site: str = Field(min_length=1, max_length=80)
    disposition_note: str = Field(min_length=1, max_length=500)


class UrgentSettingIn(BaseModel):
    timeout_minutes: int = Field(ge=0, le=60 * 24 * 7)


def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(security)) -> dict:
    if credentials is None:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        payload = jwt.decode(credentials.credentials, settings.jwt_secret, algorithms=["HS256"])
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="无效令牌") from exc
    username = payload.get("sub")
    if username not in USERS:
        raise HTTPException(status_code=401, detail="无效令牌")
    return {"username": username, "role": payload.get("role")}


def require_writer(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "writer":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅瓦斯检查员可操作")
    return user


sockets: set[WebSocket] = set()
app = FastAPI(title="矿井瓦斯班测台")


def get_timeout_minutes(db: Session) -> int:
    row = db.query(UrgentSetting).order_by(UrgentSetting.id.asc()).first()
    return row.timeout_minutes if row else settings.urgent_default_minutes


def sweep_urgent(db: Session) -> list[UrgentRecord]:
    """找出超过分钟门槛仍未确认的报警，按测点归并写入催办大事记。幂等。"""
    timeout = get_timeout_minutes(db)
    now = datetime.now(timezone.utc)
    deadline = now - timedelta(minutes=timeout)
    pending = (
        db.query(Reading)
        .filter(
            Reading.level == "报警",
            Reading.confirmed_at.is_(None),
            Reading.created_at <= deadline,
        )
        .order_by(Reading.created_at.asc())
        .all()
    )
    new_records: list[UrgentRecord] = []
    for reading in pending:
        exists = db.query(UrgentRecord).filter(UrgentRecord.reading_id == reading.id).first()
        if exists:
            continue
        record = UrgentRecord(
            site=reading.site,
            reading_id=reading.id,
            ch4_pct=reading.ch4_pct,
            kind=reading.level,
            note=reading.note,
            created_by=reading.created_by,
            alarm_at=reading.created_at,
            created_at=now,
            status="待确认",
        )
        db.add(record)
        new_records.append(record)
    if new_records:
        db.commit()
        for record in new_records:
            db.refresh(record)
    return new_records


def urgent_board(db: Session) -> list[dict]:
    """催办榜：仍有待确认催办记录的测点，按测点归并成一行。"""
    rows = (
        db.query(UrgentRecord)
        .filter(UrgentRecord.status == "待确认")
        .order_by(UrgentRecord.alarm_at.asc())
        .all()
    )
    by_site: dict[str, UrgentRecord] = {}
    for row in rows:
        current = by_site.get(row.site)
        if current is None or row.alarm_at < current.alarm_at:
            by_site[row.site] = row
    return [
        {
            "site": record.site,
            "reading_id": record.reading_id,
            "ch4_pct": record.ch4_pct,
            "kind": record.kind,
            "note": record.note,
            "created_by": record.created_by,
            "alarm_at": record.alarm_at.isoformat(),
            "urgent_since": record.created_at.isoformat(),
        }
        for record in sorted(by_site.values(), key=lambda r: r.alarm_at)
    ]


def urgent_timeline(db: Session) -> list[dict]:
    rows = db.query(UrgentRecord).order_by(UrgentRecord.id.desc()).all()
    return [
        {
            "id": r.id,
            "site": r.site,
            "reading_id": r.reading_id,
            "ch4_pct": r.ch4_pct,
            "kind": r.kind,
            "note": r.note,
            "created_by": r.created_by,
            "alarm_at": r.alarm_at.isoformat(),
            "created_at": r.created_at.isoformat(),
            "status": r.status,
            "confirmed_at": r.confirmed_at.isoformat() if r.confirmed_at else None,
            "confirmed_by": r.confirmed_by,
            "disposition_note": r.disposition_note,
        }
        for r in rows
    ]


async def broadcast(payload: dict):
    dead = []
    for ws in list(sockets):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        sockets.discard(ws)


async def broadcast_urgent(db: Session):
    await broadcast({"type": "urgent", "board": urgent_board(db), "timeline": urgent_timeline(db)})


async def urgent_sweep_loop():
    """定时扫描，保证不刷新页面也会自动上榜。"""
    while True:
        await asyncio.sleep(5)
        try:
            db = SessionLocal()
            try:
                new_records = await asyncio.to_thread(sweep_urgent, db)
                if new_records:
                    await broadcast_urgent(db)
            finally:
                db.close()
        except Exception:
            pass


@app.on_event("startup")
async def startup():
    Base.metadata.create_all(bind=engine)
    # 旧库补列（已存在则不变）
    columns = {c["name"] for c in inspect(engine).get_columns("readings")}
    with engine.begin() as conn:
        for name, ddl in (
            ("confirmed_at", "TIMESTAMP WITH TIME ZONE"),
            ("confirmed_by", "VARCHAR(64)"),
            ("disposition_note", "VARCHAR(500)"),
        ):
            if name not in columns:
                conn.execute(text(f"ALTER TABLE readings ADD COLUMN {name} {ddl}"))
    db = SessionLocal()
    try:
        if db.query(Reading).count() == 0:
            now = datetime.now(timezone.utc)
            for site, ch4 in (("东翼-12", 0.35), ("回风巷", 1.4)):
                level, note = classify(ch4)
                db.add(
                    Reading(
                        site=site,
                        ch4_pct=ch4,
                        level=level,
                        note=note,
                        created_by="gasman",
                        created_at=now,
                    )
                )
            db.commit()
        if db.query(UrgentSetting).count() == 0:
            db.add(
                UrgentSetting(
                    timeout_minutes=settings.urgent_default_minutes,
                    updated_by="system",
                    updated_at=datetime.now(timezone.utc),
                )
            )
            db.commit()
    finally:
        db.close()
    asyncio.create_task(urgent_sweep_loop())


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "mine-methane-shift"}


@app.post("/api/auth/login")
def login(body: LoginIn):
    user = USERS.get(body.username.strip())
    if not user or not pwd.verify(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    exp = datetime.now(timezone.utc) + timedelta(hours=8)
    token = jwt.encode(
        {"sub": body.username.strip(), "role": user["role"], "exp": exp},
        settings.jwt_secret,
        algorithm="HS256",
    )
    return {"access_token": token, "username": body.username.strip(), "role": user["role"]}


@app.get("/api/readings")
def list_readings(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        rows = db.query(Reading).order_by(Reading.id.desc()).all()
        return [
            {
                "id": r.id,
                "site": r.site,
                "ch4_pct": r.ch4_pct,
                "level": r.level,
                "note": r.note,
                "created_by": r.created_by,
                "confirmed": r.confirmed_at is not None,
            }
            for r in rows
        ]
    finally:
        db.close()


@app.post("/api/readings", status_code=201)
async def create_reading(body: ReadingIn, user: dict = Depends(require_writer)):
    level, note = classify(body.ch4_pct)
    db = SessionLocal()
    try:
        row = Reading(
            site=body.site.strip(),
            ch4_pct=body.ch4_pct,
            level=level,
            note=note,
            created_by=user["username"],
            created_at=datetime.now(timezone.utc),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        payload = {"id": row.id, "site": row.site, "ch4_pct": row.ch4_pct, "level": row.level, "note": row.note}
        # 门槛为 0 时新报警立刻上榜
        new_records = sweep_urgent(db)
        await broadcast({"type": "reading", **payload})
        if new_records:
            await broadcast_urgent(db)
    finally:
        db.close()
    return payload


@app.get("/api/urgent/board")
def get_urgent_board(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        sweep_urgent(db)
        return urgent_board(db)
    finally:
        db.close()


@app.get("/api/urgent/timeline")
def get_urgent_timeline(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        sweep_urgent(db)
        return urgent_timeline(db)
    finally:
        db.close()


@app.get("/api/urgent/settings")
def get_urgent_settings(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        return {"timeout_minutes": get_timeout_minutes(db)}
    finally:
        db.close()


@app.put("/api/urgent/settings")
async def update_urgent_settings(body: UrgentSettingIn, user: dict = Depends(require_writer)):
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        row = db.query(UrgentSetting).order_by(UrgentSetting.id.asc()).first()
        if row is None:
            row = UrgentSetting(timeout_minutes=body.timeout_minutes, updated_by=user["username"], updated_at=now)
            db.add(row)
        else:
            row.timeout_minutes = body.timeout_minutes
            row.updated_by = user["username"]
            row.updated_at = now
        db.commit()
        # 改完立刻生效：立即按新门槛扫描
        new_records = sweep_urgent(db)
        await broadcast_urgent(db)
    finally:
        db.close()
    return {"timeout_minutes": body.timeout_minutes, "newly_urgent": len(new_records)}


@app.post("/api/urgent/confirm")
async def confirm_urgent(body: ConfirmIn, user: dict = Depends(require_writer)):
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        records = (
            db.query(UrgentRecord)
            .filter(UrgentRecord.status == "待确认", UrgentRecord.site == body.site)
            .all()
        )
        if not records:
            raise HTTPException(status_code=404, detail="该测点不在催办榜上")
        reading_ids = [r.reading_id for r in records]
        for record in records:
            record.status = "确认完毕"
            record.confirmed_at = now
            record.confirmed_by = user["username"]
            record.disposition_note = body.disposition_note
        readings = db.query(Reading).filter(Reading.id.in_(reading_ids)).all()
        for reading in readings:
            reading.confirmed_at = now
            reading.confirmed_by = user["username"]
            reading.disposition_note = body.disposition_note
        db.commit()
        await broadcast_urgent(db)
    finally:
        db.close()
    return {"site": body.site, "status": "确认完毕"}


@app.websocket("/ws/alerts")
async def alerts(ws: WebSocket):
    await ws.accept()
    sockets.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        sockets.discard(ws)
