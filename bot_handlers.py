# --- START OF FILE bot_handlers.py ---

import random
import re
import asyncio
import logging
from typing import List, Union, Dict, Any, Callable, Awaitable

from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.types import (
    Message, CallbackQuery, LabeledPrice, PreCheckoutQuery, ContentType,
    InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto, InputMediaVideo, InputMediaAudio, InputMediaDocument
)
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter, TelegramBadRequest
from aiogram.filters.callback_data import CallbackData

# Импортируем наши модули
import database
import keyboards
import utils
from translations import TRANSLATIONS
from config import ADMIN_ID, BOOKS
from keyboards import HadithAction

# Настройка логирования
logger = logging.getLogger(__name__)

# Создаем Router
router = Router()

# --- Middleware для обработки альбомов ---
class AlbumMiddleware(BaseMiddleware):
    def __init__(self, latency: float = 0.6):
        self.latency = latency
        self.album_data: dict = {}

    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any]
    ) -> Any:
        if not event.media_group_id:
            return await handler(event, data)

        media_group_id = event.media_group_id

        if media_group_id in self.album_data:
            self.album_data[media_group_id].append(event)
            return

        self.album_data[media_group_id] = [event]
        
        try:
            await asyncio.sleep(self.latency)
        except asyncio.CancelledError:
            if media_group_id in self.album_data:
                del self.album_data[media_group_id]
            raise

        if media_group_id not in self.album_data:
            return

        album = self.album_data.pop(media_group_id)
        album.sort(key=lambda x: x.message_id)
        
        data["album"] = album
        return await handler(event, data)

router.message.middleware(AlbumMiddleware())


# --- FSM Состояния ---

class LanguageStates(StatesGroup):
    choosing_language = State()

class AdminStates(StatesGroup):
    choosing_action = State()
    choosing_language = State()
    choosing_book = State()
    waiting_for_hadith = State()
    choosing_message_type = State()
    waiting_for_message = State()

class SettingsStates(StatesGroup):
    choosing_option = State()
    choosing_mode = State()
    selecting_books = State()

class ResetProgressStates(StatesGroup):
    selecting_books_for_reset = State()
    confirming_reset = State()

class SearchStates(StatesGroup):
    waiting_for_number = State()

class ReminderStates(StatesGroup):
    setting_time = State()

# --- Вспомогательная функция для отправки сообщений ---
async def send_hadith_message(message: Message, hadith: dict, language: str):
    """
    Отправляет сообщение с хадисом.
    Исправлены ошибки №1, №8, №14 (Лимиты, Audio vs Voice, Caption).
    """
    inline_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=TRANSLATIONS[language]['hadith_studied'],
            callback_data=HadithAction(
                action="studied",
                hadith_id=hadith['id'],
                language=language
            ).pack()
        )
    ]])
    
    # Добавляем заголовок книги для красоты и ясности
    book_title = hadith.get('book_name', '')
    text_content = f"📖 {hadith['text'].strip()}"
    
    # Внутренняя функция для попытки отправки
    async def try_send(parse_mode):
        audio_id = hadith.get('audio_file_id')
        
        if audio_id:
            # Ошибка №8: Проверяем тип аудио (audio/voice)
            # Если типа нет в БД, считаем audio (как в миграции)
            audio_type = hadith.get('audio_type', 'audio')
            
            # Ошибка №1 и №14: Проверка лимита подписи (1024 символа)
            if len(text_content) > 1024:
                # Сценарий "Длинный текст": Сначала медиа, потом текст
                if audio_type == 'voice':
                    await message.answer_voice(voice=audio_id)
                else:
                    await message.answer_audio(audio=audio_id)
                # Текст отдельным сообщением (лимит 4096)
                await message.answer(text_content, reply_markup=inline_kb, parse_mode=parse_mode)
            else:
                # Сценарий "Короткий текст": Медиа с подписью
                if audio_type == 'voice':
                    await message.answer_voice(voice=audio_id, caption=text_content, reply_markup=inline_kb, parse_mode=parse_mode)
                else:
                    await message.answer_audio(audio=audio_id, caption=text_content, reply_markup=inline_kb, parse_mode=parse_mode)
        else:
            # Только текст
            await message.answer(text_content, reply_markup=inline_kb, parse_mode=parse_mode)

    try:
        await try_send(parse_mode="HTML")
    except TelegramBadRequest as e:
        if "can't parse entities" in str(e):
            logger.warning(f"Ошибка HTML в хадисе {hadith['id']}. Отправка без форматирования.")
            try:
                # Ошибка №5: Fallback при кривом HTML
                await try_send(parse_mode=None)
            except Exception as e2:
                logger.error(f"Не удалось отправить хадис {hadith['id']}: {e2}")
                await message.answer(TRANSLATIONS[language]['error'])
        else:
            logger.error(f"Ошибка API при отправке хадиса {hadith['id']}: {e}")
            await message.answer(TRANSLATIONS[language]['error'])
    except Exception as e:
        logger.error(f"Неизвестная ошибка с хадисом {hadith['id']}: {e}")
        await message.answer(TRANSLATIONS[language]['error'])

