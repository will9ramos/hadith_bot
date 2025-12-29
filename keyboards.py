# --- START OF FILE keyboards.py ---

from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.filters.callback_data import CallbackData

# Импортируем тексты и справочник книг
from translations import TRANSLATIONS
from config import BOOKS
# Импортируем функции для работы с БД
import database

# --- CallbackData Factory ---
class HadithAction(CallbackData, prefix="h"):
    action: str
    hadith_id: int
    language: str

# --- Клавиатуры-меню (Reply Keyboards) ---
def get_language_keyboard() -> InlineKeyboardMarkup:
    """Возвращает инлайн-клавиатуру для выбора языка."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Русский 🇷🇺", callback_data="lang:ru")],
            [InlineKeyboardButton(text="English 🇬🇧", callback_data="lang:en")],
            [InlineKeyboardButton(text="العربية 🇸🇦", callback_data="lang:ar")],
            [InlineKeyboardButton(text="Türkçe 🇹🇷", callback_data="lang:tr")],
            [InlineKeyboardButton(text="Français 🇫🇷", callback_data="lang:fr")],
            [InlineKeyboardButton(text="বাংলা 🇧🇩", callback_data="lang:bn")],
            [InlineKeyboardButton(text="Bahasa Indonesia 🇮🇩", callback_data="lang:id")],
            [InlineKeyboardButton(text="தமிழ் 🇮🇳", callback_data="lang:ta")] # <-- Добавлено
        ]
    )

def get_main_keyboard(language: str) -> ReplyKeyboardMarkup:
    """Возвращает главную клавиатуру с основными действиями."""
    # Тексты кнопок берутся из translations.py в зависимости от языка
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=TRANSLATIONS[language]['study_new_hadith'])],
            [
                KeyboardButton(text=TRANSLATIONS[language]['search_hadith']),
                KeyboardButton(text=TRANSLATIONS[language]['my_stats'])
            ],
            [
                KeyboardButton(text=TRANSLATIONS[language]['hadith_settings']),
                KeyboardButton(text=TRANSLATIONS[language]['donate'])
            ],
            [KeyboardButton(text=TRANSLATIONS[language]['change_language'])]
        ],
        resize_keyboard=True,
    )

# --- Инлайн-клавиатуры (Inline Keyboards) ---

async def get_settings_keyboard(user_id: int, language: str) -> InlineKeyboardMarkup:
    """Возвращает инлайн-клавиатуру для меню настроек."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=TRANSLATIONS[language]['change_mode'], callback_data="settings:change_mode")],
            [InlineKeyboardButton(text=TRANSLATIONS[language]['change_book_menu'], callback_data="settings:select_books")],
            [InlineKeyboardButton(text=TRANSLATIONS[language]['reminder_settings'], callback_data="settings:reminders")],
            [InlineKeyboardButton(text=TRANSLATIONS[language]['back_to_main_menu'], callback_data="settings:back_to_main_menu")]
        ]
    )

def get_mode_keyboard(language: str) -> InlineKeyboardMarkup:
    """Возвращает клавиатуру для выбора режима (случайный/по порядку)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=TRANSLATIONS[language]['mode_random'], callback_data="mode:random")],
            [InlineKeyboardButton(text=TRANSLATIONS[language]['mode_sequential'], callback_data="mode:sequential")]
        ]
    )

async def get_book_selection_keyboard(user_id: int, language: str) -> InlineKeyboardMarkup:
    """Возвращает клавиатуру для выбора книги для изучения."""
    selected_book = await database.get_user_selected_book(user_id)
    buttons = []
    for book_key, book_name in BOOKS.items():
        status = "✅" if book_key == selected_book else "⚪️"
        # book_name[language] берет название книги на нужном языке из config.py
        buttons.append(
            [InlineKeyboardButton(text=f"{status} {book_name[language]}", callback_data=f"change_book:{book_key}")]
        )
    
    buttons.append([
        InlineKeyboardButton(text=TRANSLATIONS[language]['back_button'], callback_data="settings:back")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def get_reset_books_keyboard(user_id: int, language: str, state: FSMContext) -> InlineKeyboardMarkup:
    """Возвращает клавиатуру для выбора книг для сброса прогресса."""
    data = await state.get_data()
    selected_for_reset = data.get('books_for_reset', [])
    buttons = []
    for book_key, book_name in BOOKS.items():
        status = "✅" if book_key in selected_for_reset else "⬜️"
        buttons.append(
            [InlineKeyboardButton(text=f"{status} {book_name[language]}", callback_data=f"reset_book_toggle:{book_key}")]
        )
    buttons.append([InlineKeyboardButton(text=TRANSLATIONS[language]['reset_confirm_button'], callback_data="reset_books:confirm")])
    buttons.append([InlineKeyboardButton(text=TRANSLATIONS[language]['reset_cancel_button'], callback_data="reset_books:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_reset_confirmation_keyboard(language: str) -> InlineKeyboardMarkup:
    """Возвращает клавиатуру для финального подтверждения сброса прогресса."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=TRANSLATIONS[language]['reset_final_confirm'], callback_data="reset_final:yes")],
            [InlineKeyboardButton(text=TRANSLATIONS[language]['reset_final_cancel'], callback_data="reset_final:no")]
        ]
    )

