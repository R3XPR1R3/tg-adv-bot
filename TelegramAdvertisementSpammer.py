import os
import time
import pandas as pd
from pyrogram import Client as PyrogramClient, errors
from pyrogram.enums import ChatType
from pyrogram.types import InputMediaPhoto
from colorama import init, Fore, Style
from telethon.sync import TelegramClient as TelethonClient
from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.types import InputPeerChannel
import asyncio
from telethon.errors import AuthRestartError, SessionPasswordNeededError
import ctypes  # Импорт для предотвращения спящего режима

# Инициализация colorama
init()

# Введи свои данные здесь
api_id = '29960588'
api_hash = 'ae63e5c242756ebd653f18843ec4d122'

# Запрос номера телефона у пользователя
phone_number = input("Введите номер телефона в международном формате (например, +123456789): ")

# Список ID чатов для исключения
excluded_chat_ids = [
]

# Путь к папке с фотографиями и сохранения Excel-файла
photos_folder = r"C:\Users\danii\Downloads\PhotosScript"
excel_folder = r"C:\Users\danii\Downloads\PhotosScript\excelcharts"

# Получаем список всех файлов в папке (берем только первые три фотографии)
photos = [os.path.join(photos_folder, file) for file in os.listdir(photos_folder) if
          file.endswith(('.png', '.jpg', '.jpeg'))][:3]

# Цвета для разных сообщений
colors = {
    'success': Fore.GREEN,
    'exclude': Fore.YELLOW,
    'wrong_type': Fore.LIGHTBLACK_EX,
    'error': Fore.RED
}

# Функция для предотвращения спящего режима
def prevent_sleep_mode():
    # Параметры для предотвращения сна
    ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)

# Восстановление нормального режима сна
def restore_sleep_mode():
    ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)

# Функция для удаления файлов сессий
def delete_session_files():
    try:
        if os.path.exists("my_account.session"):
            os.remove("my_account.session")
            print(Fore.GREEN + "Файл my_account.session успешно удален." + Style.RESET_ALL)
        if os.path.exists("session_name.session"):
            os.remove("session_name.session")
            print(Fore.GREEN + "Файл session_name.session успешно удален." + Style.RESET_ALL)
    except Exception as e:
        print(Fore.RED + f"Ошибка при удалении файлов сессий: {e}" + Style.RESET_ALL)

async def send_messages(app):
    # Запросить ввод текста сообщения
    message_text = input("Введите текст сообщения: ")

    # Запросить выбор до 10 файлов для вложений
    files = []
    for i in range(10):
        file_path = input(f"Введите путь к файлу для вложения ({i+1}/10) или нажмите Enter для завершения: ")
        if not file_path:
            break
        if os.path.isfile(file_path):
            files.append(file_path)
        else:
            print(Fore.RED + f"Файл не найден: {file_path}" + Style.RESET_ALL)

    async for dialog in app.get_dialogs():
        chat = dialog.chat
        chat_type = chat.type
        chat_title = chat.title if chat.title else "Без названия"

        try:
            if chat.id not in excluded_chat_ids:
                if chat_type in [ChatType.GROUP, ChatType.SUPERGROUP]:
                    media_group = [InputMediaPhoto(file) for file in files]

                    if media_group:
                        media_group[0].caption = message_text

                    await app.send_media_group(chat.id, media_group)
                    print(colors['success'] + f"Медиа-группа и текстовое сообщение успешно отправлены в группу {chat_title}" + Style.RESET_ALL)

                    await asyncio.sleep(10)
                else:
                    print(colors['wrong_type'] + f"Пропуск {chat_title} (не группа, тип: {chat_type})" + Style.RESET_ALL)
            else:
                print(colors['exclude'] + f"Пропуск {chat_title} (ID {chat.id} в списке исключений)" + Style.RESET_ALL)
        except Exception as e:
            print(colors['error'] + f"Ошибка при отправке в {chat_title}: {e}" + Style.RESET_ALL)