# --- Обработчики команд и основного меню ---

@router.message(Command("start"))
async def start(message: Message, state: FSMContext):
    await state.clear()
    await database.save_user(
        user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name
    )
    language = await database.get_user_language(message.from_user.id)
    if language and language in TRANSLATIONS:
        main_kb = keyboards.get_main_keyboard(language)
        await message.answer(TRANSLATIONS[language]['start_message'], reply_markup=main_kb)
    else:
        await message.answer(
            TRANSLATIONS['ru']['welcome'],
            reply_markup=keyboards.get_language_keyboard()
        )
        await state.set_state(LanguageStates.choosing_language)

@router.message(F.text.in_([t['change_language'] for t in TRANSLATIONS.values()]))
async def change_language_handler(message: Message, state: FSMContext):
    language = await database.get_user_language(message.from_user.id)
    await message.answer(
        TRANSLATIONS[language]['language_prompt'],
        reply_markup=keyboards.get_language_keyboard()
    )
    await state.set_state(LanguageStates.choosing_language)

@router.callback_query(F.data.startswith("lang:"))
@router.message(LanguageStates.choosing_language)
async def process_language_choice(update: Union[Message, CallbackQuery], state: FSMContext):
    if not update.from_user:
        return
        
    user_id = update.from_user.id
    
    if isinstance(update, Message):
        if update.text:
            text = update.text.lower()
            if "русский" in text: language = 'ru'
            elif "english" in text: language = 'en'
            elif "türkçe" in text or "turkish" in text: language = 'tr'
            elif "français" in text or "french" in text: language = 'fr'
            elif "বাংলা" in text or "bengali" in text or "bangla" in text: language = 'bn'
            elif "indonesia" in text or "bahasa" in text: language = 'id'
            elif "tamil" in text or "தமிழ்" in text: language = 'ta' 
            else:
                await update.answer("Пожалуйста, выберите язык, нажав на кнопку.")
                return
        else:
            await update.answer("Пожалуйста, используйте кнопки для выбора языка.")
            return
    else:
        language = update.data.split(":")[1]

    await database.set_user_language(user_id, language)
    await state.clear()
    main_kb = keyboards.get_main_keyboard(language)
    
    if isinstance(update, CallbackQuery):
        await update.message.delete()
    
    await update.bot.send_message(user_id, TRANSLATIONS[language]['language_changed'], reply_markup=main_kb)
    await update.bot.send_message(user_id, TRANSLATIONS[language]['start_message'], reply_markup=main_kb)


@router.message(F.text.in_([t['study_new_hadith'] for t in TRANSLATIONS.values()]))
async def new_hadith_handler(message: Message, state: FSMContext):
    user_id = message.from_user.id
    language = await database.get_user_language(user_id)
    mode = await database.get_user_mode(user_id)
    
    hadith_to_show = await database.get_next_unstudied_hadith(user_id, language, mode)
    
    if not hadith_to_show:
        selected_book = await database.get_user_selected_book(user_id)
        selected_book_name = BOOKS.get(selected_book, {}).get(language, selected_book)
        
        async with database.db_pool.acquire() as conn:
             total_count = await conn.fetchval("SELECT COUNT(*) FROM hadiths WHERE book = $1 AND language = $2", selected_book, language)
            
        if total_count == 0:
            await message.answer(TRANSLATIONS[language]['no_hadiths_in_book'].format(book_name=selected_book_name))
        else:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=TRANSLATIONS[language]['go_to_settings'],
                    callback_data="settings:select_books"
                )]
            ])
            await message.answer(TRANSLATIONS[language]['all_studied_in_book'], reply_markup=kb)
        return

    await send_hadith_message(message, hadith_to_show, language)

