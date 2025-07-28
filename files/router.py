from fastapi import APIRouter, Request, UploadFile, File, Depends, HTTPException, Query, Form
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Optional
import io
import uuid
import hashlib
import logging
from zipfile import ZipFile

from auth.dependencies import get_current_user, get_current_admin
from config.database import get_db
from models.user import User
from models.file import FileModel
from config.settings import settings

router = APIRouter(prefix="/files", tags=["files"])
logger = logging.getLogger(__name__)

# Константы
STORAGE_DIR = Path(settings.STORAGE_DIR)
ADMIN_ACTIVITY_TIMEOUT = timedelta(minutes=5)  # 5 минут активности админа
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB

def generate_file_id(path: str) -> str:
    """Генерация временного ID для файлов из storage"""
    return f"temp_{hashlib.md5(path.encode()).hexdigest()[:8]}"

def hash_file(file_path: Path) -> str:
    """Вычисляет MD5 хэш файла"""
    hash_md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()

async def is_admin_active(db: AsyncSession) -> bool:
    """Проверяет, активен ли администратор"""
    result = await db.execute(
        select(User)
        .where(User.role == "admin")
        .where(User.last_active >= datetime.utcnow() - ADMIN_ACTIVITY_TIMEOUT)
    )
    return result.scalars().first() is not None