async def collect_groups(app):
    group_data = []

    async for dialog in app.get_dialogs():
        chat = dialog.chat
        chat_type = chat.type
        chat_title = chat.title if chat.title else "Без названия"

        if chat.id not in excluded_chat_ids and chat_type in [ChatType.GROUP, ChatType.SUPERGROUP]:
            group_data.append({"Chat ID": chat.id, "Chat Title": chat_title})

    if group_data:
        df = pd.DataFrame(group_data)
        os.makedirs(excel_folder, exist_ok=True)
        excel_file = os.path.join(excel_folder, "target_groups.xlsx")
        df.to_excel(excel_file, index=False)
        print(Fore.GREEN + f"Группы успешно сохранены в {excel_file}" + Style.RESET_ALL)
    else:
        print(Fore.YELLOW + "Нет групп для сохранения." + Style.RESET_ALL)

async def collect_groups_by_query_telethon(client, keyword):
    try:
        result = await client(SearchRequest(
            q=keyword,
            limit=input("сколько групп вы хотите провести за сессию? (помните про лимит)")  # Увеличьте количество результатов поиска
        ))

        for chat in result.chats:
            chat_type = 'Канал' if isinstance(chat, InputPeerChannel) else 'Группа'
            if chat.id not in excluded_chat_ids:
                try:
                    await client(JoinChannelRequest(chat))
                    print(f"Подписан на {chat.title} ({chat_type})")
                    await asyncio.sleep(2)  # Задержка между подписками
                except errors.FloodWait as e:
                    print(f"Слишком много запросов! Ожидание {e.value} секунд.")
                    await asyncio.sleep(e.value)
                except Exception as e:
                    print(f"Ошибка при подписке на {chat.title}: {e}")
            else:
                print(f"Уже подписан на {chat.title} ({chat_type}) или это не группа.")
    except Exception as e:
        print(f"Ошибка поиска по запросу {keyword}: {e}")

async def main():
    prevent_sleep_mode()  # Отключить переход в спящий режим
    app = PyrogramClient("my_account", api_id=api_id, api_hash=api_hash)
    await app.start()

    # Создаем Telethon клиент
    telethon_client = TelethonClient('session_name', api_id, api_hash)

    try:
        await telethon_client.connect()

        if not await telethon_client.is_user_authorized():
            await telethon_client.send_code_request(phone_number)

            try:
                code = input("Введите код, который вы получили: ")
                await telethon_client.sign_in(phone_number, code)
            except AuthRestartError:
                print("Ошибка авторизации, перезапуск процесса.")
                await telethon_client.send_code_request(phone_number)
                code = input("Введите код, который вы получили: ")
                await telethon_client.sign_in(phone_number, code)
            except SessionPasswordNeededError:
                password = input("Ваш аккаунт защищен двухфакторной аутентификацией. Введите пароль: ")
                await telethon_client.sign_in(password=password)

        while True:
            print("Выберите режим работы:")
            print("1 - Спам")
            print("2 - Сбор групп")
            print("3 - Поиск групп по запросу")
            print("4 - Выход")

            choice = input("Введите номер режима: ")

            if choice == '1':
                await send_messages(app)
            elif choice == '2':
                await collect_groups(app)
            elif choice == '3':
                keyword = input("Введите ключевое слово для поиска групп: ")
                await collect_groups_by_query_telethon(telethon_client, keyword)
            elif choice == '4':
                break
            else:
                print(Fore.RED + "Неверный выбор. Попробуйте снова." + Style.RESET_ALL)
    except Exception as e:
        print(f"Произошла ошибка: {e}")
    finally:
        await app.stop()
        await telethon_client.disconnect()
        restore_sleep_mode()  # Включить нормальный режим сна
        delete_session_files()  # Удаление файлов сессий

if __name__ == "__main__":
    asyncio.run(main())