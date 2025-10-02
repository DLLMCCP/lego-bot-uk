import os
import json
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from icalendar import Calendar, Event as ICalEvent
from pytz import timezone
import aiohttp
from bs4 import BeautifulSoup

# 日誌設置
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# 數據文件
USERS_FILE = 'users.json'
EVENTS_FILE = 'events.json'
ADMIN_IDS = []  # 填入你的 Telegram User ID

def load_data(filename):
    """載入 JSON 數據"""
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_data(filename, data):
    """保存 JSON 數據"""
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def create_ics_file(event: Dict) -> str:
    """生成 Google Calendar .ics 文件"""
    cal = Calendar()
    cal.add('prodid', '-//LEGO Events UK//EN')
    cal.add('version', '2.0')
    
    ical_event = ICalEvent()
    ical_event.add('summary', event['title'])
    ical_event.add('description', f"{event.get('description', '')}\n\n店舖: {event['store']}")
    ical_event.add('location', event['location'])
    
    # 解析日期
    try:
        event_date = datetime.fromisoformat(event['date'])
        uk_tz = timezone('Europe/London')
        event_date = uk_tz.localize(event_date)
        
        ical_event.add('dtstart', event_date)
        ical_event.add('dtend', event_date + timedelta(hours=2))
        
        # 添加提醒（活動前一天）
        alarm = ical_event.add('valarm')
        alarm.add('trigger', timedelta(days=-1))
        alarm.add('action', 'DISPLAY')
        alarm.add('description', '明天有 LEGO 免費活動！')
        
    except Exception as e:
        logger.error(f"日期解析錯誤: {e}")
        return None
    
    ical_event.add('uid', f"{event.get('id', 'unknown')}@legoeventsuk")
    cal.add_component(ical_event)
    
    return cal.to_ical().decode('utf-8')

async def scrape_lego_news() -> List[Dict]:
    """爬取 LEGO 活動新聞"""
    events = []
    try:
        async with aiohttp.ClientSession() as session:
            url = 'https://www.brickfanatics.com/tag/free-lego/'
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            
            async with session.get(url, headers=headers, timeout=10) as response:
                if response.status == 200:
                    html = await response.text()
                    soup = BeautifulSoup(html, 'html.parser')
                    
                    articles = soup.find_all('article', limit=5)
                    
                    for article in articles:
                        try:
                            title_elem = article.find('h2') or article.find('h3')
                            if not title_elem:
                                continue
                                
                            title = title_elem.get_text(strip=True)
                            
                            if 'free' not in title.lower() or 'lego' not in title.lower():
                                continue
                            
                            # 提取日期
                            date_match = re.search(r'(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s*(\d{4})?', title, re.IGNORECASE)
                            
                            event_date = None
                            if date_match:
                                day = date_match.group(1)
                                month = date_match.group(2)
                                year = date_match.group(3) or str(datetime.now().year)
                                event_date = f"{year}-{month}-{day}"
                            
                            # 判斷店舖類型
                            store = 'LEGO Store'
                            location = 'UK'
                            
                            if 'smyths' in title.lower():
                                store = 'Smyths Toys'
                                location = 'UK Smyths Stores'
                            elif 'john lewis' in title.lower():
                                store = 'John Lewis'
                            
                            # 提取城市
                            cities = ['London', 'Manchester', 'Birmingham', 'Liverpool', 'Glasgow', 'Leeds', 'Edinburgh']
                            for city in cities:
                                if city.lower() in title.lower():
                                    location = city
                                    break
                            
                            events.append({
                                'title': title,
                                'location': location,
                                'date': event_date or 'TBA',
                                'date_display': event_date or '待公布',
                                'store': store,
                                'description': '查看連結了解活動詳情',
                                'source': 'auto_scrape',
                                'url': article.find('a')['href'] if article.find('a') else ''
                            })
                            
                        except Exception as e:
                            logger.error(f"解析文章錯誤: {e}")
                            continue
                    
    except Exception as e:
        logger.error(f"爬取活動失敗: {e}")
    
    return events

