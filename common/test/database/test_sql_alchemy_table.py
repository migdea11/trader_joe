import pytest
from pydantic import BaseModel
from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import declarative_base

from common.database.sql_alchemy_table import CustomTypeTable
from common.database.sql_alchemy_types import BaseCustomSqlType, CustomColumn

Base = declarative_base()


class MockCustomType(BaseCustomSqlType[str, String]):
    def __init__(self):
        BaseCustomSqlType.__init__(self, str, String)

    def get_type(self) -> str:
        return self._model_type

    def validate_column_params(self, **kwargs) -> bool:
        return True

    def to_model_type(self, value: str) -> str:
        return f"model_{value}"

    def to_schema_type(self, value: int) -> str:
        return f"schema_{value}"

# Define a test table inheriting from CustomTypeTable
class StubModel(Base, CustomTypeTable):
    __tablename__ = "test_table"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    custom_field = CustomColumn(MockCustomType, nullable=False)

# Define a test Pydantic schema
class StubSchema(BaseModel):
    id: int
    name: str
    custom_field: str

@pytest.fixture
def test_model():
    return StubModel(id=1, name="test", custom_field="custom_value")

@pytest.fixture
def test_schema():
    return StubSchema(id=1, name="test", custom_field="schema_custom_value")

# Test _get_columns method
def test_get_columns(test_model: StubModel):
    other_columns, custom_columns = test_model._get_columns()
    print(other_columns, custom_columns)
    assert "id" in other_columns
    assert "name" in other_columns
    assert "custom_field" in custom_columns
    assert isinstance(custom_columns["custom_field"], CustomColumn)

# Test to_schema method
def test_to_schema(test_model: StubModel):
    schema_data = test_model.to_schema(StubSchema)
    assert schema_data["id"] == 1
    assert schema_data["name"] == "test"
    assert schema_data["custom_field"] == "schema_custom_value"

# Test to_validated_schema method
def test_to_validated_schema(test_model: StubModel):
    validated_schema = test_model.to_validated_schema(StubSchema)
    assert validated_schema.id == 1
    assert validated_schema.name == "test"
    assert validated_schema.custom_field == "schema_custom_value"

# Test get_fields method
def test_get_fields(test_schema: StubSchema):
    fields = StubModel.get_fields(test_schema)
    assert fields["id"] == 1
    assert fields["name"] == "test"
    assert fields["custom_field"] == "model_schema_custom_value"

# Test get_model method
def test_get_model(test_schema: StubSchema):
    model_instance = StubModel.get_model(test_schema)
    assert model_instance.id == 1
    assert model_instance.name == "test"
    assert model_instance.custom_field == "model_schema_custom_value"
