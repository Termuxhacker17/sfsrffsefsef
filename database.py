import sqlite3
from typing import List, Optional
from datetime import datetime, timedelta

DB_NAME = "users.db"


# ===========================================================================
# Инициализация БД
# ===========================================================================

def init_db():
    """Создаёт все необходимые таблицы и выполняет миграции для существующей БД."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    # --- Таблица пользователей ---
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id                INTEGER   PRIMARY KEY,
            notifications_enabled  INTEGER   DEFAULT 1,
            joined_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            support_mode           INTEGER   DEFAULT 0,
            last_ticket_closed_at  TIMESTAMP
        )
    """)
    _safe_add_column(c, "users", "support_mode",          "INTEGER DEFAULT 0")
    _safe_add_column(c, "users", "last_ticket_closed_at", "TIMESTAMP")

    # --- Таблица тикетов ---
    c.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id         INTEGER   PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER   NOT NULL,
            subject    TEXT      DEFAULT '',
            username   TEXT      DEFAULT '',
            status     TEXT      DEFAULT 'open',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            closed_at  TIMESTAMP,
            closed_by  TEXT,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    """)
    # Миграция: добавляем subject и username если их нет
    _safe_add_column(c, "tickets", "subject",  "TEXT DEFAULT ''")
    _safe_add_column(c, "tickets", "username", "TEXT DEFAULT ''")

    # --- Таблица сообщений тикета ---
    c.execute("""
        CREATE TABLE IF NOT EXISTS ticket_messages (
            id          INTEGER   PRIMARY KEY AUTOINCREMENT,
            ticket_id   INTEGER   NOT NULL,
            sender_type TEXT      NOT NULL,
            text        TEXT      NOT NULL,
            sent_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (ticket_id) REFERENCES tickets(id)
        )
    """)

    conn.commit()
    conn.close()


def _safe_add_column(cursor, table: str, column: str, definition: str):
    """Добавляет колонку в таблицу, если она ещё не существует."""
    try:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    except sqlite3.OperationalError:
        pass  # Колонка уже есть


def _ticket_row_to_dict(row) -> dict:
    """
    Преобразует строку из таблицы tickets (8 колонок) в словарь.
    Порядок колонок: id, user_id, subject, username, status,
                     created_at, closed_at, closed_by
    """
    return {
        "id":         row[0],
        "user_id":    row[1],
        "subject":    row[2] or "",
        "username":   row[3] or "",
        "status":     row[4],
        "created_at": row[5],
        "closed_at":  row[6],
        "closed_by":  row[7],
    }


# ===========================================================================
# Пользователи
# ===========================================================================

def add_user(user_id: int):
    """Добавляет нового пользователя в БД."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()


def toggle_notifications(user_id: int) -> bool:
    """Переключает статус уведомлений и возвращает новое состояние."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT notifications_enabled FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    if row is None:
        add_user(user_id)
        new_state = 0
    else:
        new_state = 0 if row[0] == 1 else 1
    c.execute("UPDATE users SET notifications_enabled=? WHERE user_id=?", (new_state, user_id))
    conn.commit()
    conn.close()
    return bool(new_state)


def get_notification_status(user_id: int) -> bool:
    """Возвращает статус уведомлений пользователя."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT notifications_enabled FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return bool(row[0]) if row else True


def get_all_users() -> List[int]:
    """Возвращает список ID всех пользователей."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    rows = c.fetchall()
    conn.close()
    return [row[0] for row in rows]


def get_user_count() -> int:
    """Возвращает количество пользователей."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    count = c.fetchone()[0]
    conn.close()
    return count


# ===========================================================================
# Режим поддержки
# 0 — обычный режим
# 1 — ожидаем ввод темы (новый тикет, шаг 1)
# 2 — ожидаем ввод сообщения (новый тикет, шаг 2; тема в user_data)
# 3 — ожидаем ответ в открытый тикет
# ===========================================================================

def set_support_mode(user_id: int, mode: int):
    """Устанавливает режим поддержки для пользователя."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE users SET support_mode=? WHERE user_id=?", (mode, user_id))
    conn.commit()
    conn.close()


def get_support_mode(user_id: int) -> int:
    """Возвращает текущий режим поддержки пользователя (0/1/2/3)."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT support_mode FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0


# ===========================================================================
# Тикеты — создание и базовые операции
# ===========================================================================

def create_ticket(user_id: int, subject: str = "", username: str = "") -> int:
    """Создаёт новый тикет и возвращает его ID."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "INSERT INTO tickets (user_id, subject, username, status) VALUES (?, ?, ?, 'open')",
        (user_id, subject.strip(), username)
    )
    ticket_id = c.lastrowid
    conn.commit()
    conn.close()
    return ticket_id


def get_open_ticket(user_id: int) -> Optional[dict]:
    """Возвращает открытый тикет пользователя или None."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "SELECT id, user_id, subject, username, status, created_at, closed_at, closed_by "
        "FROM tickets WHERE user_id=? AND status='open' "
        "ORDER BY created_at DESC LIMIT 1",
        (user_id,)
    )
    row = c.fetchone()
    conn.close()
    return _ticket_row_to_dict(row) if row else None


