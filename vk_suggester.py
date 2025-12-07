"""
VK Suggester - модуль для отправки постов в предложку сообществ ВКонтакте.
"""
import time
import uuid
import re
import logging
from typing import Optional, List, Dict, Any, Callable, Tuple
from dataclasses import dataclass, field
from enum import Enum
import requests

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class PostStatus(Enum):
    """Статусы отправки поста."""
    SUCCESS = "success"
    WALL_DISABLED = "wall_disabled"
    SUGGEST_DISABLED = "suggest_disabled"
    ACCESS_DENIED = "access_denied"
    NOT_MEMBER = "not_member"
    RATE_LIMIT = "rate_limit"
    AUTH_ERROR = "auth_error"
    CAPTCHA = "captcha"
    GROUP_NOT_FOUND = "group_not_found"
    NETWORK_ERROR = "network_error"
    UNKNOWN_ERROR = "unknown_error"


@dataclass
class PostResult:
    """Результат отправки поста в одно сообщество."""
    group_id: int
    group_name: str
    status: PostStatus
    post_id: Optional[int] = None
    error_message: Optional[str] = None
    error_code: Optional[int] = None


@dataclass
class UserInfo:
    """Информация о пользователе токена."""
    user_id: int
    first_name: str
    last_name: str
    
    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


@dataclass 
class GroupInfo:
    """Информация о сообществе."""
    group_id: int
    name: str
    screen_name: str
    can_post: bool = False
    can_suggest: bool = False
    is_closed: int = 0  # 0=открытое, 1=закрытое, 2=приватное
    is_member: bool = False