async def get_reminder_settings_keyboard(user_id: int, language: str) -> InlineKeyboardMarkup:
    """Возвращает клавиатуру для настроек напоминаний."""
    settings = await database.get_reminder_settings(user_id)
    status_text = TRANSLATIONS[language]['reminder_status_enabled'] if settings['enabled'] else TRANSLATIONS[language]['reminder_status_disabled']
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=status_text, callback_data="reminder:toggle")],
            [InlineKeyboardButton(text=TRANSLATIONS[language]['reminder_frequency'], callback_data="reminder:frequency")],
            [InlineKeyboardButton(text=TRANSLATIONS[language]['reminder_time_setting'], callback_data="reminder:time")],
            [InlineKeyboardButton(text=TRANSLATIONS[language]['back_button'], callback_data="reminder:back")]
        ]
    )

def get_reminder_frequency_keyboard(language: str) -> InlineKeyboardMarkup:
    """Возвращает клавиатуру для выбора частоты напоминаний."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=TRANSLATIONS[language]['frequency_1h'], callback_data="freq:1"),
                InlineKeyboardButton(text=TRANSLATIONS[language]['frequency_3h'], callback_data="freq:3"),
            ],
            [
                InlineKeyboardButton(text=TRANSLATIONS[language]['frequency_6h'], callback_data="freq:6"),
                InlineKeyboardButton(text=TRANSLATIONS[language]['frequency_12h'], callback_data="freq:12"),
            ],
            [
                InlineKeyboardButton(text=TRANSLATIONS[language]['frequency_24h'], callback_data="freq:24"),
                InlineKeyboardButton(text=TRANSLATIONS[language]['frequency_48h'], callback_data="freq:48"),
            ],
            [InlineKeyboardButton(text=TRANSLATIONS[language]['frequency_weekly'], callback_data="freq:168")],
            [InlineKeyboardButton(text=TRANSLATIONS[language]['back_button'], callback_data="reminder:back")]
        ]
    )

def get_donate_menu(language: str) -> InlineKeyboardMarkup:
    """Возвращает клавиатуру для выбора суммы пожертвования."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=TRANSLATIONS[language]["donate_10"], callback_data="donate:10"),
                InlineKeyboardButton(text=TRANSLATIONS[language]["donate_50"], callback_data="donate:50")
            ],
            [
                InlineKeyboardButton(text=TRANSLATIONS[language]["donate_100"], callback_data="donate:100"),
                InlineKeyboardButton(text=TRANSLATIONS[language]["donate_500"], callback_data="donate:500")
            ]
        ]
    )

# --- Клавиатуры для админ-панели ---

def get_admin_actions_keyboard(language: str) -> InlineKeyboardMarkup:
    """Возвращает клавиатуру с основными действиями администратора."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=TRANSLATIONS[language]['admin_add_hadiths'], callback_data="admin_action:add_hadiths")],
            [InlineKeyboardButton(text=TRANSLATIONS[language]['admin_send_message'], callback_data="admin_action:send_message")],
            [InlineKeyboardButton(text=TRANSLATIONS[language]['admin_check_users'], callback_data="admin_action:check_users")]
        ]
    )

def get_admin_language_keyboard(language: str) -> InlineKeyboardMarkup:
    """Возвращает клавиатуру для выбора языка при добавлении хадиса через бота."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Русский 🇷🇺", callback_data="admin_lang:ru")],
            [InlineKeyboardButton(text="English 🇬🇧", callback_data="admin_lang:en")],
            [InlineKeyboardButton(text="العربية 🇸🇦", callback_data="admin_lang:ar")],
            [InlineKeyboardButton(text="Türkçe 🇹🇷", callback_data="admin_lang:tr")],
            [InlineKeyboardButton(text="Français 🇫🇷", callback_data="admin_lang:fr")],
            [InlineKeyboardButton(text="বাংলা 🇧🇩", callback_data="admin_lang:bn")],
            [InlineKeyboardButton(text="Bahasa Indonesia 🇮🇩", callback_data="admin_lang:id")],
            [InlineKeyboardButton(text="தமிழ் 🇮🇳", callback_data="admin_lang:ta")] # <-- Добавлено
        ]
    )

def get_admin_books_keyboard(admin_language: str) -> InlineKeyboardMarkup:
    """Возвращает клавиатуру для выбора книги при добавлении хадиса."""
    buttons = []
    for book_key, book_names in BOOKS.items():
        # Берем название книги на выбранном для добавления языке
        book_name = book_names.get(admin_language, book_key)
        buttons.append(
            [InlineKeyboardButton(text=book_name, callback_data=f"admin_book:{book_key}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_message_type_keyboard(language: str) -> InlineKeyboardMarkup:
    """Возвращает клавиатуру для выбора типа рассылки (переслать/от имени бота)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=TRANSLATIONS[language]['admin_message_forward'], callback_data="message_type:forward")],
            [InlineKeyboardButton(text=TRANSLATIONS[language]['admin_message_bot'], callback_data="message_type:bot")]
        ]
    )

# --- END OF FILE keyboards.py ---