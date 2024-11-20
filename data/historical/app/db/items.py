from sqlalchemy.orm import Session

from schemas import items
import models

def create_item(item: items.ItemCreate, db: Session):
    db_item = models.Item(title=item.title, description=item.description)
    db.add(db_item)
    db.commit()
    db.refresh(db_item)
    return db_item

def read_items(db: Session):
    items = db.query(models.Item).all()
    return items