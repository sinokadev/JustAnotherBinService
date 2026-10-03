from fastapi import Depends, Form, Request, FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager
from sqlmodel import Field, Session, SQLModel, create_engine, func, select
from typing import Annotated
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from datetime import datetime, timezone
import math
import hashlib
import os
import logging
from logging.handlers import TimedRotatingFileHandler


file_handler = TimedRotatingFileHandler(
    "app.log",
    when="MIDNIGHT",
    interval=1,
    backupCount=90,
    encoding="utf-8",
)

file_handler.setFormatter(
    logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
)

logging.basicConfig(
    level=logging.INFO,
    handlers=[
        file_handler,
        logging.StreamHandler(),
    ],
)

logger = logging.getLogger(__name__)


class BinModel(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    content: str
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


sqlite_file_name = "database.db"
sqlite_url = f"sqlite:///{sqlite_file_name}"

connect_args = {
    "check_same_thread": False,
}

engine = create_engine(
    sqlite_url,
    connect_args=connect_args,
)


def create_db_and_tables():
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]

TRUST_PROXY_HEADERS = (
    os.getenv("TRUST_PROXY_HEADERS", "false").lower() == "true"
)


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    logger.info("Application started - DB tables created")

    yield

    logger.info("Application stopped")


limiter = Limiter(
    key_func=get_real_ip,
)

app = FastAPI(
    lifespan=lifespan,
)

app.state.limiter = limiter

app.add_exception_handler(
    RateLimitExceeded,
    _rate_limit_exceeded_handler,
)

app.mount(
    "/static",
    StaticFiles(directory="static"),
    name="static",
)

templates = Jinja2Templates(
    directory="templates",
)

MAX_CONTENT_LENGTH = 50_000_000
PAGE_SIZE = 50


def create_bin(
    request: Request,
    session: Session,
    content: str,
) -> BinModel:
    clean_content = content.strip()

    if not clean_content:
        logger.warning(
            "Empty content submission attempt - IP: %s",
            mask_ip(get_real_ip(request)),
        )

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Content cannot be empty",
        )

    if len(clean_content) > MAX_CONTENT_LENGTH:
        logger.warning(
            "Content length exceeded - length: %d, IP: %s",
            len(clean_content),
            mask_ip(get_real_ip(request)),
        )

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Content exceeds maximum limit of "
                f"{MAX_CONTENT_LENGTH} characters"
            ),
        )

    bin_item = BinModel(
        content=clean_content,
    )

    session.add(bin_item)
    session.commit()
    session.refresh(bin_item)

    logger.info(
        "New bin created - ID: %s, length: %d, IP: %s",
        bin_item.id,
        len(clean_content),
        mask_ip(get_real_ip(request)),
    )

    return bin_item


@app.get(
    "/",
    response_class=HTMLResponse,
)
async def main(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
    )


@app.post(
    "/bin",
    response_class=RedirectResponse,
)
@limiter.limit("10/minute")
async def post_bin(
    request: Request,
    session: SessionDep,
    content: str = Form(),
):
    bin_item = create_bin(
        request=request,
        session=session,
        content=content,
    )

    return RedirectResponse(
        url=f"/bin/{bin_item.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post(
    "/api/bin",
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit("10/minute")
async def post_bin_api(
    request: Request,
    session: SessionDep,
    content: str = Form(),
):
    bin_item = create_bin(
        request=request,
        session=session,
        content=content,
    )

    return {
        "id": bin_item.id,
        "url": f"/bin/{bin_item.id}",
        "raw_url": f"/raw/{bin_item.id}",
        "api_url": f"/api/bin/{bin_item.id}",
        "created_at": bin_item.created_at,
    }


@app.get("/api/bin/{bin_id}")
@limiter.limit("10/minute")
async def get_bin_json(
    request: Request,
    bin_id: int,
    session: SessionDep,
):
    bin_item = session.get(
        BinModel,
        bin_id,
    )

    if not bin_item:
        logger.warning(
            "Bin not found (JSON) - ID: %d, IP: %s",
            bin_id,
            mask_ip(get_real_ip(request)),
        )

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Bin not found",
        )

    logger.info(
        "Bin JSON retrieved - ID: %d, IP: %s",
        bin_id,
        mask_ip(get_real_ip(request)),
    )

    return bin_item


@app.get(
    "/raw/{bin_id}",
    response_class=PlainTextResponse,
)
@limiter.limit("10/minute")
async def get_bin_raw(
    request: Request,
    bin_id: int,
    session: SessionDep,
):
    bin_item = session.get(
        BinModel,
        bin_id,
    )

    if not bin_item:
        logger.warning(
            "Bin not found (RAW) - ID: %d, IP: %s",
            bin_id,
            mask_ip(get_real_ip(request)),
        )

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Bin not found",
        )

    logger.info(
        "Bin RAW retrieved - ID: %d, IP: %s",
        bin_id,
        mask_ip(get_real_ip(request)),
    )

    return bin_item.content


@app.get(
    "/bin/{bin_id}",
    response_class=HTMLResponse,
)
@limiter.limit("60/minute")
async def get_bin(
    request: Request,
    bin_id: int,
    session: SessionDep,
):
    bin_item = session.get(
        BinModel,
        bin_id,
    )

    if not bin_item:
        logger.warning(
            "Bin not found (HTML) - ID: %d, IP: %s",
            bin_id,
            mask_ip(get_real_ip(request)),
        )

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Bin not found",
        )

    logger.info(
        "Bin HTML retrieved - ID: %d, IP: %s",
        bin_id,
        mask_ip(get_real_ip(request)),
    )

    return templates.TemplateResponse(
        request=request,
        name="bin.html",
        context={
            "bin_id": bin_id,
            "bin_content": bin_item.content,
        },
    )


@app.get(
    "/bins",
    response_class=HTMLResponse,
)
@limiter.limit("30/minute")
async def get_bins_list(
    request: Request,
    session: SessionDep,
    page: int = 1,
):
    if page < 1:
        page = 1

    total_count = session.exec(
        select(func.count(BinModel.id))
    ).one()

    total_pages = (
        math.ceil(total_count / PAGE_SIZE)
        or 1
    )

    offset = (
        page - 1
    ) * PAGE_SIZE

    statement = (
        select(BinModel)
        .order_by(BinModel.id.desc())
        .offset(offset)
        .limit(PAGE_SIZE)
    )

    bins = session.exec(
        statement
    ).all()

    logger.info(
        "Bin list retrieved (HTML) - page: %d/%d, IP: %s",
        page,
        total_pages,
        mask_ip(get_real_ip(request)),
    )

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
async def get_bins_json(
    request: Request,
    session: SessionDep,
    page: int = 1,
    page_size: int = 50,
):
    if page < 1:
        page = 1

    page_size = max(
        1,
        min(page_size, 100),
    )

    total_count = session.exec(
        select(func.count(BinModel.id))
    ).one()

    offset = (
        page - 1
    ) * page_size

    statement = (
        select(BinModel)
        .order_by(BinModel.id.desc())
        .offset(offset)
        .limit(page_size)
    )

    bins = session.exec(
        statement
    ).all()

    total_pages = (
        math.ceil(total_count / page_size)
        or 1
    )

    logger.info(
        "Bin list retrieved (JSON) - "
        "page: %d, size: %d, total: %d, IP: %s",
        page,
        page_size,
        total_count,
        mask_ip(get_real_ip(request)),
    )

    return {
        "items": bins,
        "page": page,
        "page_size": page_size,
        "total_count": total_count,
        "total_pages": total_pages,
    }
