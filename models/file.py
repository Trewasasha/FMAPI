from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, BigInteger
from sqlalchemy.orm import relationship
from .base import Base
from datetime import datetime

class FileModel(Base):
    __tablename__ = "files"
    
    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String, index=True)  
    path = Column(String)                  
    size = Column(BigInteger) 
    modified = Column(DateTime) 
    hash = Column(String)  # MD5 хэш файла
    owner_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.utcnow)
    
    owner = relationship("User", back_populates="files")