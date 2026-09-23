from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from sqlalchemy import DateTime, Float, Integer, String, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.rules import classify


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg2://app:app@localhost:54391/methane"
    jwt_secret: str = "mine-methane-dev-secret"


settings = Settings()
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer(auto_error=False)
USERS = {
    "gasman": {"role": "writer", "password_hash": pwd.hash("gas123456")},
    "viewer": {"role": "reader", "password_hash": pwd.hash("view123456")},
}

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)

DEFAULT_THRESHOLD_MINUTES = 30
URGE_OPEN = "催办中"
URGE_DONE = "确认完毕"


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
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_by: Mapped[str | None] = mapped_column(String(64))
    disposal: Mapped[str | None] = mapped_column(String(200))


class UrgeEvent(Base):
    __tablename__ = "urge_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    site: Mapped[str] = mapped_column(String(80))
    urged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default=URGE_OPEN)
    disposal: Mapped[str | None] = mapped_column(String(200))
    confirmed_by: Mapped[str | None] = mapped_column(String(64))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UrgeSetting(Base):
    __tablename__ = "urge_settings"
    id: Mapped[int] = mapped_column(primary_key=True)
    threshold_minutes: Mapped[int] = mapped_column(Integer)


class LoginIn(BaseModel):
    username: str
    password: str


class ReadingIn(BaseModel):
    site: str = Field(min_length=1, max_length=80)
    ch4_pct: float


class ConfirmIn(BaseModel):
    disposal: str = Field(min_length=1, max_length=200)


class ThresholdIn(BaseModel):
    minutes: int = Field(ge=0)


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


def get_threshold(db: Session) -> int:
    row = db.get(UrgeSetting, 1)
    if row is None:
        row = UrgeSetting(id=1, threshold_minutes=DEFAULT_THRESHOLD_MINUTES)
        db.add(row)
        db.commit()
    return row.threshold_minutes


def scan_overdue(db: Session) -> None:
    """超时未确认的报警按测点归并进催办榜，每个测点只留一条进行中的大事记。"""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=get_threshold(db))
    rows = (
        db.query(Reading)
        .filter(Reading.level == "报警", Reading.confirmed_at.is_(None), Reading.created_at <= cutoff)
        .all()
    )
    now = datetime.now(timezone.utc)
    for site in {r.site for r in rows}:
        open_event = (
            db.query(UrgeEvent)
            .filter(UrgeEvent.site == site, UrgeEvent.status == URGE_OPEN)
            .first()
        )
        if open_event is None:
            db.add(UrgeEvent(site=site, urged_at=now, status=URGE_OPEN))
    db.commit()


sockets: set[WebSocket] = set()
app = FastAPI(title="矿井瓦斯班测台")


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE readings ADD COLUMN IF NOT EXISTS confirmed_at TIMESTAMPTZ"))
            conn.execute(text("ALTER TABLE readings ADD COLUMN IF NOT EXISTS confirmed_by VARCHAR(64)"))
            conn.execute(text("ALTER TABLE readings ADD COLUMN IF NOT EXISTS disposal VARCHAR(200)"))
    db = SessionLocal()
    try:
        get_threshold(db)
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
    finally:
        db.close()


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
        scan_overdue(db)
        payload = {"id": row.id, "site": row.site, "ch4_pct": row.ch4_pct, "level": row.level, "note": row.note}
    finally:
        db.close()
    dead = []
    for ws in list(sockets):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        sockets.discard(ws)
    return payload


@app.get("/api/urges")
def urge_board(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        scan_overdue(db)
        events = (
            db.query(UrgeEvent)
            .filter(UrgeEvent.status == URGE_OPEN)
            .order_by(UrgeEvent.urged_at.desc())
            .all()
        )
        board = []
        for ev in events:
            pending = (
                db.query(Reading)
                .filter(Reading.site == ev.site, Reading.level == "报警", Reading.confirmed_at.is_(None))
                .order_by(Reading.created_at)
                .all()
            )
            board.append(
                {
                    "event_id": ev.id,
                    "site": ev.site,
                    "urged_at": ev.urged_at,
                    "pending_count": len(pending),
                    "readings": [
                        {"id": r.id, "ch4_pct": r.ch4_pct, "created_at": r.created_at} for r in pending
                    ],
                }
            )
        return board
    finally:
        db.close()


@app.post("/api/urges/{site}/confirm")
def confirm_site(site: str, body: ConfirmIn, user: dict = Depends(require_writer)):
    disposal = body.disposal.strip()
    if not disposal:
        raise HTTPException(status_code=422, detail="请填写处置简述")
    db = SessionLocal()
    try:
        rows = (
            db.query(Reading)
            .filter(Reading.site == site, Reading.level == "报警", Reading.confirmed_at.is_(None))
            .all()
        )
        if not rows:
            raise HTTPException(status_code=404, detail="该测点没有待确认的报警")
        now = datetime.now(timezone.utc)
        for r in rows:
            r.confirmed_at = now
            r.confirmed_by = user["username"]
            r.disposal = disposal
        event = (
            db.query(UrgeEvent)
            .filter(UrgeEvent.site == site, UrgeEvent.status == URGE_OPEN)
            .first()
        )
        if event is not None:
            event.status = URGE_DONE
            event.disposal = disposal
            event.confirmed_by = user["username"]
            event.confirmed_at = now
        db.commit()
        return {"site": site, "confirmed": len(rows), "disposal": disposal}
    finally:
        db.close()


@app.get("/api/urge-events")
def urge_events(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        scan_overdue(db)
        rows = db.query(UrgeEvent).order_by(UrgeEvent.id.desc()).all()
        return [
            {
                "id": e.id,
                "site": e.site,
                "urged_at": e.urged_at,
                "status": e.status,
                "disposal": e.disposal,
                "confirmed_by": e.confirmed_by,
                "confirmed_at": e.confirmed_at,
            }
            for e in rows
        ]
    finally:
        db.close()


@app.get("/api/settings/confirm-threshold")
def read_confirm_threshold(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        return {"minutes": get_threshold(db)}
    finally:
        db.close()


@app.put("/api/settings/confirm-threshold")
def write_confirm_threshold(body: ThresholdIn, user: dict = Depends(require_writer)):
    db = SessionLocal()
    try:
        row = db.get(UrgeSetting, 1)
        if row is None:
            row = UrgeSetting(id=1, threshold_minutes=body.minutes)
            db.add(row)
        else:
            row.threshold_minutes = body.minutes
        db.commit()
        scan_overdue(db)
        return {"minutes": row.threshold_minutes}
    finally:
        db.close()


@app.websocket("/ws/alerts")
async def alerts(ws: WebSocket):
    await ws.accept()
    sockets.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        sockets.discard(ws)