async def auto_scrape_task(context: ContextTypes.DEFAULT_TYPE):
    """自動爬取任務（每6小時執行一次）"""
    logger.info("開始自動爬取活動...")
    
    all_new_events = await scrape_lego_news()
    
    if not all_new_events:
        logger.info("沒有發現新活動")
        return
    
    existing_events = load_data(EVENTS_FILE)
    existing_titles = {e['title'] for e in existing_events.values()}
    
    new_count = 0
    for event in all_new_events:
        if event['title'] not in existing_titles:
            event_id = f"auto_{len(existing_events) + new_count + 1}"
            event['id'] = event_id
            event['created_at'] = datetime.now().isoformat()
            existing_events[event_id] = event
            
            await broadcast_new_event(context, event)
            new_count += 1
            await asyncio.sleep(1)
    
    if new_count > 0:
        save_data(EVENTS_FILE, existing_events)
        logger.info(f"發現並添加了 {new_count} 個新活動")
    else:
        logger.info("沒有發現全新的活動")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理 /start 命令"""
    user_id = str(update.effective_user.id)
    users = load_data(USERS_FILE)
    
    if user_id not in users:
        users[user_id] = {
            'subscribed': True,
            'locations': [],
            'reminders': {}
        }
        save_data(USERS_FILE, users)
    
    welcome_text = """
🎉 歡迎使用 LEGO 免費活動追蹤 Bot！

✨ **功能**：
• 🤖 自動搜尋最新活動
• 📅 匯出到 Google Calendar
• 🔔 新活動通知

📍 支援店舖：
• LEGO Store
• Smyths Toys
• John Lewis

🔧 指令：
/list - 查看所有活動
/subscribe - 訂閱通知
/calendar - 匯出到日曆
/scrape - 立即搜尋活動
/help - 幫助

Bot 每 6 小時自動搜尋新活動！
"""
    await update.message.reply_text(welcome_text)

async def list_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """列出所有活動"""
    events = load_data(EVENTS_FILE)
    
    if not events:
        await update.message.reply_text(
            "🤷‍♂️ 暫時沒有活動。\n\n使用 /scrape 立即搜尋！"
        )
        return
    
    sorted_events = sorted(events.items(), key=lambda x: x[1].get('date', 'ZZZ'))
    
    message = "📅 **即將舉行的活動**\n\n"
    
    for event_id, event in sorted_events[:10]:
        message += f"🎪 *{event['title']}*\n"
        message += f"📍 {event['location']}\n"
        message += f"📅 {event['date_display']}\n"
        message += f"🏪 {event['store']}\n"
        
        if event.get('url'):
            message += f"🔗 [詳情]({event['url']})\n"
        
        message += f"📥 /export_{event_id}\n"
        message += "─" * 30 + "\n\n"
    
    if len(message) > 4000:
        chunks = [message[i:i+4000] for i in range(0, len(message), 4000)]
        for chunk in chunks:
            await update.message.reply_text(chunk, parse_mode='Markdown', disable_web_page_preview=True)
    else:
        await update.message.reply_text(message, parse_mode='Markdown', disable_web_page_preview=True)

async def export_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """匯出單個活動"""
    command = update.message.text
    event_id = command.replace('/export_', '')
    
    events = load_data(EVENTS_FILE)
    
    if event_id not in events:
        await update.message.reply_text("❌ 找不到這個活動！")
        return
    
    event = events[event_id]
    ics_content = create_ics_file(event)
    
    if not ics_content:
        await update.message.reply_text("❌ 無法生成日曆文件。")
        return
    
    await update.message.reply_document(
        document=ics_content.encode('utf-8'),
        filename=f"lego_event_{event_id}.ics",
        caption=f"📅 日曆文件已生成！\n\n點擊文件用 Google Calendar 打開",
        parse_mode='Markdown'
    )

async def export_all_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """匯出所有活動"""
    events = load_data(EVENTS_FILE)
    
    if not events:
        await update.message.reply_text("沒有活動可以匯出。")
        return
    
    cal = Calendar()
    cal.add('prodid', '-//LEGO Events UK//EN')
    cal.add('version', '2.0')
    
    count = 0
    for event_id, event in events.items():
        try:
            if event.get('date') == 'TBA':
                continue
                
            ical_event = ICalEvent()
            ical_event.add('summary', event['title'])
            ical_event.add('description', f"{event.get('description', '')}\n店舖: {event['store']}")
            ical_event.add('location', event['location'])
            
            event_date = datetime.fromisoformat(event['date'])
            uk_tz = timezone('Europe/London')
            event_date = uk_tz.localize(event_date)
            
            ical_event.add('dtstart', event_date)
            ical_event.add('dtend', event_date + timedelta(hours=2))
            ical_event.add('uid', f"{event_id}@legoeventsuk")
            
            cal.add_component(ical_event)
            count += 1
            
        except Exception as e:
            logger.error(f"添加活動失敗: {e}")
            continue
    
    if count == 0:
        await update.message.reply_text("沒有有效日期的活動。")
        return
    
    ics_content = cal.to_ical().decode('utf-8')
    
    await update.message.reply_document(
        document=ics_content.encode('utf-8'),
        filename="all_lego_events.ics",
        caption=f"📅 已匯出 {count} 個活動！",
        parse_mode='Markdown'
    )

async def manual_scrape(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """手動觸發爬取"""
    await update.message.reply_text("🔍 正在搜尋新活動...")
    
    await auto_scrape_task(context)
    
    events = load_data(EVENTS_FILE)
    await update.message.reply_text(
        f"✅ 搜尋完成！\n\n目前共有 {len(events)} 個活動。\n使用 /list 查看。"
    )

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """訂閱地區"""
    keyboard = [
        [InlineKeyboardButton("🏴󠁧󠁢󠁥󠁮󠁧󠁿 倫敦", callback_data='sub_london')],
        [InlineKeyboardButton("🏴󠁧󠁢󠁥󠁮󠁧󠁿 曼徹斯特", callback_data='sub_manchester')],
        [InlineKeyboardButton("🏴󠁧󠁢󠁥󠁮󠁧󠁿 伯明翰", callback_data='sub_birmingham')],
        [InlineKeyboardButton("✅ 訂閱所有", callback_data='sub_all')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text('選擇你的地區：', reply_markup=reply_markup)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理按鈕"""
    query = update.callback_query
    await query.answer()
    
    user_id = str(query.from_user.id)
    users = load_data(USERS_FILE)
    
    if user_id not in users:
        users[user_id] = {'subscribed': True, 'locations': [], 'reminders': {}}
    
    if query.data.startswith('sub_'):
        location = query.data.replace('sub_', '')
        
        if location == 'all':
            users[user_id]['locations'] = ['all']
            await query.edit_message_text('✅ 已訂閱所有地區！')
        else:
            if location not in users[user_id]['locations']:
                users[user_id]['locations'].append(location)
            await query.edit_message_text(f'✅ 已訂閱 {location.title()}！')
        
        save_data(USERS_FILE, users)

