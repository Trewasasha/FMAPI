from fastapi import APIRouter, UploadFile, File, Depends, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func
from pathlib import Path
from datetime import datetime, timedelta
from typing import List
from fastapi import Response
from zipfile import ZipFile
import io
import os
import uuid
import hashlib
from typing import List, Dict, Union
from fastapi_cache.decorator import cache
import logging

from auth.dependencies import get_current_user
from config.database import get_db
from models.user import User
from models.file import FileModel
from config.settings import settings

router = APIRouter(prefix="/files", tags=["files"])
logger = logging.getLogger(__name__)

# Настройки
CACHE_EXPIRE = timedelta(minutes=5)  # Время жизни кеша
STORAGE_DIR = Path(settings.STORAGE_DIR)

def generate_temp_id(path: str) -> str:
    """Генерирует временный ID на основе хеша пути"""
    return f"temp_{hashlib.md5(path.encode()).hexdigest()[:8]}"

@router.get("/")
@cache(expire=CACHE_EXPIRE, namespace="files_list")
async def list_all_files(
    skip: int = Query(0, ge=0, description="Смещение"),
    limit: int = Query(10, le=100, description="Лимит"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Получить все файлы (из storage и БД) с пагинацией"""
    try:
        # 1. Получаем файлы из БД
        db_files = (await db.execute(select(FileModel))).scalars().all()
        db_files_map = {f.path: f for f in db_files}
        
        # 2. Сканируем storage
        STORAGE_DIR.mkdir(exist_ok=True, parents=True)
        all_files = []
        
        for item in STORAGE_DIR.rglob("*"):
            if item.is_file():
                rel_path = str(item.relative_to(STORAGE_DIR))
                try:
                    stat = item.stat()
                    modified = datetime.fromtimestamp(stat.st_mtime).isoformat()
                    created_at = datetime.fromtimestamp(stat.st_ctime).isoformat()
                except Exception as e:
                    logger.warning(f"Could not get file stats for {rel_path}: {str(e)}")
                    modified = created_at = "unknown"
                
                file_info = {
                    "id": generate_temp_id(rel_path),
                    "name": item.name,
                    "filename": item.name,
                    "path": rel_path,
                    "size": stat.st_size if 'stat' in locals() else 0,
                    "modified": modified,
                    "created_at": created_at,
                    "owner_id": None,
                    "registered": False,
                    "type": "file"
                }
                
                # Добавляем данные из БД если есть
                db_file = db_files_map.get(rel_path)
                if db_file:
                    try:
                        file_info.update({
                            "id": db_file.id,
                            "filename": db_file.filename or item.name,
                            "owner_id": db_file.owner_id,
                            "created_at": db_file.created_at.isoformat() if db_file.created_at else created_at,
                            "registered": True
                        })
                    except Exception as e:
                        logger.error(f"Error processing DB file {rel_path}: {str(e)}")
                        continue
                
                all_files.append(file_info)
        
        # 3. Сортировка по дате создания (новые сначала)
        all_files.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        
        # 4. Пагинация
        total = len(all_files)
        paginated = all_files[skip:skip + limit]
        
        return {
            "items": paginated,
            "pagination": {
                "total": total,
                "skip": skip,
                "limit": limit,
                "has_more": (skip + limit) < total
            }
        }
        
    except Exception as e:
        logger.error(f"List files error: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Загрузить новый файл"""
    try:
        STORAGE_DIR.mkdir(exist_ok=True, parents=True)
        
        # Генерируем уникальное имя
        file_ext = Path(file.filename).suffix
        unique_name = f"{uuid.uuid4()}{file_ext}"
        file_path = STORAGE_DIR / unique_name
        
        # Сохраняем файл
        content = await file.read()
        with open(file_path, "wb") as buffer:
            buffer.write(content)
        
        # Регистрируем в БД
        db_file = FileModel(
            filename=file.filename,
            path=unique_name,
            owner_id=user.id
        )
        db.add(db_file)
        await db.commit()
        await db.refresh(db_file)
        
        return {
            "id": db_file.id,
            "filename": file.filename,
            "path": unique_name,
            "size": len(content),
            "registered": True
        }
        
    except Exception as e:
        await db.rollback()
        logger.error(f"Upload error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/download/{file_id}")
async def download_file(
    file_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Скачать файл по ID"""
    try:
        # Ищем только зарегистрированные файлы
        file = await db.get(FileModel, file_id)
        if not file or file.owner_id != user.id:
            raise HTTPException(status_code=404, detail="File not found")
        
        file_path = STORAGE_DIR / file.path
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="File missing in storage")
        
        return FileResponse(
            file_path,
            filename=file.filename,
            media_type="application/octet-stream"
        )
        
    except Exception as e:
        logger.error(f"Download error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/info/{file_id}")
async def get_file_info(
    file_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Информация о файле по ID"""
    try:
        file = await db.get(FileModel, file_id)
        if not file or file.owner_id != user.id:
            raise HTTPException(status_code=404, detail="File not found")
        
        file_path = STORAGE_DIR / file.path
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="File missing in storage")
        
        stat = file_path.stat()
        return {
            "id": file.id,
            "filename": file.filename,
            "path": file.path,
            "size": stat.st_size,
            "created_at": file.created_at.isoformat(),
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "owner_id": file.owner_id,
            "registered": True
        }
        
    except Exception as e:
        logger.error(f"File info error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/register")
async def register_existing_file(
    path: str = Query(..., description="Относительный путь"),
    filename: str = Query(None, description="Имя файла"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Зарегистрировать существующий файл"""
    try:
        file_path = STORAGE_DIR / path
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="File not found in storage")
        
        # Проверяем дубликаты
        existing = await db.execute(
            select(FileModel).where(FileModel.path == path)
        )
        if existing.scalar():
            raise HTTPException(status_code=400, detail="File already registered")
        
        # Создаем запись
        db_file = FileModel(
            filename=filename or file_path.name,
            path=path,
            owner_id=user.id
        )
        db.add(db_file)
        await db.commit()
        await db.refresh(db_file)
        
        return {
            "id": db_file.id,
            "filename": db_file.filename,
            "path": path,
            "registered": True
        }
        
    except Exception as e:
        await db.rollback()
        logger.error(f"Registration error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/register-all")
async def register_all_unregistered(

    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Зарегистрировать все незарегистрированные файлы"""
    try:
        # Получаем все зарегистрированные пути
        registered = set(
            (await db.execute(select(FileModel.path))).scalars().all()
        )
        
        new_files = []
        for item in STORAGE_DIR.rglob("*"):
            if item.is_file():
                rel_path = str(item.relative_to(STORAGE_DIR))
                if rel_path not in registered:
                    db_file = FileModel(
                        filename=item.name,
                        path=rel_path,
                        owner_id=user.id
                    )
                    db.add(db_file)
                    new_files.append({
                        "path": rel_path,
                        "id": db_file.id
                    })
        
        await db.commit()
        return {
            "message": f"Registered {len(new_files)} new files",
            "files": new_files
        }
        
    except Exception as e:
        await db.rollback()
        logger.error(f"Mass registration error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
    

@router.post("/upload-multiple")
async def upload_multiple_files(
    files: List[UploadFile] = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Загрузить несколько файлов одновременно"""
    try:
        STORAGE_DIR.mkdir(exist_ok=True, parents=True)
        uploaded_files = []
        
        for file in files:
            # Генерируем уникальное имя для каждого файла
            file_ext = Path(file.filename).suffix
            unique_name = f"{uuid.uuid4()}{file_ext}"
            file_path = STORAGE_DIR / unique_name
            
            # Сохраняем файл
            content = await file.read()
            with open(file_path, "wb") as buffer:
                buffer.write(content)
            
            # Регистрируем в БД
            db_file = FileModel(
                filename=file.filename,
                path=unique_name,
                owner_id=user.id
            )
            db.add(db_file)
            await db.commit()
            await db.refresh(db_file)
            
            uploaded_files.append({
                "id": db_file.id,
                "filename": file.filename,
                "path": unique_name,
                "size": len(content),
                "registered": True
            })
        
        return {"files": uploaded_files}
        
    except Exception as e:
        await db.rollback()
        logger.error(f"Multiple upload error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/download-multiple")
async def download_multiple_files(
    file_ids: List[int] = Query(..., description="Список ID файлов"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Скачать несколько файлов в виде ZIP-архива"""
    try:
        # Проверяем права на все файлы
        files = []
        for file_id in file_ids:
            file = await db.get(FileModel, file_id)
            if not file or file.owner_id != user.id:
                raise HTTPException(
                    status_code=404, 
                    detail=f"File with ID {file_id} not found or access denied"
                )
            file_path = STORAGE_DIR / file.path
            if not file_path.exists():
                raise HTTPException(
                    status_code=404,
                    detail=f"File with ID {file_id} missing in storage"
                )
            files.append((file, file_path))
        
        # Создаем ZIP-архив в памяти
        zip_buffer = io.BytesIO()
        with ZipFile(zip_buffer, "w") as zip_file:
            for file, file_path in files:
                zip_file.write(file_path, file.filename)
        
        zip_buffer.seek(0)
        
        # Возвращаем архив как ответ
        return Response(
            zip_buffer.getvalue(),
            media_type="application/zip",
            headers={
                "Content-Disposition": "attachment; filename=files.zip",
                "Content-Length": str(zip_buffer.getbuffer().nbytes)
            }
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Multiple download error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))