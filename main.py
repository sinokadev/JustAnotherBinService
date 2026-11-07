import hashlib
import logging
from logging.handlers import TimedRotatingFileHandler
import math
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import event
from sqlmodel import Field, Session, SQLModel, create_engine, func, select
from starlette.middleware.gzip import GZipMiddleware

# Load environment variables
load_dotenv()

# Configuration
DATABASE_FILE = os.getenv("DATABASE_FILE", "database.db")
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATABASE_FILE}")
MAX_CONTENT_LENGTH = int(os.getenv("MAX_CONTENT_LENGTH", "50000000"))
PAGE_SIZE = int(os.getenv("PAGE_SIZE", "50"))
BIN_PREVIEW_LENGTH = 200
TRUST_PROXY_HEADERS = os.getenv("TRUST_PROXY_HEADERS", "false").lower() == "true"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin1234")
ADMIN_SESSION_TOKEN = hashlib.sha256(f"admin_salt_{ADMIN_PASSWORD}".encode()).hexdigest()

# Logging setup
file_handler = TimedRotatingFileHandler("app.log", when="MIDNIGHT", interval=1, backupCount=90, encoding="utf-8")
file_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[file_handler, logging.StreamHandler()])
logger = logging.getLogger(__name__)


# Database model & setup
class BinModel(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    content: str
    created_at: datetime | None = Field(default_factory=lambda: datetime.now(timezone.utc))
    is_deleted: bool = Field(default=False)


connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)

if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA synchronous = NORMAL")
        cursor.execute("PRAGMA busy_timeout = 5000")
        cursor.close()


def create_db_and_tables():
    SQLModel.metadata.create_all(engine)
    with engine.begin() as connection:
        columns = connection.exec_driver_sql("PRAGMA table_info(binmodel)").all()
        col_names = {column[1] for column in columns}
        if columns and "created_at" not in col_names:
            connection.exec_driver_sql("ALTER TABLE binmodel ADD COLUMN created_at DATETIME")
        if columns and "is_deleted" not in col_names:
            connection.exec_driver_sql("ALTER TABLE binmodel ADD COLUMN is_deleted BOOLEAN DEFAULT 0")


def get_session():
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


# Utilities
def format_bytes(size: int) -> str:
    units = ("bytes", "KiB", "MiB", "GiB", "TiB")
    value = float(size)
    for unit in units[:-1]:
        if value < 1024:
            return f"{int(value)} {unit}" if unit == "bytes" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} {units[-1]}"


def get_real_ip(request: Request) -> str:
    if TRUST_PROXY_HEADERS:
        x_real_ip = request.headers.get("X-Real-IP")
        if x_real_ip:
            return x_real_ip.strip()
        x_forwarded_for = request.headers.get("X-Forwarded-For")
        if x_forwarded_for:
            return x_forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def mask_ip(ip: str) -> str:
    if ip == "unknown":
        return ip
    parts = ip.split(".")
    if len(parts) == 4:
        return f"{parts[0]}.{parts[1]}.{parts[2]}.xxx"
    return hashlib.sha256(ip.encode()).hexdigest()[:12]


def is_admin(request: Request) -> bool:
    cookie_token = request.cookies.get("admin_session")
    return bool(cookie_token and cookie_token == ADMIN_SESSION_TOKEN)


def get_bin_or_404(session: Session, bin_id: int, request: Request | None = None, label: str = "") -> BinModel:
    bin_item = session.get(BinModel, bin_id)
    if not bin_item:
        if request:
            prefix = f" ({label})" if label else ""
            logger.warning("Bin not found%s - ID: %d, IP: %s", prefix, bin_id, mask_ip(get_real_ip(request)))
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bin not found")
    return bin_item


