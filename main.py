from fastapi import Depends, Form, File, UploadFile, Request, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager
from sqlmodel import Field, Session, SQLModel, create_engine, select
from typing import Annotated

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


SessionDep = Annotated[Session, Depends(get_session)]

@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    yield

app = FastAPI(lifespan=lifespan)

app.mount("/static", StaticFiles(directory="static"), name="static")


templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=HTMLResponse)
async def main(request: Request):
    return templates.TemplateResponse(
        request=request, name="index.html"
    )


@app.post("/bin", response_class=RedirectResponse)
async def post_bin(session: SessionDep, content: str = Form()):
    bin_id = None
    bin_asdf = BinModel(content=content)

    session.add(bin_asdf)
    session.commit()
    session.refresh(bin_asdf)

    bin_id = bin_asdf.id

    return RedirectResponse(f"/bin/{bin_id}", status_code=303)

@app.get("/api/bin/{bin_id}")
async def get_bin_json(bin_id: int, session: SessionDep):
    bin_asdf = session.get(BinModel, bin_id)
    if not bin_asdf:
        raise HTTPException(status_code=404, detail="Bin not found")
    return bin_asdf

@app.get("/bin/{bin_id}", response_class=HTMLResponse)
async def get_bin(request: Request, bin_id: int, session: SessionDep):
    bin_asdf = session.get(BinModel, bin_id)
    if not bin_asdf:
        raise HTTPException(status_code=404, detail="Bin not found")
    return templates.TemplateResponse(request=request, name="bin.html", context={"bin_id":bin_id, "bin_content":bin_asdf.content})