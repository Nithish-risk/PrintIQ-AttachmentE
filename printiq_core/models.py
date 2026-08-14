from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field
class DataState(str,Enum): NORMAL='NORMAL'; UNKNOWN='UNKNOWN'; MISSING='MISSING'
class WorkflowState(str,Enum): MATCHED='MATCHED'; REVIEW_REQUIRED='REVIEW_REQUIRED'; NOT_APPLICABLE='NOT_APPLICABLE'
class FieldKind(str,Enum): TEXT='TEXT'; DATE='DATE'; CHECKBOX_GROUP='CHECKBOX_GROUP'; STATIC_TEXT='STATIC_TEXT'
class BBox(BaseModel):
    page:int; x0:float=0; y0:float=0; x1:float=0; y1:float=0; normalized:bool=True
    @property
    def cy(self): return (self.y0+self.y1)/2
class CanonicalRule(BaseModel):
    id:str; item_name:str; section:Optional[str]=None; subsection:Optional[str]=None; field_kind:FieldKind=FieldKind.TEXT; document_order_index:int; instruction_original:str=''; instruction_normalized:str=''; expected_options:list[str]=Field(default_factory=list)
class LogicalPdfField(BaseModel):
    id:str; kind:FieldKind; page:int=1; label_text:str=''; value_text:str=''; section:Optional[str]=None; subsection:Optional[str]=None; label_bbox:BBox; document_order_index:Optional[int]=None; raw:dict[str,Any]=Field(default_factory=dict)
class FieldMatch(BaseModel): rule_id:str; field_id:Optional[str]=None; score:float=0; workflow_state:WorkflowState; reasons:list[str]=Field(default_factory=list)