@router.get("/", summary="Получить список файлов")
async def list_files(
    skip: int = Query(0, ge=0),
    limit: int = Query(10, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Возвращает список файлов:
    - Если администратор активен: все файлы из storage
    - Если неактивен: только зарегистрированные файлы из БД
    """
    try:
        admin_active = await is_admin_active(db)
        all_files = []

        # Получаем файлы из storage (если admin активен)
        if admin_active:
            for item in STORAGE_DIR.rglob("*"):
                if item.is_file():
                    try:
                        stat = item.stat()
                        all_files.append({
                            "id": generate_file_id(str(item.relative_to(STORAGE_DIR))),
                            "name": item.name,
                            "path": str(item.relative_to(STORAGE_DIR)),
                            "size": stat.st_size,
                            "modified": datetime.fromtimestamp(stat.st_mtime),
                            "source": "storage"
                        })
                    except Exception as e:
                        logger.error(f"Error processing file {item}: {str(e)}")
                        continue

        # Добавляем файлы из БД
        db_files = (await db.execute(select(FileModel))).scalars().all()
        for file in db_files:
            file_path = STORAGE_DIR / file.path
            if file_path.exists():
                stat = file_path.stat()
                all_files.append({
                    "id": file.id,
                    "name": file.filename,
                    "path": file.path,
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime),
                    "owner_id": file.owner_id,
                    "source": "database"
                })

        # Удаляем дубликаты (если файл есть и в storage и в БД)
        unique_files = {}
        for file in all_files:
            key = file["path"]
            if key not in unique_files or file["source"] == "database":
                unique_files[key] = file

        # Сортировка и пагинация
        sorted_files = sorted(unique_files.values(), 
                            key=lambda x: x["modified"], 
                            reverse=True)
        
        paginated = sorted_files[skip:skip + limit]
        
        return {
            "items": paginated,
            "pagination": {
                "total": len(unique_files),
                "skip": skip,
                "limit": limit,
                "has_more": (skip + limit) < len(unique_files)
            },
            "admin_active": admin_active
        }

    except Exception as e:
        logger.error(f"Failed to list files: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve files")

@router.post("/upload", summary="Загрузить файл")
async def upload_file(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Загружает файл в storage и регистрирует в БД"""
    try:
        # Проверка размера файла
        if file.size > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=413,
                detail=f"File too large. Max size: {MAX_FILE_SIZE/1024/1024}MB"
            )

        STORAGE_DIR.mkdir(exist_ok=True, parents=True)
        
        # Генерация уникального имени
        file_ext = Path(file.filename).suffix
        unique_name = f"{uuid.uuid4()}{file_ext}"
        file_path = STORAGE_DIR / unique_name
        
        # Сохранение файла
        contents = await file.read()
        with open(file_path, "wb") as f:
            f.write(contents)
        
        # Регистрация в БД
        db_file = FileModel(
            filename=file.filename,
            path=unique_name,
            size=len(contents),
            modified=datetime.utcnow(),
            owner_id=user.id,
            hash=hash_file(file_path)
        )
        db.add(db_file)
        await db.commit()
        await db.refresh(db_file)
        
        return {
            "id": db_file.id,
            "filename": file.filename,
            "path": unique_name,
            "size": len(contents),
            "registered": True
        }
        
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Upload failed: {str(e)}")
        raise HTTPException(status_code=500, detail="File upload failed")

@router.get("/download/{file_id}", summary="Скачать файл")
async def download_file(
    file_id: str,  
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Скачивает файл по ID (поддерживает временные ID для storage)"""
    try:
        # 1. Обработка временных файлов (начинаются с 'temp_')
        if file_id.startswith("temp_"):
            file_path = STORAGE_DIR / file_id[5:]  # Убираем 'temp_' префикс
            if not file_path.exists():
                raise HTTPException(status_code=404, detail="File not found in storage")
            
            return FileResponse(
                file_path,
                filename=file_path.name,
                media_type="application/octet-stream"
            )

        # 2. Обработка обычных файлов (числовые ID)
        try:
            file_id_int = int(file_id) 
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Invalid file ID format. Must be integer or start with 'temp_'"
            )

        # 3. Используем явный параметризованный запрос
        stmt = select(FileModel).where(FileModel.id == file_id_int)
        result = await db.execute(stmt)
        file = result.scalars().first()

        if not file:
            raise HTTPException(status_code=404, detail="File not found in database")

        file_path = STORAGE_DIR / file.path
        if not file_path.exists():
            raise HTTPException(
                status_code=404,
                detail="File exists in database but missing in storage"
            )

        return FileResponse(
            file_path,
            filename=file.filename,
            media_type="application/octet-stream"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Download failed: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="File download failed")

@router.get("/download-multiple", summary="Скачать несколько файлов")
async def download_multiple_files(
    file_ids: List[str] = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Скачивает несколько файлов в ZIP-архиве"""
    try:
        zip_buffer = io.BytesIO()
        with ZipFile(zip_buffer, "w") as zip_file:
            for file_id in file_ids:
                if file_id.startswith("temp_"):
                    file_path = STORAGE_DIR / file_id[5:]
                    if file_path.exists():
                        zip_file.write(file_path, file_path.name)
                else:
                    file = await db.get(FileModel, file_id)
                    if file:
                        file_path = STORAGE_DIR / file.path
                        if file_path.exists():
                            zip_file.write(file_path, file.filename)
        
        zip_buffer.seek(0)
        return StreamingResponse(
            zip_buffer,
            media_type="application/zip",
            headers={
                "Content-Disposition": "attachment; filename=files.zip",
                "Content-Length": str(zip_buffer.getbuffer().nbytes)
            }
        )
        
    except Exception as e:
        logger.error(f"Multi-download failed: {str(e)}")
        raise HTTPException(status_code=500, detail="Archive creation failed")

@router.post("/register", summary="Зарегистрировать файл")
async def register_file(
    path: str = Query(...),
    filename: Optional[str] = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Регистрирует существующий файл из storage в БД"""
    try:
        file_path = STORAGE_DIR / path
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="File not found in storage")
        
        existing = await db.execute(
            select(FileModel).where(FileModel.path == path)
        )
        if existing.scalars().first():
            raise HTTPException(status_code=400, detail="File already registered")
        
        stat = file_path.stat()
        db_file = FileModel(
            filename=filename or file_path.name,
            path=path,
            size=stat.st_size,
            modified=datetime.fromtimestamp(stat.st_mtime),
            owner_id=user.id,
            hash=hash_file(file_path)
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
        
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Registration failed: {str(e)}")
        raise HTTPException(status_code=500, detail="File registration failed")

@router.post("/register-all", summary="Зарегистрировать все файлы")
async def register_all_files(
    user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Регистрирует все незарегистрированные файлы из storage (только для admin)"""
    try:
        registered = set(
            (await db.execute(select(FileModel.path))).scalars().all()
        )
        
        new_files = []
        for item in STORAGE_DIR.rglob("*"):
            if item.is_file():
                rel_path = str(item.relative_to(STORAGE_DIR))
                if rel_path not in registered:
                    stat = item.stat()
                    db_file = FileModel(
                        filename=item.name,
                        path=rel_path,
                        size=stat.st_size,
                        modified=datetime.fromtimestamp(stat.st_mtime),
                        owner_id=user.id,
                        hash=hash_file(item)
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
        logger.error(f"Bulk registration failed: {str(e)}")
        raise HTTPException(status_code=500, detail="Bulk registration failed")

@router.post("/admin/sync-file", summary="Синхронизировать один файл")
async def sync_file(
    file: UploadFile = File(...),
    path: str = Form(...),
    hash: str = Form(...),
    size: int = Form(...),
    modified: float = Form(...),
    user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Синхронизирует один файл между storage и БД"""
    try:
        # Проверяем существование файла в БД по пути
        existing = await db.execute(
            select(FileModel).where(FileModel.path == path)
        )
        existing_file = existing.scalars().first()
        
        file_path = STORAGE_DIR / path
        modified_dt = datetime.fromtimestamp(modified)
        
        if existing_file:
            # Файл уже есть в БД - проверяем изменения
            if existing_file.hash != hash or existing_file.size != size:
                # Обновляем файл
                file_path.parent.mkdir(parents=True, exist_ok=True)
                with open(file_path, "wb") as f:
                    f.write(await file.read())
                
                existing_file.size = size
                existing_file.hash = hash
                existing_file.modified = modified_dt
                await db.commit()
                
                return {"status": "updated", "id": existing_file.id}
            else:
                return {"status": "skipped", "id": existing_file.id}
        else:
            # Новый файл - сохраняем и регистрируем
            file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(file_path, "wb") as f:
                f.write(await file.read())
            
            new_file = FileModel(
                filename=file.filename,
                path=path,
                size=size,
                hash=hash,
                modified=modified_dt,
                owner_id=user.id
            )
            db.add(new_file)
            await db.commit()
            await db.refresh(new_file)
            
            return {"status": "added", "id": new_file.id}
            
    except Exception as e:
        await db.rollback()
        logger.error(f"File sync failed: {str(e)}")
        raise HTTPException(status_code=500, detail="File synchronization failed")

@router.post("/admin/cleanup-files", summary="Очистка несуществующих файлов")
async def cleanup_files(
    request: Request,
    storage_path: str,
    user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    print("Received storage_path:", storage_path)  # Логируем входные данные
    if not storage_path:
        raise HTTPException(status_code=422, detail="storage_path is required")
    
    """Удаляет записи о файлах, которых нет в storage"""
    try:
        # Получаем все файлы из БД
        db_files = (await db.execute(select(FileModel))).scalars().all()
        
        deleted = 0
        for file in db_files:
            file_path = STORAGE_DIR / file.path
            if not file_path.exists():
                await db.delete(file)
                deleted += 1
        
        await db.commit()
        return {"deleted": deleted}
        
    except Exception as e:
        await db.rollback()
        logger.error(f"Cleanup failed: {str(e)}")
        raise HTTPException(status_code=500, detail="Files cleanup failed")

@router.post("/admin/import-file", summary="Импорт файла в БД")
async def import_file(
    file: UploadFile = File(...),
    path: str = Form(...),
    user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Импортирует файл в БД (без проверки изменений)"""
    try:
        # Проверяем, не зарегистрирован ли уже файл
        existing = await db.execute(
            select(FileModel).where(FileModel.path == path)
        )
        if existing.scalars().first():
            return {"status": "skipped"}
        
        # Сохраняем файл
        file_path = STORAGE_DIR / path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, "wb") as f:
            f.write(await file.read())
        
        # Регистрируем в БД
        stat = file_path.stat()
        new_file = FileModel(
            filename=file.filename,
            path=path,
            size=stat.st_size,
            modified=datetime.fromtimestamp(stat.st_mtime),
            owner_id=user.id,
            hash=hash_file(file_path)
        )
        db.add(new_file)
        await db.commit()
        await db.refresh(new_file)
        
        return {"status": "imported", "id": new_file.id}
        
    except Exception as e:
        await db.rollback()
        logger.error(f"File import failed: {str(e)}")
        raise HTTPException(status_code=500, detail="File import failed")

@router.get("/admin/file-hashes", summary="Получить хэши файлов")
async def get_file_hashes(
    user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Возвращает словарь {path: hash} для всех файлов в БД"""
    try:
        files = (await db.execute(select(FileModel))).scalars().all()
        return {file.path: file.hash for file in files if file.hash}
    except Exception as e:
        logger.error(f"Failed to get file hashes: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to retrieve file hashes")

@router.get("/admin/storage-stats", summary="Статистика storage")
async def get_storage_stats(
    user: User = Depends(get_current_admin)
):
    """Возвращает статистику по файлам в storage"""
    try:
        total_size = 0
        file_count = 0
        
        for item in STORAGE_DIR.rglob("*"):
            if item.is_file():
                total_size += item.stat().st_size
                file_count += 1
        
        return {
            "total_files": file_count,
            "total_size": total_size,
            "storage_path": str(STORAGE_DIR)
        }
    except Exception as e:
        logger.error(f"Failed to get storage stats: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get storage statistics")