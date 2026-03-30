import os
import lancedb
import json
from lancedb.pydantic import LanceModel, Vector
from pydantic import Field

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "lancedb_data")

class Document(LanceModel):
    id: str = Field(description="Unique string ID for the document chunk")
    content: str = Field(description="Text content or visual description for LLM context")
    vector: Vector(1536) = Field(description="Embedding vector from Gemini")
    source_type: str = Field(description="Type: text, image, video, pdf, docx, etc")
    source_file: str = Field(description="Filename of the original source")
    chunk_index: int = Field(default=-1, description="Index of the chunk in the file")
    metadata: str = Field(default="{}", description="JSON string of additional metadata")

def get_table():
    db = lancedb.connect(DB_PATH)
    if "knowledge_base" not in db.table_names():
        return db.create_table("knowledge_base", schema=Document)
    return db.open_table("knowledge_base")