@router.callback_query(HadithAction.filter(F.action == "studied"))
async def mark_studied_callback(callback: CallbackQuery, callback_data: HadithAction, state: FSMContext):
    user_id = callback.from_user.id
    language = await database.get_user_language(user_id)
    hadith_id = callback_data.hadith_id
    hadith_lang = callback_data.language

    if hadith_lang != language:
        await callback.answer("Error: Language mismatch. Please switch language.", show_alert=True)
        return
        
    studied = await database.load_progress(user_id, language)
    if hadith_id in studied:
        await callback.answer(TRANSLATIONS[language]['already_studied'], show_alert=True)
        return
        
    await database.save_progress(user_id, hadith_id, language)
    
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer(TRANSLATIONS[language]['hadith_studied_alert'])
    await callback.message.reply(
        TRANSLATIONS[language]['study_success'],
        reply_markup=keyboards.get_main_keyboard(language)
    )

# --- Обработчики статистики и сброса ---

@router.message(F.text.in_([t['my_stats'] for t in TRANSLATIONS.values()]))
async def show_user_stats(message: Message):
    user_id = message.from_user.id
    language = await database.get_user_language(user_id)
    
    stats_data = await database.get_detailed_user_stats(user_id, language)
    if not stats_data:
        await message.answer(TRANSLATIONS[language]['error'])
        return
        
    response_lines = [f"<b>{TRANSLATIONS[language]['user_stats_header']}</b>"]
    has_progress = any(stat['studied'] > 0 for stat in stats_data)
    
    for stat in stats_data:
        if stat['total'] > 0:
            percentage = (stat['studied'] / stat['total']) * 100
            response_lines.append(
                TRANSLATIONS[language]['user_stats_line'].format(
                    book_name=stat['book_name'], studied=stat['studied'],
                    total=stat['total'], percentage=f"{percentage:.1f}"
                )
            )
    
    inline_kb = None
    if has_progress:
        inline_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=TRANSLATIONS[language]['reset_progress_button'],
                callback_data="reset_progress:start"
            )
        ]])
        
    await message.answer("\n".join(response_lines), reply_markup=inline_kb, parse_mode="HTML")

@router.callback_query(F.data == "reset_progress:start")
async def start_reset_progress(callback: CallbackQuery, state: FSMContext):
    language = await database.get_user_language(callback.from_user.id)
    await state.set_state(ResetProgressStates.selecting_books_for_reset)
    await state.update_data(books_for_reset=[])
    kb = await keyboards.get_reset_books_keyboard(callback.from_user.id, language, state)
    await callback.message.edit_text(TRANSLATIONS[language]['reset_select_books'], reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data.startswith("reset_book_toggle:"), ResetProgressStates.selecting_books_for_reset)
async def toggle_book_for_reset(callback: CallbackQuery, state: FSMContext):
    language = await database.get_user_language(callback.from_user.id)
    book_key = callback.data.split(":")[1]
    
    data = await state.get_data()
    selected_books = data.get('books_for_reset', [])
    
    if book_key in selected_books: selected_books.remove(book_key)
    else: selected_books.append(book_key)
        
    await state.update_data(books_for_reset=selected_books)
    kb = await keyboards.get_reset_books_keyboard(callback.from_user.id, language, state)
    await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data == "reset_books:cancel", ResetProgressStates.selecting_books_for_reset)
async def cancel_reset_progress(callback: CallbackQuery, state: FSMContext):
    language = await database.get_user_language(callback.from_user.id)
    await callback.message.delete()
    await callback.message.answer(TRANSLATIONS[language]['reset_cancelled'])
    await state.clear()
    await callback.answer()

