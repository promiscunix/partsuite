from typing import Generator

from sqlmodel import SQLModel, create_engine, Session

# For now we'll use a local SQLite file. Easy to swap later.
DATABASE_URL = "sqlite:///./invoice_app.db"

engine = create_engine(DATABASE_URL, echo=True)


def init_db() -> None:
    """Create all tables if they don't exist."""
    SQLModel.metadata.create_all(engine)


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a DB session."""
    with Session(engine) as session:
        yield session