async def broadcast_new_event(context: ContextTypes.DEFAULT_TYPE, event):
    """廣播新活動"""
    users = load_data(USERS_FILE)
    
    message = f"""
🆕 **發現新活動！**

🎪 {event['title']}
📍 {event['location']}
📅 {event['date_display']}
🏪 {event['store']}
"""
    
    if event.get('url'):
        message += f"\n🔗 [查看詳情]({event['url']})"
    
    message += f"\n\n📥 /export_{event['id']}"
    
    for user_id in users:
        try:
            await context.bot.send_message(
                chat_id=int(user_id),
                text=message,
                parse_mode='Markdown',
                disable_web_page_preview=True
            )
            await asyncio.sleep(0.1)
        except Exception as e:
            logger.error(f"發送失敗: {e}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """幫助"""
    help_text = """
🤖 **Bot 使用指南**

**指令：**
/start - 開始使用
/list - 查看活動
/subscribe - 訂閱通知
/calendar - 匯出所有活動
/scrape - 立即搜尋
/help - 幫助

**自動化：**
每 6 小時自動搜尋新活動

**資料來源：**
• Brick Fanatics
• 官方公告
"""
    await update.message.reply_text(help_text, parse_mode='Markdown')

# 初始化
if not os.path.exists(USERS_FILE):
    save_data(USERS_FILE, {})
if not os.path.exists(EVENTS_FILE):
    save_data(EVENTS_FILE, {})

def main():
    """主函數"""
    # 在這裡直接填入你的 Token！
    TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')  # ← 把這裡改成你的 Token
    
    application = Application.builder().token(TOKEN).build()
    
    # 指令處理
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("list", list_events))
    application.add_handler(CommandHandler("subscribe", subscribe))
    application.add_handler(CommandHandler("calendar", export_all_events))
    application.add_handler(CommandHandler("scrape", manual_scrape))
    application.add_handler(CommandHandler("help", help_command))
    
    # 匯出活動處理
    async def handle_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.message.text.startswith('/export_'):
            await export_event(update, context)
    
    application.add_handler(MessageHandler(filters.Regex(r'^/export_'), handle_export))
    application.add_handler(CallbackQueryHandler(button_callback))
    
    # 定時任務（每6小時）
    job_queue = application.job_queue
    job_queue.run_repeating(auto_scrape_task, interval=21600, first=10)
    
    print("🤖 Bot 已啟動！")
    print("✅ 自動爬取已啟用")
    print("✅ Google Calendar 已啟用")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