class VKSuggester:
    """
    Класс для отправки постов в предложку сообществ ВК.
    
    Особенности:
    - Контроль rate limit с паузами между запросами
    - Обработка ошибок VK API
    - Логирование результатов
    - Поддержка вложений (фото)
    """
    
    API_VERSION = "5.131"
    BASE_URL = "https://api.vk.com/method"
    
    # Коды ошибок VK
    ERROR_AUTH = 5
    ERROR_TOO_MANY_REQUESTS_1 = 6
    ERROR_FLOOD = 9
    ERROR_CAPTCHA = 14
    ERROR_ACCESS_DENIED = 15
    ERROR_TOO_MANY_REQUESTS_2 = 29
    ERROR_WALL_ACCESS_DENIED = 30
    ERROR_WALL_DISABLED = 214
    ERROR_GROUP_ACCESS_DENIED = 203
    
    def __init__(
        self,
        access_token: str,
        request_delay: float = 0.5,
        on_log: Optional[Callable[[str, str], None]] = None
    ):
        """
        Инициализация VK Suggester.
        
        Args:
            access_token: Токен пользователя VK
            request_delay: Минимальная пауза между запросами (секунды)
            on_log: Callback для логирования (message, level)
        """
        self.access_token = access_token
        self.request_delay = request_delay
        self.on_log = on_log
        self._last_request_time = 0.0
        self._request_count = 0
        self._session = requests.Session()
        
    def _log(self, message: str, level: str = "info"):
        """Логирование с callback."""
        if self.on_log:
            self.on_log(message, level)
        if level == "error":
            logger.error(message)
        elif level == "warning":
            logger.warning(message)
        else:
            logger.info(message)
    
    def _wait_rate_limit(self):
        """Ожидание для соблюдения rate limit."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self.request_delay:
            sleep_time = self.request_delay - elapsed
            # Добавляем небольшую случайность
            import random
            sleep_time += random.uniform(0.05, 0.15)
            time.sleep(sleep_time)
        self._last_request_time = time.time()
        self._request_count += 1
    
    def _api_request(
        self,
        method: str,
        params: Dict[str, Any],
        retry_count: int = 3
    ) -> Dict[str, Any]:
        """
        Выполнение запроса к VK API.
        
        Args:
            method: Название метода API
            params: Параметры запроса
            retry_count: Количество повторных попыток при rate limit
            
        Returns:
            Ответ API
            
        Raises:
            VKApiError: При ошибке API
        """
        params = {**params, "access_token": self.access_token, "v": self.API_VERSION}
        
        for attempt in range(retry_count):
            self._wait_rate_limit()
            
            try:
                response = self._session.post(
                    f"{self.BASE_URL}/{method}",
                    data=params,
                    timeout=30
                )
                response.raise_for_status()
                data = response.json()
                
                if "error" in data:
                    error = data["error"]
                    error_code = error.get("error_code", 0)
                    error_msg = error.get("error_msg", "Unknown error")
                    
                    # Rate limit - ждём и повторяем
                    if error_code in (self.ERROR_TOO_MANY_REQUESTS_1, 
                                     self.ERROR_TOO_MANY_REQUESTS_2,
                                     self.ERROR_FLOOD):
                        if attempt < retry_count - 1:
                            wait_time = (attempt + 1) * 2
                            self._log(f"Rate limit, ожидание {wait_time}с...", "warning")
                            time.sleep(wait_time)
                            continue
                    
                    raise VKApiError(error_code, error_msg)
                
                return data.get("response", {})
                
            except requests.RequestException as e:
                if attempt < retry_count - 1:
                    self._log(f"Сетевая ошибка, повтор через 2с: {e}", "warning")
                    time.sleep(2)
                    continue
                raise VKApiError(-1, f"Сетевая ошибка: {e}")
        
        raise VKApiError(-1, "Превышено количество попыток")
    
    def get_user_info(self) -> UserInfo:
        """Получение информации о владельце токена."""
        try:
            response = self._api_request("users.get", {})
            if response and len(response) > 0:
                user = response[0]
                return UserInfo(
                    user_id=user["id"],
                    first_name=user.get("first_name", ""),
                    last_name=user.get("last_name", "")
                )
        except VKApiError as e:
            self._log(f"Ошибка получения информации о пользователе: {e}", "error")
            raise
        raise VKApiError(-1, "Не удалось получить информацию о пользователе")
    
    def resolve_group_ids(self, group_identifiers: List[str]) -> Dict[str, int]:
        """
        Резолвинг идентификаторов групп в числовые ID.
        
        Args:
            group_identifiers: Список screen_name или числовых ID групп
            
        Returns:
            Словарь {identifier: group_id}
        """
        result = {}
        to_resolve = []
        
        for identifier in group_identifiers:
            identifier = identifier.strip()
            if not identifier:
                continue
                
            # Убираем возможные префиксы URL
            identifier = self._clean_group_identifier(identifier)
            
            # Если это число - сразу добавляем
            try:
                gid = int(identifier)
                result[identifier] = abs(gid)
            except ValueError:
                to_resolve.append(identifier)
        
        # Резолвим screen names батчами по 25
        for i in range(0, len(to_resolve), 25):
            batch = to_resolve[i:i+25]
            try:
                response = self._api_request("groups.getById", {
                    "group_ids": ",".join(batch)
                })
                # VK API v5.131+ возвращает {"groups": [...]}
                groups = response.get("groups", response) if isinstance(response, dict) else response
                if isinstance(groups, list):
                    for group in groups:
                        screen_name = group.get("screen_name", "")
                        for orig in batch:
                            if orig.lower() == screen_name.lower() or str(group["id"]) == orig:
                                result[orig] = group["id"]
                                break
            except VKApiError as e:
                self._log(f"Ошибка резолва групп {batch}: {e}", "warning")
                
        return result
    
    def _clean_group_identifier(self, identifier: str) -> str:
        """Очистка идентификатора группы от URL и лишних символов."""
        # Убираем URL часть
        patterns = [
            r"https?://vk\.com/",
            r"https?://m\.vk\.com/",
            r"vk\.com/",
            r"^@",
            r"^public",
            r"^club"
        ]
        for pattern in patterns:
            identifier = re.sub(pattern, "", identifier, flags=re.IGNORECASE)
        return identifier.strip()
    
    def get_groups_info(self, group_ids: List[int]) -> Dict[int, GroupInfo]:
        """
        Получение информации о группах.
        
        Args:
            group_ids: Список числовых ID групп
            
        Returns:
            Словарь {group_id: GroupInfo}
        """
        result = {}
        
        # Запрашиваем батчами по 500
        for i in range(0, len(group_ids), 500):
            batch = group_ids[i:i+500]
            try:
                response = self._api_request("groups.getById", {
                    "group_ids": ",".join(map(str, batch)),
                    "fields": "can_post,can_suggest,is_closed,is_member,wall"
                })
                groups = response.get("groups", response) if isinstance(response, dict) else response
                if isinstance(groups, list):
                    for group in groups:
                        gid = group["id"]
                        result[gid] = GroupInfo(
                            group_id=gid,
                            name=group.get("name", f"Группа {gid}"),
                            screen_name=group.get("screen_name", str(gid)),
                            can_post=group.get("can_post", 0) == 1,
                            can_suggest=group.get("can_suggest", 0) == 1,
                            is_closed=group.get("is_closed", 0),
                            is_member=group.get("is_member", 0) == 1
                        )
            except VKApiError as e:
                self._log(f"Ошибка получения информации о группах: {e}", "warning")
                # Создаём заглушки для групп
                for gid in batch:
                    if gid not in result:
                        result[gid] = GroupInfo(
                            group_id=gid,
                            name=f"Группа {gid}",
                            screen_name=str(gid)
                        )
        
        return result
    
    def upload_photo(self, photo_data: bytes, filename: str = "photo.jpg") -> Optional[str]:
        """
        Загрузка фото для поста.
        
        Args:
            photo_data: Бинарные данные фото
            filename: Имя файла
            
        Returns:
            Строка вложения формата photo{owner_id}_{id} или None при ошибке
        """
        try:
            # Получаем URL для загрузки (без group_id - на стену пользователя)
            upload_server = self._api_request("photos.getWallUploadServer", {})
            upload_url = upload_server.get("upload_url")
            
            if not upload_url:
                self._log("Не удалось получить URL для загрузки фото", "error")
                return None
            
            # Загружаем фото
            self._wait_rate_limit()
            response = self._session.post(
                upload_url,
                files={"photo": (filename, photo_data)},
                timeout=60
            )
            response.raise_for_status()
            upload_result = response.json()
            
            if not upload_result.get("photo") or upload_result.get("photo") == "[]":
                self._log("Не удалось загрузить фото на сервер", "error")
                return None
            
            # Сохраняем фото
            save_result = self._api_request("photos.saveWallPhoto", {
                "photo": upload_result["photo"],
                "server": upload_result["server"],
                "hash": upload_result["hash"]
            })
            
            if save_result and len(save_result) > 0:
                photo = save_result[0]
                owner_id = photo["owner_id"]
                photo_id = photo["id"]
                access_key = photo.get("access_key", "")
                
                if access_key:
                    return f"photo{owner_id}_{photo_id}_{access_key}"
                return f"photo{owner_id}_{photo_id}"
            
        except Exception as e:
            self._log(f"Ошибка загрузки фото: {e}", "error")
        
        return None
    
    def join_group(self, group_id: int) -> bool:
        """
        Подписка на сообщество.
        
        Args:
            group_id: ID группы (положительное число)
            
        Returns:
            True если подписка успешна, False при ошибке
        """
        try:
            response = self._api_request("groups.join", {
                "group_id": group_id
            })
            # Успешный ответ: {"response": 1}
            return response == 1
        except VKApiError as e:
            self._log(f"Ошибка подписки на группу {group_id}: {e.message}", "warning")
            raise
    
    def delete_post(self, group_id: int, post_id: int) -> bool:
        """
        Удаление поста/предложки со стены сообщества.
        
        Args:
            group_id: ID группы (положительное число)
            post_id: ID поста
            
        Returns:
            True если удаление успешно
        """
        try:
            response = self._api_request("wall.delete", {
                "owner_id": -group_id,
                "post_id": post_id
            })
            # Успешный ответ: {"response": 1}
            return response == 1
        except VKApiError as e:
            self._log(f"Ошибка удаления поста {post_id} в группе {group_id}: {e.message}", "warning")
            raise

    def post_to_suggestion(
        self,
        group_id: int,
        group_name: str,
        message: str,
        attachments: Optional[str] = None
    ) -> PostResult:
        """
        Отправка поста в предложку одного сообщества.
        
        Args:
            group_id: ID группы (положительное число)
            group_name: Название группы для логов
            message: Текст поста
            attachments: Строка вложений через запятую
            
        Returns:
            PostResult с результатом отправки
        """
        guid = str(uuid.uuid4())
        
        params = {
            "owner_id": -group_id,  # Отрицательный для групп
            "message": message,
            "from_group": 0,  # От имени пользователя -> в предложку
            "guid": guid
        }
        
        if attachments:
            params["attachments"] = attachments
        
        try:
            response = self._api_request("wall.post", params)
            post_id = response.get("post_id")
            
            if post_id:
                return PostResult(
                    group_id=group_id,
                    group_name=group_name,
                    status=PostStatus.SUCCESS,
                    post_id=post_id
                )
            else:
                return PostResult(
                    group_id=group_id,
                    group_name=group_name,
                    status=PostStatus.UNKNOWN_ERROR,
                    error_message="Не получен post_id"
                )
                
        except VKApiError as e:
            status = self._classify_error(e.code)
            return PostResult(
                group_id=group_id,
                group_name=group_name,
                status=status,
                error_code=e.code,
                error_message=e.message
            )
    
    def _classify_error(self, error_code: int) -> PostStatus:
        """Классификация ошибки VK по коду."""
        mapping = {
            self.ERROR_AUTH: PostStatus.AUTH_ERROR,
            self.ERROR_TOO_MANY_REQUESTS_1: PostStatus.RATE_LIMIT,
            self.ERROR_FLOOD: PostStatus.RATE_LIMIT,
            self.ERROR_CAPTCHA: PostStatus.CAPTCHA,
            self.ERROR_ACCESS_DENIED: PostStatus.ACCESS_DENIED,
            self.ERROR_TOO_MANY_REQUESTS_2: PostStatus.RATE_LIMIT,
            self.ERROR_WALL_ACCESS_DENIED: PostStatus.ACCESS_DENIED,
            self.ERROR_WALL_DISABLED: PostStatus.WALL_DISABLED,
            self.ERROR_GROUP_ACCESS_DENIED: PostStatus.GROUP_NOT_FOUND,
        }
        return mapping.get(error_code, PostStatus.UNKNOWN_ERROR)
    
    def process_groups(
        self,
        group_identifiers: List[str],
        message: str,
        attachments: Optional[str] = None,
        on_progress: Optional[Callable[[int, int, PostResult], None]] = None,
        stop_on_auth_error: bool = True
    ) -> List[PostResult]:
        """
        Массовая отправка в предложку списка групп.
        
        Args:
            group_identifiers: Список идентификаторов групп
            message: Текст поста
            attachments: Строка вложений
            on_progress: Callback (current, total, result)
            stop_on_auth_error: Останавливаться при ошибке авторизации
            
        Returns:
            Список результатов
        """
        results = []
        
        # 1. Резолвим ID групп
        self._log(f"Резолвинг {len(group_identifiers)} групп...")
        resolved = self.resolve_group_ids(group_identifiers)
        
        if not resolved:
            self._log("Не удалось получить ID ни одной группы", "error")
            return results
        
        self._log(f"Найдено {len(resolved)} групп")
        
        # 2. Получаем информацию о группах
        group_ids = list(resolved.values())
        self._log("Получение информации о группах...")
        groups_info = self.get_groups_info(group_ids)
        
        # 3. Отправляем посты
        total = len(group_ids)
        for i, (identifier, gid) in enumerate(resolved.items()):
            info = groups_info.get(gid)
            group_name = info.name if info else f"Группа {gid}"
            
            # Проверяем доступность предложки
            if info and not info.can_suggest and not info.can_post:
                result = PostResult(
                    group_id=gid,
                    group_name=group_name,
                    status=PostStatus.SUGGEST_DISABLED,
                    error_message="Предложка/стена закрыта"
                )
                self._log(f"[{i+1}/{total}] {group_name}: предложка закрыта", "warning")
            else:
                self._log(f"[{i+1}/{total}] Отправка в {group_name}...")
                result = self.post_to_suggestion(gid, group_name, message, attachments)
                
                if result.status == PostStatus.SUCCESS:
                    self._log(f"[{i+1}/{total}] {group_name}: ✓ успешно (post_id={result.post_id})")
                else:
                    msg = result.error_message or result.status.value
                    self._log(f"[{i+1}/{total}] {group_name}: ✗ {msg}", "warning")
                
                # Прерываем при ошибке авторизации
                if stop_on_auth_error and result.status == PostStatus.AUTH_ERROR:
                    self._log("Ошибка авторизации! Требуется новый токен.", "error")
                    results.append(result)
                    break
            
            results.append(result)
            
            if on_progress:
                on_progress(i + 1, total, result)
        
        return results
    
    def get_results_summary(self, results: List[PostResult]) -> Dict[str, Any]:
        """Получение сводки по результатам."""
        summary = {
            "total": len(results),
            "success": 0,
            "failed": 0,
            "by_status": {}
        }
        
        for result in results:
            status_name = result.status.value
            if status_name not in summary["by_status"]:
                summary["by_status"][status_name] = 0
            summary["by_status"][status_name] += 1
            
            if result.status == PostStatus.SUCCESS:
                summary["success"] += 1
            else:
                summary["failed"] += 1
        
        return summary


class VKApiError(Exception):
    """Ошибка VK API."""
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


def generate_oauth_url(client_id: int) -> str:
    """Генерация URL для получения токена."""
    return (
        f"https://oauth.vk.com/authorize"
        f"?client_id={client_id}"
        f"&display=page"
        f"&scope=wall,photos,docs,offline,groups"
        f"&redirect_uri=https://oauth.vk.com/blank.html"
        f"&response_type=token"
        f"&v=5.131"
        f"&state=vk_suggester"
    )