@router.callback_query(F.data == "reset_books:confirm", ResetProgressStates.selecting_books_for_reset)
async def confirm_reset_selection(callback: CallbackQuery, state: FSMContext):
    language = await database.get_user_language(callback.from_user.id)
    data = await state.get_data()
    books_to_reset = data.get('books_for_reset', [])

    if not books_to_reset:
        await callback.answer(TRANSLATIONS[language]['reset_no_books_selected'], show_alert=True)
        return

    book_names = [BOOKS[key][language] for key in books_to_reset]
    await state.set_state(ResetProgressStates.confirming_reset)
    await callback.message.edit_text(
        TRANSLATIONS[language]['reset_confirmation_warning'].format(book_list="\n - ".join(book_names)),
        reply_markup=keyboards.get_reset_confirmation_keyboard(language),
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("reset_final:"), ResetProgressStates.confirming_reset)
async def final_reset_confirmation(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    language = await database.get_user_language(user_id)
    if callback.data.split(":")[1] == 'yes':
        data = await state.get_data()
        books_to_reset = data.get('books_for_reset', [])
        deleted_count = await database.reset_user_progress(user_id, language, books_to_reset)
        await callback.message.edit_text(TRANSLATIONS[language]['reset_success'].format(count=deleted_count))
    else:
        await callback.message.edit_text(TRANSLATIONS[language]['reset_cancelled'])
    
    await state.clear()
    await callback.answer()

# --- Обработчики настроек ---

@router.message(F.text.in_([t['hadith_settings'] for t in TRANSLATIONS.values()]))
async def hadith_settings_handler(message: Message, state: FSMContext):
    language = await database.get_user_language(message.from_user.id)
    kb = await keyboards.get_settings_keyboard(message.from_user.id, language)
    await message.answer(TRANSLATIONS[language]['settings_prompt'], reply_markup=kb, parse_mode="HTML")
    await state.set_state(SettingsStates.choosing_option)

@router.callback_query(F.data.startswith("settings:"), SettingsStates.choosing_option)
async def process_settings_choice(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    language = await database.get_user_language(user_id)
    option = callback.data.split(":")[1]
    
    if option == "change_mode":
        await callback.message.edit_text(TRANSLATIONS[language]['mode_prompt'], reply_markup=keyboards.get_mode_keyboard(language))
        await state.set_state(SettingsStates.choosing_mode)
    elif option == "select_books":
        kb = await keyboards.get_book_selection_keyboard(user_id, language)
        await callback.message.edit_text(TRANSLATIONS[language]['change_book_prompt'], reply_markup=kb)
    elif option == "back": 
        kb = await keyboards.get_settings_keyboard(user_id, language)
        await callback.message.edit_text(TRANSLATIONS[language]['settings_prompt'], reply_markup=kb, parse_mode="HTML")
        await state.set_state(SettingsStates.choosing_option)
    elif option == "reminders":
        kb = await keyboards.get_reminder_settings_keyboard(user_id, language)
        await callback.message.edit_text(TRANSLATIONS[language]['reminder_settings'], reply_markup=kb)
    elif option == "back_to_main_menu":
        await callback.message.delete()
        await callback.bot.send_message(
            chat_id=user_id,
            text=TRANSLATIONS[language]['main_menu'],
            reply_markup=keyboards.get_main_keyboard(language)
        )
        await state.clear()
    await callback.answer()

@router.callback_query(F.data.startswith("mode:"), SettingsStates.choosing_mode)
async def process_mode_choice(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    language = await database.get_user_language(user_id)
    mode = callback.data.split(":")[1]
    await database.set_user_mode(user_id, mode)
    mode_name = TRANSLATIONS[language][f'mode_{mode}']
    await callback.answer(TRANSLATIONS[language]['mode_changed'].format(mode_name=mode_name), show_alert=True)
    
    kb = await keyboards.get_settings_keyboard(user_id, language)
    await callback.message.edit_text(TRANSLATIONS[language]['settings_prompt'], reply_markup=kb, parse_mode="HTML")
    await state.set_state(SettingsStates.choosing_option)

@router.callback_query(F.data.startswith("change_book:"))
async def process_book_change(callback: CallbackQuery):
    user_id = callback.from_user.id
    language = await database.get_user_language(user_id)
    book_key = callback.data.split(":")[1]
    await database.set_user_selected_book(user_id, book_key)
    book_name = BOOKS[book_key][language]
    kb = await keyboards.get_book_selection_keyboard(user_id, language)
    await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer(TRANSLATIONS[language]['book_changed'].format(book_name=book_name))

# --- Обработчики поиска ---

@router.message(F.text.in_([t['search_hadith'] for t in TRANSLATIONS.values()]))
async def start_hadith_search(message: Message, state: FSMContext):
    language = await database.get_user_language(message.from_user.id)
    selected_book = await database.get_user_selected_book(message.from_user.id)
    book_name = BOOKS[selected_book][language]
    await message.answer(TRANSLATIONS[language]['search_hadith_prompt'].format(book_name=book_name), parse_mode="HTML")
    await state.set_state(SearchStates.waiting_for_number)

@router.message(SearchStates.waiting_for_number)
async def process_search_number(message: Message, state: FSMContext):
    user_id = message.from_user.id
    language = await database.get_user_language(user_id)
    
    if message.text and message.text.lower() in ['/cancel', 'отмена', 'cancel']:
        await message.answer(TRANSLATIONS[language]['search_cancelled'])
        await state.clear()
        return

    if not re.match(r'^\d+$', message.text.strip()):
        await message.answer(TRANSLATIONS[language]['invalid_number_format'])
        return
        
    selected_book = await database.get_user_selected_book(user_id)
    hadith = await database.search_hadith_by_number(message.text, language, selected_book)
    
    if hadith:
        await send_hadith_message(message, hadith, language)
    else:
        await message.answer(TRANSLATIONS[language]['hadith_not_found'].format(number=message.text), parse_mode="HTML")
    
    await state.clear()

# --- Обработчики настроек напоминаний ---

@router.callback_query(F.data.startswith("reminder:"))
async def process_reminder_settings(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    language = await database.get_user_language(user_id)
    action = callback.data.split(":")[1]
    
    if action == "toggle":
        settings = await database.get_reminder_settings(user_id)
        new_status = not settings['enabled']
        await database.set_reminder_enabled(user_id, new_status)
        alert_text = TRANSLATIONS[language]['reminder_enabled'] if new_status else TRANSLATIONS[language]['reminder_disabled']
        await callback.answer(alert_text, show_alert=True)
        kb = await keyboards.get_reminder_settings_keyboard(user_id, language)
        await callback.message.edit_reply_markup(reply_markup=kb)
    elif action == "frequency":
        await callback.message.edit_text(TRANSLATIONS[language]['reminder_frequency_prompt'], reply_markup=keyboards.get_reminder_frequency_keyboard(language))
    elif action == "time":
        await callback.message.edit_text(TRANSLATIONS[language]['reminder_time_prompt'], parse_mode="HTML")
        await state.set_state(ReminderStates.setting_time)
    elif action == "back": 
        kb = await keyboards.get_settings_keyboard(user_id, language)
        await callback.message.edit_text(TRANSLATIONS[language]['settings_prompt'], reply_markup=kb, parse_mode="HTML")
        await state.set_state(SettingsStates.choosing_option)
    await callback.answer()

@router.callback_query(F.data.startswith("freq:"))
async def process_frequency_selection(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    language = await database.get_user_language(user_id)
    frequency = int(callback.data.split(":")[1])
    await database.set_reminder_frequency(user_id, frequency)
    
    freq_names = {1: '1h', 3: '3h', 6: '6h', 12: '12h', 24: '24h', 48: '48h', 168: 'weekly'}
    freq_name_key = f"frequency_{freq_names.get(frequency, '12h')}"
    freq_name = TRANSLATIONS[language].get(freq_name_key, f"{frequency} hours")
    
    await callback.answer(TRANSLATIONS[language]['reminder_frequency_set'].format(frequency=freq_name), show_alert=True)
    kb = await keyboards.get_reminder_settings_keyboard(user_id, language)
    await callback.message.edit_text(TRANSLATIONS[language]['reminder_settings'], reply_markup=kb)
    await state.set_state(SettingsStates.choosing_option)

@router.message(ReminderStates.setting_time)
async def process_reminder_time(message: Message, state: FSMContext):
    user_id = message.from_user.id
    language = await database.get_user_language(user_id)
    
    if message.text and message.text.lower() in ['/cancel', 'отмена', 'cancel']:
        await message.answer(TRANSLATIONS[language]['reminder_time_cancelled'])
        await state.clear()
        return
        
    if not re.match(r'^([0-1]?[0-9]|2[0-3]):[0-5][0-9]$', message.text.strip()):
        await message.answer(TRANSLATIONS[language]['invalid_time_format'], parse_mode="HTML")
        return
        
    await database.set_reminder_time(user_id, message.text.strip())
    await message.answer(TRANSLATIONS[language]['reminder_time_set'].format(time=message.text.strip()))
    
    kb = await keyboards.get_reminder_settings_keyboard(user_id, language)
    await message.answer(TRANSLATIONS[language]['reminder_settings'], reply_markup=kb)
    await state.clear()
    await state.set_state(SettingsStates.choosing_option)

# --- Обработчики пожертвований ---

@router.message(F.text.in_([t['donate'] for t in TRANSLATIONS.values()]))
async def donate_handler(message: Message):
    language = await database.get_user_language(message.from_user.id)
    await message.answer(
        TRANSLATIONS[language]['donate_message'],
        reply_markup=keyboards.get_donate_menu(language)
    )

@router.callback_query(F.data.startswith("donate:"))
async def send_donation_invoice(callback: CallbackQuery, bot: Bot):
    language = await database.get_user_language(callback.from_user.id)
    amount = int(callback.data.split(":")[1])
    try:
        await bot.send_invoice(
            chat_id=callback.from_user.id,
            title="Поддержка бота" if language == "ru" else "Bot Support",
            description=TRANSLATIONS[language]['donate_message'],
            payload=f"donation_stars_{amount}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label="Пожертвование" if language == "ru" else "Donation", amount=amount)]
        )
    except Exception as e:
        logger.error(f"Ошибка создания счета на пожертвование: {e}")
        await callback.message.answer(TRANSLATIONS[language]['donation_error'])
    await callback.answer()

@router.pre_checkout_query()
async def pre_checkout_query_handler(pre_checkout_query: PreCheckoutQuery, bot: Bot):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@router.message(F.content_type == ContentType.SUCCESSFUL_PAYMENT)
async def successful_payment_handler(message: Message):
    language = await database.get_user_language(message.from_user.id)
    await message.answer(TRANSLATIONS[language]['donation_success'])

# --- Админские обработчики ---

@router.message(Command("sheri"))
async def admin_panel_command(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    await state.clear()
    language = await database.get_user_language(message.from_user.id)
    await message.answer(
        TRANSLATIONS[language]['admin_mode'],
        reply_markup=keyboards.get_admin_actions_keyboard(language),
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.choosing_action)

@router.callback_query(F.data.startswith("admin_action:"), AdminStates.choosing_action)
async def admin_choose_action(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    language = await database.get_user_language(callback.from_user.id)
    action = callback.data.split(":")[1]

    if action == "add_hadiths":
        await callback.message.edit_text(
            TRANSLATIONS[language]['admin_choose_language'],
            reply_markup=keyboards.get_admin_language_keyboard(language)
        )
        await state.set_state(AdminStates.choosing_language)
    elif action == "send_message":
        await callback.message.edit_text(
            TRANSLATIONS[language]['admin_message_prompt'],
            reply_markup=keyboards.get_message_type_keyboard(language)
        )
        await state.set_state(AdminStates.choosing_message_type)
    elif action == "check_users":
        total, blocked = await database.get_user_report()
        await callback.answer(TRANSLATIONS[language]['user_report'].format(total, blocked), show_alert=True)
    await callback.answer()

@router.callback_query(F.data.startswith("admin_lang:"), AdminStates.choosing_language)
async def admin_choose_language(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    language = await database.get_user_language(callback.from_user.id)
    admin_language = callback.data.split(":")[1]
    await state.update_data(admin_language=admin_language)
    await callback.message.edit_text(
        TRANSLATIONS[language]['admin_prompt'],
        reply_markup=keyboards.get_admin_books_keyboard(admin_language)
    )
    await state.set_state(AdminStates.choosing_book)
    await callback.answer()

@router.callback_query(F.data.startswith("admin_book:"), AdminStates.choosing_book)
async def admin_choose_book(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    language = await database.get_user_language(callback.from_user.id)
    book_key = callback.data.split(":")[1]
    await state.update_data(admin_book=book_key)
    await callback.message.edit_text(TRANSLATIONS[language]['admin_hadith_prompt'])
    await state.set_state(AdminStates.waiting_for_hadith)
    await callback.answer()

@router.message(AdminStates.waiting_for_hadith)
async def receive_hadith(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    language = await database.get_user_language(message.from_user.id)
    
    if message.text and message.text.lower() == "/cancel":
        await state.clear()
        await message.answer(TRANSLATIONS[language]['admin_cancel'])
        return
        
    data = await state.get_data()
    # Сохраняем хадис
    await database.save_hadith(message.text.strip(), data['admin_language'], data['admin_book'])
    await message.answer(TRANSLATIONS[language]['hadith_added'])

@router.callback_query(F.data.startswith("message_type:"), AdminStates.choosing_message_type)
async def admin_choose_message_type(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    language = await database.get_user_language(callback.from_user.id)
    message_type = callback.data.split(":")[1]
    await state.update_data(message_type=message_type)
    await callback.message.edit_text(TRANSLATIONS[language]['admin_message_input'])
    await state.set_state(AdminStates.waiting_for_message)
    await callback.answer()

@router.message(AdminStates.waiting_for_message, F.content_type.in_({ContentType.TEXT, ContentType.PHOTO, ContentType.VIDEO, ContentType.DOCUMENT, ContentType.AUDIO, ContentType.VOICE}))
async def receive_broadcast_message(message: Message, state: FSMContext, album: List[Message] = None):
    """
    Обработчик рассылки от админа.
    Исправлена ошибка №4 (Protected Content) и логика альбомов.
    """
    if message.from_user.id != ADMIN_ID: return
    
    data = await state.get_data()
    message_type = data.get('message_type') # 'forward' или 'bot' (copy)
    
    messages_to_send = album if album else [message]
    
    # Ошибка №4: Проверка на защищенный контент (для режима 'bot')
    if message_type == "bot":
        for m in messages_to_send:
            if m.has_protected_content:
                await message.answer("❌ <b>Ошибка!</b>\n\nЭто сообщение имеет защиту от копирования (Protected Content). Его нельзя разослать от имени бота.\nИспользуйте режим 'Переслать' или создайте новое сообщение.")
                return

    # ВАЛИДАЦИЯ ТИПОВ МЕДИА (только для режима 'bot'/Copy)
    if message_type == "bot" and len(messages_to_send) > 1:
        has_visual = any(m.photo or m.video for m in messages_to_send)
        has_audio = any(m.audio for m in messages_to_send)
        has_doc = any(m.document for m in messages_to_send)
        
        # Telegram позволяет группировать (Фото+Видео), (Аудио+Аудио), (Док+Док). Смешивать их нельзя.
        if (has_visual and has_audio) or (has_visual and has_doc) or (has_audio and has_doc):
            await message.answer("❌ <b>Ошибка формата!</b>\n\nНельзя смешивать разные типы медиа в одном альбоме.")
            return

    task = {
        'type': 'copy' if message_type == 'bot' else 'forward', 
        'from_chat_id': message.chat.id,
        'message_ids': [m.message_id for m in messages_to_send]
    }
    
    await utils.schedule_broadcast(task)
    await message.answer("✅ <b>Рассылка поставлена в очередь!</b>")
    await state.clear()

@router.message(Command("cancel"))
async def cancel_state(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        return
        
    language = await database.get_user_language(message.from_user.id)
    await state.clear()
    await message.answer(
        TRANSLATIONS[language]['action_cancelled'],
        reply_markup=keyboards.get_main_keyboard(language)
    )

@router.message(Command("stats"))
async def show_stats_command(message: Message):
    if message.from_user.id != ADMIN_ID: return
    language = await database.get_user_language(message.from_user.id)
    async with database.db_pool.acquire() as conn:
        total_users = await conn.fetchval('SELECT COUNT(*) FROM users')
        active_today = await conn.fetchval("SELECT COUNT(*) FROM users WHERE registered_at >= extract(epoch from now() - interval '24 hour')")
        total_hadiths = await conn.fetchval('SELECT COUNT(*) FROM hadiths')
        
        stats_text = TRANSLATIONS[language]['stats'].format(
            total_users=total_users,
            active_today=active_today,
            total_hadiths=total_hadiths
        )
        await message.answer(stats_text, parse_mode="HTML")

# --- Обработчик неизвестных сообщений (Должен быть в самом низу!) ---

@router.message(F.text, StateFilter(None))
async def unknown_message_handler(message: Message):
    """
    Отвечает на любой текст, который не является командой или кнопкой,
    и если пользователь не находится в процессе ввода данных (State is None).
    """
    # Игнорируем команды (на всякий случай, хотя Command handler выше их перехватит)
    if message.text.startswith('/'):
        return

    user_id = message.from_user.id
    language = await database.get_user_language(user_id)
    
    # Отключаем предпросмотр ссылок, если хотите, чтобы ссылка была просто текстом
    # Но обычно для каналов лучше оставить предпросмотр (поэтому disable_web_page_preview не ставим)
    await message.answer(TRANSLATIONS[language]['bot_disclaimer'])

# --- END OF FILE bot_handlers.py ---