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
import math

class BinModel(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    content: str

sqlite_file_name = "database.db"
sqlite_url = f"sqlite:///{sqlite_file_name}"

connect_args = {"check_same_thread": False}
engine = create_engine(sqlite_url, connect_args=connect_args)

def create_db_and_tables():
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session


def get_real_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return get_remote_address(request)

SessionDep = Annotated[Session, Depends(get_session)]

@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    yield

limiter = Limiter(key_func=get_real_ip)

app = FastAPI(lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


app.mount("/static", StaticFiles(directory="static"), name="static")


templates = Jinja2Templates(directory="templates")

MAX_CONTENT_LENGTH = 100_000


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
        raise HTTPException(status_code=400, detail="Content cannot be empty")
        
    if len(clean_content) > MAX_CONTENT_LENGTH:
        raise HTTPException(
            status_code=400, 
            detail=f"Content exceeds maximum limit of {MAX_CONTENT_LENGTH} characters"
        )

    bin_item = BinModel(content=clean_content)
    session.add(bin_item)
    session.commit()
    session.refresh(bin_item)

    return RedirectResponse(f"/bin/{bin_item.id}", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/api/bin/{bin_id}")
@limiter.limit("10/minute")
async def get_bin_json(request: Request, bin_id: int, session: SessionDep):
    bin_asdf = session.get(BinModel, bin_id)
    if not bin_asdf:
        raise HTTPException(status_code=404, detail="Bin not found")
    return bin_asdf

@app.get("/raw/{bin_id}", response_class=PlainTextResponse)
@limiter.limit("10/minute")
async def get_bin_raw(request: Request, bin_id: int, session: SessionDep):
    bin_item = session.get(BinModel, bin_id)
    if not bin_item:
        raise HTTPException(status_code=404, detail="Bin not found")
    return bin_item.content

@app.get("/bin/{bin_id}", response_class=HTMLResponse)
@limiter.limit("60/minute")
async def get_bin(request: Request, bin_id: int, session: SessionDep):
    bin_asdf = session.get(BinModel, bin_id)
    if not bin_asdf:
        raise HTTPException(status_code=404, detail="Bin not found")
    return templates.TemplateResponse(request=request, name="bin.html", context={"bin_id":bin_id, "bin_content":bin_asdf.content})

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
    page_size = min(page_size, 100)

    total_count = session.exec(select(func.count(BinModel.id))).one()
    offset = (page - 1) * page_size

    statement = (
        select(BinModel)
        .order_by(BinModel.id.desc())
        .offset(offset)
        .limit(page_size)
    )
    bins = session.exec(statement).all()

    return {
        "items": bins,
        "page": page,
        "page_size": page_size,
        "total_count": total_count,
        "total_pages": math.ceil(total_count / page_size) or 1,
    }