def get_ticket_by_id(ticket_id: int) -> Optional[dict]:
    """Возвращает тикет по его ID или None."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "SELECT id, user_id, subject, username, status, created_at, closed_at, closed_by "
        "FROM tickets WHERE id=?",
        (ticket_id,)
    )
    row = c.fetchone()
    conn.close()
    return _ticket_row_to_dict(row) if row else None


def close_ticket(ticket_id: int, closed_by: str):
    """
    Закрывает тикет.
    closed_by: 'user' — закрыл пользователь, 'admin' — закрыл администратор.
    """
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "UPDATE tickets SET status='closed', closed_at=CURRENT_TIMESTAMP, closed_by=? "
        "WHERE id=?",
        (closed_by, ticket_id)
    )
    conn.commit()
    conn.close()


# ===========================================================================
# Тикеты — листинг (для раздела «Тикеты»)
# ===========================================================================

def get_user_tickets(user_id: int, offset: int = 0, limit: int = 5) -> List[dict]:
    """Возвращает тикеты конкретного пользователя, новые первыми."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "SELECT id, user_id, subject, username, status, created_at, closed_at, closed_by "
        "FROM tickets WHERE user_id=? "
        "ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (user_id, limit, offset)
    )
    rows = c.fetchall()
    conn.close()
    return [_ticket_row_to_dict(r) for r in rows]


def get_user_ticket_count(user_id: int) -> int:
    """Возвращает общее количество тикетов пользователя."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM tickets WHERE user_id=?", (user_id,))
    count = c.fetchone()[0]
    conn.close()
    return count


def get_all_tickets(offset: int = 0, limit: int = 5) -> List[dict]:
    """Возвращает все тикеты (для администратора), новые первыми."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "SELECT id, user_id, subject, username, status, created_at, closed_at, closed_by "
        "FROM tickets "
        "ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (limit, offset)
    )
    rows = c.fetchall()
    conn.close()
    return [_ticket_row_to_dict(r) for r in rows]


def get_all_ticket_count() -> int:
    """Возвращает общее количество тикетов (для администратора)."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM tickets")
    count = c.fetchone()[0]
    conn.close()
    return count


# ===========================================================================
# Сообщения тикета
# ===========================================================================

def add_ticket_message(ticket_id: int, sender_type: str, text: str):
    """
    Добавляет сообщение в тикет.
    sender_type: 'user' или 'admin'.
    """
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "INSERT INTO ticket_messages (ticket_id, sender_type, text) VALUES (?, ?, ?)",
        (ticket_id, sender_type, text)
    )
    conn.commit()
    conn.close()


def get_last_ticket_messages(ticket_id: int, n: int = 3) -> List[dict]:
    """
    Возвращает последние n сообщений тикета в хронологическом порядке
    (от старых к новым).
    """
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "SELECT sender_type, text, sent_at "
        "FROM ticket_messages WHERE ticket_id=? "
        "ORDER BY sent_at DESC, id DESC LIMIT ?",
        (ticket_id, n)
    )
    rows = c.fetchall()
    conn.close()
    return [{"sender_type": r[0], "text": r[1], "sent_at": r[2]} for r in reversed(rows)]


# ===========================================================================
# Антиспам
# ===========================================================================

def get_ticket_count_today(user_id: int) -> int:
    """Возвращает количество тикетов, открытых пользователем за последние 24 часа."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "SELECT COUNT(*) FROM tickets "
        "WHERE user_id=? AND created_at >= datetime('now', '-1 day')",
        (user_id,)
    )
    count = c.fetchone()[0]
    conn.close()
    return count


def set_last_ticket_closed(user_id: int):
    """Записывает время последнего закрытия тикета для кулдауна."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "UPDATE users SET last_ticket_closed_at=CURRENT_TIMESTAMP WHERE user_id=?",
        (user_id,)
    )
    conn.commit()
    conn.close()


def get_minutes_until_next_ticket(user_id: int, cooldown_minutes: int) -> int:
    """
    Возвращает количество минут до снятия кулдауна.
    Если кулдаун не активен — возвращает 0.
    """
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT last_ticket_closed_at FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()

    if not row or not row[0]:
        return 0

    try:
        last_closed = datetime.fromisoformat(str(row[0]).replace(" ", "T"))
    except ValueError:
        return 0

    cooldown_end = last_closed + timedelta(minutes=cooldown_minutes)
    now = datetime.utcnow()

    if now >= cooldown_end:
        return 0

    remaining = cooldown_end - now
    return int(remaining.total_seconds() / 60) + 1
