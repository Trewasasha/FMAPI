from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from datetime import datetime, timedelta
from pydantic import BaseModel
from jose import JWTError, jwt
from models.user import User
from models.token import Token
from config.database import get_db
from .utils import verify_password, create_access_token, get_password_hash
from config.settings import settings

router = APIRouter(prefix="/auth", tags=["auth"])

# Схема аутентификации OAuth2
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/token")

# Модели запросов
class UserCreate(BaseModel):
    """Модель для регистрации пользователя"""
    username: str
    password: str
    role: str = "user"  # По умолчанию роль 'user'
    admin_secret: str = None  # Секретный ключ для создания админа

class ChangeRoleRequest(BaseModel):
    """Модель для изменения роли"""
    username: str
    new_role: str

# Вспомогательные функции
async def get_user(db: AsyncSession, username: str) -> User:
    """Получает пользователя из БД по имени"""
    result = await db.execute(select(User).where(User.username == username))
    return result.scalars().first()

async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db)
) -> User:
    """Проверяет токен и возвращает текущего пользователя"""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Не удалось подтвердить учетные данные",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    
    user = await get_user(db, username)
    if user is None:
        raise credentials_exception
    return user

async def get_current_admin(
    current_user: User = Depends(get_current_user)
) -> User:
    """Проверяет, что пользователь является администратором"""
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Требуются права администратора"
        )
    return current_user

# Эндпоинты
@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    user_data: UserCreate,
    db: AsyncSession = Depends(get_db)
):
    """Регистрация нового пользователя"""
    # Проверяем, существует ли пользователь
    existing_user = await get_user(db, user_data.username)
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Имя пользователя уже занято"
        )
    
    # Проверяем корректность роли
    if user_data.role not in ["user", "admin"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Недопустимая роль пользователя"
        )
    
    # Для создания администратора проверяем секретный ключ
    if user_data.role == "admin":
        if user_data.admin_secret != settings.ADMIN_SECRET_KEY:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Неверный секретный ключ администратора"
            )
    
    # Создаем нового пользователя
    hashed_password = get_password_hash(user_data.password)
    new_user = User(
        username=user_data.username,
        hashed_password=hashed_password,
        role=user_data.role,
        last_active=datetime.utcnow() if user_data.role == "admin" else None
    )
    
    db.add(new_user)
    await db.commit()
    
    return {"message": "Пользователь успешно создан"}

@router.post("/token", response_model=Token)
async def login_for_access_token(
    form_data: OAuth2PasswordRequestForm = Depends(),
    request: Request = None,
    db: AsyncSession = Depends(get_db)
):
    """Аутентификация и получение токена"""
    user = await get_user(db, form_data.username)
    
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверные учетные данные",
        )
    
    # Обновляем время последней активности
    user.last_active = datetime.utcnow()
    await db.commit()
    
    # Создаем JWT токен
    access_token = create_access_token(
        data={"sub": user.username, "role": user.role},
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "role": user.role
    }

@router.post("/change-role", status_code=status.HTTP_200_OK)
async def change_user_role(
    role_data: ChangeRoleRequest,
    current_user: User = Depends(get_current_admin),  # Только для админов
    db: AsyncSession = Depends(get_db)
):
    """Изменение роли пользователя (только для администраторов)"""
    user = await get_user(db, role_data.username)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Пользователь не найден"
        )
    
    if role_data.new_role not in ["user", "admin"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Недопустимая роль пользователя"
        )
    
    user.role = role_data.new_role
    await db.commit()
    
    return {"message": f"Роль пользователя {role_data.username} изменена на {role_data.new_role}"}