def create_bin(request: Request, session: Session, content: str) -> BinModel:
    clean_content = content.strip()
    client_ip = mask_ip(get_real_ip(request))

    if not clean_content:
        logger.warning("Empty content submission attempt - IP: %s", client_ip)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Content cannot be empty")

    if len(clean_content) > MAX_CONTENT_LENGTH:
        logger.warning("Content length exceeded - length: %d, IP: %s", len(clean_content), client_ip)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Content exceeds maximum limit of {MAX_CONTENT_LENGTH} characters",
        )

    bin_item = BinModel(content=clean_content)
    session.add(bin_item)
    session.commit()
    session.refresh(bin_item)

    logger.info("New bin created - ID: %s, length: %d, IP: %s", bin_item.id, len(clean_content), client_ip)
    return bin_item


# Lifespan & App Setup
@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    logger.info("Application started - DB tables created")
    yield
    logger.info("Application stopped")


class CachedStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "public, max-age=604800, stale-while-revalidate=86400"
        return response


limiter = Limiter(key_func=get_real_ip)
app = FastAPI(lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=500)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.mount("/static", CachedStaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


def render_bin_page(
    request: Request,
    bin_id: int | str,
    content: str | None,
    created_at: datetime | None = None,
    view: str = "text",
    is_local: bool = False,
):
    content_size_bytes = len(content.encode("utf-8")) if content is not None else 0
    text_url = "/bin/local" if is_local else f"/bin/{bin_id}"
    hex_url = f"{text_url}/hex"

    return templates.TemplateResponse(
        request=request,
        name="bin.html",
        context={
            "bin_id": str(bin_id),
            "bin_content": content or "",
            "content_length": len(content) if content is not None else 0,
            "content_size_bytes": content_size_bytes,
            "content_size_human": format_bytes(content_size_bytes),
            "created_at": created_at,
            "max_content_length": MAX_CONTENT_LENGTH,
            "is_local": is_local,
            "view": view,
            "text_url": text_url,
            "hex_url": hex_url,
            "local_url": "/bin/local/hex" if view == "hex" else "/bin/local",
        },
    )


# Web Routes
@app.get("/", response_class=HTMLResponse)
async def main(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.post("/bin", response_class=RedirectResponse)
@limiter.limit("10/minute")
async def post_bin(request: Request, session: SessionDep, content: str = Form()):
    bin_item = create_bin(request=request, session=session, content=content)
    return RedirectResponse(url=f"/bin/{bin_item.id}", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/bin/local", response_class=HTMLResponse)
async def get_local_bin(request: Request):
    return render_bin_page(request, "local", None, is_local=True)


@app.get("/bin/local/hex", response_class=HTMLResponse)
async def get_local_bin_hex(request: Request):
    return render_bin_page(request, "local", None, view="hex", is_local=True)


@app.get("/bin/{bin_id}", response_class=HTMLResponse)
@limiter.limit("60/minute")
async def get_bin(request: Request, bin_id: int, session: SessionDep):
    bin_item = get_bin_or_404(session, bin_id, request, "HTML")
    if bin_item.is_deleted:
        return templates.TemplateResponse(
            request=request,
            name="deleted.html",
            context={"bin_id": bin_id},
            status_code=status.HTTP_410_GONE,
        )
    logger.info("Bin HTML retrieved - ID: %d, IP: %s", bin_id, mask_ip(get_real_ip(request)))
    return render_bin_page(request, bin_id, bin_item.content, bin_item.created_at)


@app.get("/bin/{bin_id}/hex", response_class=HTMLResponse)
@limiter.limit("60/minute")
async def get_bin_hex(request: Request, bin_id: int, session: SessionDep):
    bin_item = get_bin_or_404(session, bin_id, request, "HEX")
    if bin_item.is_deleted:
        return templates.TemplateResponse(
            request=request,
            name="deleted.html",
            context={"bin_id": bin_id},
            status_code=status.HTTP_410_GONE,
        )
    return render_bin_page(request, bin_id, bin_item.content, bin_item.created_at, view="hex")


@app.get("/raw/{bin_id}", response_class=PlainTextResponse)
@limiter.limit("10/minute")
async def get_bin_raw(request: Request, bin_id: int, session: SessionDep):
    bin_item = get_bin_or_404(session, bin_id, request, "RAW")
    if bin_item.is_deleted:
        return PlainTextResponse("This bin has been deleted.", status_code=status.HTTP_410_GONE)
    logger.info("Bin RAW retrieved - ID: %d, IP: %s", bin_id, mask_ip(get_real_ip(request)))
    return bin_item.content


@app.get("/random", response_class=RedirectResponse)
async def get_random_bin(session: SessionDep):
    bin_item = session.exec(
        select(BinModel).where(BinModel.is_deleted == False).order_by(func.random()).limit(1)  # noqa: E712
    ).first()
    if bin_item is None:
        return RedirectResponse("/bins", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse(f"/bin/{bin_item.id}", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/bins", response_class=HTMLResponse)
@limiter.limit("30/minute")
async def get_bins_list(request: Request, session: SessionDep, page: int = 1):
    page = max(1, page)
    where_clause = BinModel.is_deleted == False  # noqa: E712
    total_count = session.exec(select(func.count(BinModel.id)).where(where_clause)).one()
    total_pages = math.ceil(total_count / PAGE_SIZE) or 1
    offset = (page - 1) * PAGE_SIZE

    bins = session.exec(
        select(BinModel).where(where_clause).order_by(BinModel.id.desc()).offset(offset).limit(PAGE_SIZE)
    ).all()
    logger.info("Bin list retrieved (HTML) - page: %d/%d, IP: %s", page, total_pages, mask_ip(get_real_ip(request)))

    return templates.TemplateResponse(
        request=request,
        name="bins.html",
        context={
            "bins": bins,
            "page": page,
            "total_pages": total_pages,
            "has_next": page < total_pages,
            "has_prev": page > 1,
        },
    )


# API Routes
@app.post("/api/bin", status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
async def post_bin_api(request: Request, session: SessionDep, content: str = Form()):
    bin_item = create_bin(request=request, session=session, content=content)
    return {
        "id": bin_item.id,
        "url": f"/bin/{bin_item.id}",
        "raw_url": f"/raw/{bin_item.id}",
        "api_url": f"/api/bin/{bin_item.id}",
        "created_at": bin_item.created_at,
    }


@app.get("/api/bin/{bin_id}")
@limiter.limit("10/minute")
async def get_bin_json(request: Request, bin_id: int, session: SessionDep):
    bin_item = get_bin_or_404(session, bin_id, request, "JSON")
    if bin_item.is_deleted:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="This bin has been deleted.")
    logger.info("Bin JSON retrieved - ID: %d, IP: %s", bin_id, mask_ip(get_real_ip(request)))
    return bin_item


@app.get("/api/bins")
@limiter.limit("30/minute")
async def get_bins_json(request: Request, session: SessionDep, page: int = 1, page_size: int = 50):
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    where_clause = BinModel.is_deleted == False  # noqa: E712
    total_count = session.exec(select(func.count(BinModel.id)).where(where_clause)).one()
    offset = (page - 1) * page_size

    statement = (
        select(
            BinModel.id,
            func.substr(BinModel.content, 1, BIN_PREVIEW_LENGTH).label("preview"),
            func.length(BinModel.content).label("content_length"),
            BinModel.created_at,
        )
        .where(where_clause)
        .order_by(BinModel.id.desc())
        .offset(offset)
        .limit(page_size)
    )
    rows = session.exec(statement).all()
    items = [
        {"id": row.id, "preview": row.preview, "content_length": row.content_length, "created_at": row.created_at}
        for row in rows
    ]
    total_pages = math.ceil(total_count / page_size) or 1

    logger.info(
        "Bin list retrieved (JSON) - page: %d, size: %d, total: %d, IP: %s",
        page, page_size, total_count, mask_ip(get_real_ip(request)),
    )

    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total_count": total_count,
        "total_pages": total_pages,
    }


# Admin Routes
@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request, next: str = "/admin"):
    if is_admin(request):
        return RedirectResponse(url=next, status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request=request, name="admin/login.html", context={"next_url": next, "error": None})


@app.post("/admin/login")
async def admin_login(request: Request, password: str = Form(""), next: str = Form("/admin")):
    if password == ADMIN_PASSWORD:
        resp = RedirectResponse(url=next, status_code=status.HTTP_303_SEE_OTHER)
        resp.set_cookie(key="admin_session", value=ADMIN_SESSION_TOKEN, httponly=True, samesite="lax")
        return resp
    return templates.TemplateResponse(
        request=request,
        name="admin/login.html",
        context={"next_url": next, "error": "Invalid password."},
        status_code=status.HTTP_401_UNAUTHORIZED,
    )


@app.get("/admin/logout")
async def admin_logout():
    resp = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(key="admin_session")
    return resp


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request, session: SessionDep, page: int = 1):
    if not is_admin(request):
        return RedirectResponse(url="/admin/login?next=/admin", status_code=status.HTTP_303_SEE_OTHER)
    page = max(1, page)
    total_count = session.exec(select(func.count(BinModel.id))).one()
    total_pages = math.ceil(total_count / PAGE_SIZE) or 1
    offset = (page - 1) * PAGE_SIZE

    bins = session.exec(select(BinModel).order_by(BinModel.id.desc()).offset(offset).limit(PAGE_SIZE)).all()
    return templates.TemplateResponse(
        request=request,
        name="admin/bins.html",
        context={
            "bins": bins,
            "page": page,
            "total_pages": total_pages,
            "total_count": total_count,
        },
    )


@app.get("/admin/bin/{bin_id}", response_class=HTMLResponse)
async def admin_get_bin(request: Request, bin_id: int, session: SessionDep):
    if not is_admin(request):
        return RedirectResponse(url=f"/admin/login?next=/admin/bin/{bin_id}", status_code=status.HTTP_303_SEE_OTHER)
    bin_item = get_bin_or_404(session, bin_id, request, "ADMIN")
    size_bytes = len(bin_item.content.encode("utf-8"))
    return templates.TemplateResponse(
        request=request,
        name="admin/bin.html",
        context={
            "bin": bin_item,
            "content_size_bytes": size_bytes,
            "content_size_human": format_bytes(size_bytes),
        },
    )


@app.post("/admin/bin/{bin_id}/delete")
async def admin_delete_bin(request: Request, bin_id: int, session: SessionDep, return_to: str = Form("/admin")):
    if not is_admin(request):
        return RedirectResponse(url=f"/admin/login?next=/admin/bin/{bin_id}", status_code=status.HTTP_303_SEE_OTHER)
    bin_item = get_bin_or_404(session, bin_id, request, "ADMIN_DELETE")
    bin_item.is_deleted = True
    session.add(bin_item)
    session.commit()
    logger.info("Bin soft-deleted - ID: %d, IP: %s", bin_id, mask_ip(get_real_ip(request)))
    return RedirectResponse(url=return_to, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/bin/{bin_id}/restore")
async def admin_restore_bin(request: Request, bin_id: int, session: SessionDep, return_to: str = Form("/admin")):
    if not is_admin(request):
        return RedirectResponse(url=f"/admin/login?next=/admin/bin/{bin_id}", status_code=status.HTTP_303_SEE_OTHER)
    bin_item = get_bin_or_404(session, bin_id, request, "ADMIN_RESTORE")
    bin_item.is_deleted = False
    session.add(bin_item)
    session.commit()
    logger.info("Bin restored - ID: %d, IP: %s", bin_id, mask_ip(get_real_ip(request)))
    return RedirectResponse(url=return_to, status_code=status.HTTP_303_SEE_OTHER)
