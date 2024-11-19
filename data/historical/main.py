from fastapi import FastAPI
from fastapi import Depends
from sqlalchemy.orm import Session
from . import models, schemas
from .database import SessionLocal

app = FastAPI()


@app.get("/")
async def root():
    return {"message": "Hello World"}


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.post("/items/", response_model=schemas.Item)
def create_item(item: schemas.ItemCreate, db: Session = Depends(get_db)):
    db_item = models.Item(title=item.title, description=item.description)
    db.add(db_item)
    db.commit()
    db.refresh(db_item)
    return db_item

