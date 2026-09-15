from fastapi import Depends, Form, Request, FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager
from sqlmodel import Field, Session, SQLModel, create_engine, func, select
from typing import Annotated
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from datetime import datetime, timezone
import math
import hashlib
import os
import logging

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("app.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class BinModel(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    content: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


sqlite_file_name = "database.db"
sqlite_url = f"sqlite:///{sqlite_file_name}"
connect_args = {"check_same_thread": False}
engine = create_engine(sqlite_url, connect_args=connect_args)


def create_db_and_tables():
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session


# 기본값 False = 프록시 없음(단일 인스턴스) 전제.
# 나중에 nginx 등 리버스 프록시를 앞에 두게 되면 환경변수만 true로 켜면 됨.
# 단, 이때 nginx가 X-Forwarded-For를 $remote_addr로 "덮어쓰도록" 설정되어 있어야 함
# (proxy_set_header X-Forwarded-For $remote_addr;) - 그렇지 않으면 스푸핑 가능.
TRUST_PROXY_HEADERS = os.getenv("TRUST_PROXY_HEADERS", "false").lower() == "true"


def get_real_ip(request: Request) -> str:
    if TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def mask_ip(ip: str) -> str:
    """로그에 남기기 전 IP를 마스킹. rate limit 키 등에는 원본(get_real_ip)을 그대로 쓰고,
    로깅할 때만 이 함수를 통과시킨다."""
    if ip == "unknown":
        return ip
    parts = ip.split(".")
    if len(parts) == 4:  # IPv4
        return f"{parts[0]}.{parts[1]}.{parts[2]}.xxx"
    # IPv6나 기타 포맷은 해시로
    return hashlib.sha256(ip.encode()).hexdigest()[:12]


SessionDep = Annotated[Session, Depends(get_session)]


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    logger.info("Application started - DB tables created")
    yield
    logger.info("Application stopped")


limiter = Limiter(key_func=get_real_ip)
app = FastAPI(lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

MAX_CONTENT_LENGTH = 5_000_000


@app.get("/", response_class=HTMLResponse)
async def main(request: Request):
    return templates.TemplateResponse(
        request=request, name="index.html"
    )


@app.post("/bin", response_class=RedirectResponse)
@limiter.limit("10/minute")
async def post_bin(request: Request, session: SessionDep, content: str = Form()):
    clean_content = content.strip()

    if not clean_content:
        logger.warning(f"Empty content submission attempt - IP: {mask_ip(get_real_ip(request))}")
        raise HTTPException(status_code=400, detail="Content cannot be empty")

    if len(clean_content) > MAX_CONTENT_LENGTH:
        logger.warning(
            f"Content length exceeded - length: {len(clean_content)}, IP: {mask_ip(get_real_ip(request))}"
        )
        raise HTTPException(
            status_code=400,
            detail=f"Content exceeds maximum limit of {MAX_CONTENT_LENGTH} characters"
        )

    bin_item = BinModel(content=clean_content)
    session.add(bin_item)
    session.commit()
    session.refresh(bin_item)

    logger.info(f"New bin created - ID: {bin_item.id}, length: {len(clean_content)}, IP: {mask_ip(get_real_ip(request))}")
    return RedirectResponse(f"/bin/{bin_item.id}", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/api/bin/{bin_id}")
@limiter.limit("10/minute")
async def get_bin_json(request: Request, bin_id: int, session: SessionDep):
    bin_asdf = session.get(BinModel, bin_id)
    if not bin_asdf:
        logger.warning(f"Bin not found (JSON) - ID: {bin_id}, IP: {mask_ip(get_real_ip(request))}")
        raise HTTPException(status_code=404, detail="Bin not found")

    logger.info(f"Bin JSON retrieved - ID: {bin_id}, IP: {mask_ip(get_real_ip(request))}")
    return bin_asdf


@app.get("/raw/{bin_id}", response_class=PlainTextResponse)
@limiter.limit("10/minute")
async def get_bin_raw(request: Request, bin_id: int, session: SessionDep):
    bin_item = session.get(BinModel, bin_id)
    if not bin_item:
        logger.warning(f"Bin not found (RAW) - ID: {bin_id}, IP: {mask_ip(get_real_ip(request))}")
        raise HTTPException(status_code=404, detail="Bin not found")

    logger.info(f"Bin RAW retrieved - ID: {bin_id}, IP: {mask_ip(get_real_ip(request))}")
    return bin_item.content


@app.get("/bin/{bin_id}", response_class=HTMLResponse)
@limiter.limit("60/minute")
async def get_bin(request: Request, bin_id: int, session: SessionDep):
    bin_asdf = session.get(BinModel, bin_id)
    if not bin_asdf:
        logger.warning(f"Bin not found (HTML) - ID: {bin_id}, IP: {mask_ip(get_real_ip(request))}")
        raise HTTPException(status_code=404, detail="Bin not found")

    logger.info(f"Bin HTML retrieved - ID: {bin_id}, IP: {mask_ip(get_real_ip(request))}")
    return templates.TemplateResponse(
        request=request,
        name="bin.html",
        context={"bin_id": bin_id, "bin_content": bin_asdf.content}
    )


PAGE_SIZE = 50


@app.get("/bins", response_class=HTMLResponse)
@limiter.limit("30/minute")
async def get_bins_list(request: Request, session: SessionDep, page: int = 1):
    if page < 1:
        page = 1

    total_count = session.exec(select(func.count(BinModel.id))).one()
    total_pages = math.ceil(total_count / PAGE_SIZE) or 1
    offset = (page - 1) * PAGE_SIZE

    statement = (
        select(BinModel)
        .order_by(BinModel.id.desc())
        .offset(offset)
        .limit(PAGE_SIZE)
    )
    bins = session.exec(statement).all()

    logger.info(f"Bin list retrieved (HTML) - page: {page}/{total_pages}, IP: {mask_ip(get_real_ip(request))}")
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


@app.get("/api/bins")
@limiter.limit("30/minute")
async def get_bins_json(request: Request, session: SessionDep, page: int = 1, page_size: int = 50):
    if page < 1:
        page = 1
    page_size = max(1, min(page_size, 100))  # 하한 추가: 0/음수 → ZeroDivisionError 방지

    total_count = session.exec(select(func.count(BinModel.id))).one()
    offset = (page - 1) * page_size

    statement = (
        select(BinModel)
        .order_by(BinModel.id.desc())
        .offset(offset)
        .limit(page_size)
    )
    bins = session.exec(statement).all()

    logger.info(
        f"Bin list retrieved (JSON) - page: {page}, size: {page_size}, total: {total_count}, IP: {mask_ip(get_real_ip(request))}"
    )
    return {
        "items": bins,
        "page": page,
        "page_size": page_size,
        "total_count": total_count,
        "total_pages": math.ceil(total_count / page_size) or 1,
    }