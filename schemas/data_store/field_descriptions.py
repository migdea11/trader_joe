"""Field description text shared by the data_store HTTP contract models.

MOVED DOWN OUT OF routers/data_store/app_endpoints.py (tj-iontkq.3). These strings are rendered
into the OpenAPI document as Field descriptions, which makes them contract surface -- and contract
surface belongs where a client can reach it without importing a FastAPI router. Declared here, both
of this package's consumers import them in the direction the rest of the codebase already runs,
schemas being the tier routers depends on rather than the other way round.

The values are unchanged from their old home, byte for byte: this is a move of definitions, not a
rewrite, so every description already in the published document stays identical.
"""

ASSET_TYPE_DESC = 'Type of financial asset'
SYMBOL_DESC = 'Symbol of the financial asset (aka ticker)'
DATA_TYPE_DESC = 'Type of financial data'
ASSET_DATASET_ID_DESC = 'Unique identifier of the dataset entry.'
ASSET_DATA_ID_DESC = 'Unique identifier of the data entry.'
