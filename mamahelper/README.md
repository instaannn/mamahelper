# MamaHelper - Telegram Bot

Telegram бот для расчета доз лекарств для детей.

## Установка и настройка

1. Клонируйте репозиторий:
```bash
git clone <your-repo-url>
cd mamahelper
```

2. Создайте виртуальное окружение:
```bash
python -m venv venv
source venv/bin/activate  # На Windows: venv\Scripts\activate
```

3. Установите зависимости:
```bash
pip install -r requirements.txt
```

4. Создайте файл `.env` на основе `env.example`:
```bash
cp env.example .env
```

5. Отредактируйте файл `.env` и добавьте ваш Telegram Bot Token:
```
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
```

6. Получите токен бота:
   - Напишите @BotFather в Telegram
   - Создайте нового бота командой `/newbot`
   - Скопируйте полученный токен в файл `.env`

7. Запустите бота:
```bash
python app/main.py
```

## Безопасность

- Никогда не коммитьте файл `.env` в репозиторий
- Файл `.env` уже добавлен в `.gitignore`
- Используйте `env.example` как шаблон для других разработчиков

## Структура проекта

- `app/main.py` - основной файл бота
- `app/handlers/` - обработчики команд
- `app/data/` - данные (формуляры лекарств)
- `bot.py` - альтернативный простой бот


