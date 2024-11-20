from fastapi import APIRouter
from typing import List
from fastapi import Depends
from sqlalchemy.orm import Session

from schemas import items
from data.historical.app.db import database, models

router = APIRouter()

@router.get("/")
async def root():
    return {"message": "Hello World"}


@router.post("/items/", response_model=items.Item)
def create_item(item: items.ItemCreate, db: Session = Depends(database.get_db)):
    db_item = models.Item(title=item.title, description=item.description)
    db.add(db_item)
    db.commit()
    db.refresh(db_item)
    return db_item

@router.get("/items/", response_model=List[items.Item])
def read_items(db: Session = Depends(database.get_db)):
    items = db.query(models.Item).all()
    return items