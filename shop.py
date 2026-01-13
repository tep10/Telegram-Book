import logging
import sqlite3
import pandas as pd
from datetime import datetime, timedelta
from telegram import (
    Update, 
    InlineKeyboardButton, 
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes
)
import os
from functools import wraps
import requests
from io import BytesIO
import math

# ========== CONFIGURATION ==========
BOT_TOKEN = "8502848831:AAG184UsX7tirVtPSCsAcjzPBN8_t4PQ42E"  # Your bot token
ADMIN_ID = 1273972944  # Your Telegram ID

# Products with prices in USD
PRODUCTS = {
    "math_book": {"name": "Math Book", "price": 1.70, "emoji": "📐"},
    "human_society": {"name": "Human & Society", "price": 1.99, "emoji": "👥"},
    "business": {"name": "Principle of Business", "price": 1.99, "emoji": "💼"},
    "computer": {"name": "Computer Book", "price": 2.50, "emoji": "💻"},
}

# Payment URLs
KHQR_URL = "https://files.catbox.moe/0cofqs.jpg"
ABA_PAY_URL = "https://pay.ababank.com/oRF8/7y7y1tha"
DEVELOPER_USERNAME = "@tephh"

# Pagination settings
ORDERS_PER_PAGE = 10
USERS_PER_PAGE = 15

# Conversation states
NAME, GROUP, PHONE, QUANTITY, CONFIRMATION, PAYMENT = range(6)

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ========== DATABASE FUNCTIONS ==========
def init_db():
    """Initialize SQLite database"""
    conn = sqlite3.connect('bookshop.db')
    c = conn.cursor()
    
    # Create users table
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY, 
                  username TEXT,
                  first_name TEXT,
                  last_name TEXT,
                  phone TEXT,
                  group_name TEXT,
                  registration_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                  total_orders INTEGER DEFAULT 0,
                  total_spent REAL DEFAULT 0)''')
    
    # Create orders table
    c.execute('''CREATE TABLE IF NOT EXISTS orders
                 (order_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER,
                  product_name TEXT,
                  quantity INTEGER,
                  total_price REAL,
                  status TEXT DEFAULT 'pending',
                  payment_method TEXT,
                  payment_proof TEXT,
                  order_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                  admin_notes TEXT,
                  FOREIGN KEY (user_id) REFERENCES users (user_id))''')
    
    # Create indexes for faster queries
    c.execute('''CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)''')
    c.execute('''CREATE INDEX IF NOT EXISTS idx_orders_date ON orders(order_date)''')
    c.execute('''CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id)''')
    
    # Create products table
    c.execute('''CREATE TABLE IF NOT EXISTS products
                 (product_id TEXT PRIMARY KEY,
                  name TEXT,
                  price REAL,
                  emoji TEXT,
                  stock INTEGER DEFAULT 100,
                  total_sold INTEGER DEFAULT 0)''')
    
    # Insert products if not exists
    for pid, info in PRODUCTS.items():
        c.execute('''INSERT OR IGNORE INTO products (product_id, name, price, emoji) 
                     VALUES (?, ?, ?, ?)''', 
                  (pid, info['name'], info['price'], info.get('emoji', '📚')))
    
    conn.commit()
    conn.close()

def add_user(user_id, username, first_name, last_name):
    """Add or update user in database"""
    conn = sqlite3.connect('bookshop.db')
    c = conn.cursor()
    c.execute('''INSERT OR IGNORE INTO users 
                 (user_id, username, first_name, last_name) 
                 VALUES (?, ?, ?, ?)''',
              (user_id, username, first_name, last_name))
    conn.commit()
    conn.close()

def update_user_info(user_id, group_name, phone):
    """Update user's group and phone"""
    conn = sqlite3.connect('bookshop.db')
    c = conn.cursor()
    c.execute('''UPDATE users SET group_name = ?, phone = ? 
                 WHERE user_id = ?''',
              (group_name, phone, user_id))
    conn.commit()
    conn.close()

def create_order(user_id, product_name, quantity, total_price):
    """Create a new order"""
    conn = sqlite3.connect('bookshop.db')
    c = conn.cursor()
    
    # Create order
    c.execute('''INSERT INTO orders 
                 (user_id, product_name, quantity, total_price, status) 
                 VALUES (?, ?, ?, ?, 'pending')''',
              (user_id, product_name, quantity, total_price))
    order_id = c.lastrowid
    
    # Update user stats
    c.execute('''UPDATE users SET 
                 total_orders = total_orders + 1,
                 total_spent = total_spent + ?
                 WHERE user_id = ?''',
              (total_price, user_id))
    
    # Update product stats
    c.execute('''UPDATE products SET 
                 total_sold = total_sold + ?
                 WHERE name = ?''',
              (quantity, product_name))
    
    conn.commit()
    conn.close()
    return order_id

def update_order_payment(order_id, payment_method, payment_proof=None):
    """Update order with payment information"""
    conn = sqlite3.connect('bookshop.db')
    c = conn.cursor()
    c.execute('''UPDATE orders SET payment_method = ?, payment_proof = ?, status = 'awaiting_verification'
                 WHERE order_id = ?''',
              (payment_method, payment_proof, order_id))
    conn.commit()
    conn.close()

def get_orders_count(status_filter=None, date_filter=None):
    """Get total count of orders with filters"""
    conn = sqlite3.connect('bookshop.db')
    c = conn.cursor()
    
    query = "SELECT COUNT(*) FROM orders WHERE 1=1"
    params = []
    
    if status_filter and status_filter != 'all':
        query += " AND status = ?"
        params.append(status_filter)
    
    if date_filter:
        if date_filter == 'today':
            query += " AND date(order_date) = date('now')"
        elif date_filter == 'week':
            query += " AND order_date >= date('now', '-7 days')"
        elif date_filter == 'month':
            query += " AND order_date >= date('now', '-30 days')"
    
    c.execute(query, params)
    count = c.fetchone()[0]
    conn.close()
    return count

def get_orders_paginated(page=1, status_filter=None, date_filter=None, search_query=None):
    """Get orders with pagination and filters"""
    conn = sqlite3.connect('bookshop.db')
    c = conn.cursor()
    
    offset = (page - 1) * ORDERS_PER_PAGE
    
    # Base query
    query = '''SELECT o.order_id, u.first_name, u.group_name, u.phone, 
                      o.product_name, o.quantity, o.total_price, o.status, 
                      o.payment_method, o.order_date, o.admin_notes
               FROM orders o
               JOIN users u ON o.user_id = u.user_id
               WHERE 1=1'''
    params = []
    
    # Apply filters
    if status_filter and status_filter != 'all':
        query += " AND o.status = ?"
        params.append(status_filter)
    
    if date_filter:
        if date_filter == 'today':
            query += " AND date(o.order_date) = date('now')"
        elif date_filter == 'week':
            query += " AND o.order_date >= date('now', '-7 days')"
        elif date_filter == 'month':
            query += " AND o.order_date >= date('now', '-30 days')"
    
    if search_query:
        query += ''' AND (o.order_id LIKE ? OR u.first_name LIKE ? OR 
                         u.group_name LIKE ? OR o.product_name LIKE ?)'''
        search_param = f"%{search_query}%"
        params.extend([search_param, search_param, search_param, search_param])
    
    # Order and pagination
    query += " ORDER BY o.order_date DESC LIMIT ? OFFSET ?"
    params.extend([ORDERS_PER_PAGE, offset])
    
    c.execute(query, params)
    orders = c.fetchall()
    conn.close()
    return orders

def get_order_details(order_id):
    """Get specific order details"""
    conn = sqlite3.connect('bookshop.db')
    c = conn.cursor()
    c.execute('''SELECT o.*, u.first_name, u.group_name, u.phone, u.username
                 FROM orders o
                 JOIN users u ON o.user_id = u.user_id
                 WHERE o.order_id = ?''', (order_id,))
    order = c.fetchone()
    conn.close()
    return order

def update_order_status(order_id, status, notes=None):
    """Update order status"""
    conn = sqlite3.connect('bookshop.db')
    c = conn.cursor()
    if notes:
        c.execute('''UPDATE orders SET status = ?, admin_notes = ? WHERE order_id = ?''',
                  (status, notes, order_id))
    else:
        c.execute('''UPDATE orders SET status = ? WHERE order_id = ?''',
                  (status, order_id))
    conn.commit()
    
    # Get user_id for notification
    c.execute('''SELECT user_id FROM orders WHERE order_id = ?''', (order_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else None

def get_user_orders(user_id):
    """Get orders for a specific user"""
    conn = sqlite3.connect('bookshop.db')
    c = conn.cursor()
    c.execute('''SELECT order_id, product_name, quantity, total_price, status, 
                        payment_method, order_date, admin_notes
                 FROM orders 
                 WHERE user_id = ?
                 ORDER BY order_date DESC''',
              (user_id,))
    orders = c.fetchall()
    conn.close()
    return orders

def get_users_paginated(page=1, search_query=None):
    """Get users with pagination"""
    conn = sqlite3.connect('bookshop.db')
    c = conn.cursor()
    
    offset = (page - 1) * USERS_PER_PAGE
    
    query = '''SELECT user_id, first_name, group_name, phone, 
                      registration_date, total_orders, total_spent
               FROM users WHERE 1=1'''
    params = []
    
    if search_query:
        query += " AND (first_name LIKE ? OR group_name LIKE ? OR phone LIKE ?)"
        search_param = f"%{search_query}%"
        params.extend([search_param, search_param, search_param])
    
    query += " ORDER BY registration_date DESC LIMIT ? OFFSET ?"
    params.extend([USERS_PER_PAGE, offset])
    
    c.execute(query, params)
    users = c.fetchall()
    conn.close()
    return users

def get_users_count(search_query=None):
    """Get total count of users"""
    conn = sqlite3.connect('bookshop.db')
    c = conn.cursor()
    
    query = "SELECT COUNT(*) FROM users"
    params = []
    
    if search_query:
        query += " WHERE first_name LIKE ? OR group_name LIKE ? OR phone LIKE ?"
        search_param = f"%{search_query}%"
        params.extend([search_param, search_param, search_param])
    
    c.execute(query, params)
    count = c.fetchone()[0]
    conn.close()
    return count

def export_to_excel(status_filter=None, date_filter=None):
    """Export orders to Excel file with filters"""
    conn = sqlite3.connect('bookshop.db')
    c = conn.cursor()
    
    query = '''SELECT o.order_id, u.first_name, u.group_name, u.phone, 
                      o.product_name, o.quantity, o.total_price, o.status, 
                      o.payment_method, o.order_date, o.admin_notes
               FROM orders o
               JOIN users u ON o.user_id = u.user_id
               WHERE 1=1'''
    params = []
    
    if status_filter and status_filter != 'all':
        query += " AND o.status = ?"
        params.append(status_filter)
    
    if date_filter:
        if date_filter == 'today':
            query += " AND date(o.order_date) = date('now')"
        elif date_filter == 'week':
            query += " AND o.order_date >= date('now', '-7 days')"
        elif date_filter == 'month':
            query += " AND o.order_date >= date('now', '-30 days')"
    
    query += " ORDER BY o.order_date DESC"
    
    c.execute(query, params)
    orders = c.fetchall()
    conn.close()
    
    df = pd.DataFrame(orders, columns=[
        'Order ID', 'Name', 'Group', 'Phone', 
        'Product', 'Quantity', 'Total Price', 
        'Status', 'Payment Method', 'Order Date', 'Admin Notes'
    ])
    filename = f'orders_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'
    df.to_excel(filename, index=False)
    return filename

def get_statistics():
    """Get shop statistics"""
    conn = sqlite3.connect('bookshop.db')
    c = conn.cursor()
    
    # Total orders
    c.execute('''SELECT COUNT(*) FROM orders''')
    total_orders = c.fetchone()[0]
    
    # Orders by status
    c.execute('''SELECT status, COUNT(*) FROM orders GROUP BY status''')
    status_counts = dict(c.fetchall())
    
    # Total revenue
    c.execute('''SELECT SUM(total_price) FROM orders WHERE status = 'completed' ''')
    revenue = c.fetchone()[0] or 0
    
    # Total users
    c.execute('''SELECT COUNT(*) FROM users''')
    total_users = c.fetchone()[0]
    
    # Today's orders
    c.execute('''SELECT COUNT(*) FROM orders WHERE date(order_date) = date('now')''')
    today_orders = c.fetchone()[0]
    
    # Today's revenue
    c.execute('''SELECT SUM(total_price) FROM orders WHERE date(order_date) = date('now') AND status = 'completed' ''')
    today_revenue = c.fetchone()[0] or 0
    
    # Product sales
    c.execute('''SELECT product_name, SUM(quantity) as total_sold 
                 FROM orders WHERE status = 'completed' 
                 GROUP BY product_name ORDER BY total_sold DESC''')
    product_sales = c.fetchall()
    
    conn.close()
    
    return {
        'total_orders': total_orders,
        'status_counts': status_counts,
        'revenue': revenue,
        'total_users': total_users,
        'today_orders': today_orders,
        'today_revenue': today_revenue,
        'product_sales': product_sales
    }

# ========== ADMIN DECORATOR ==========
def admin_only(func):
    """Decorator to restrict access to admin only"""
    @wraps(func)
    async def wrapped(update, context, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id != ADMIN_ID:
            await update.message.reply_text("⚠️ អ្នកគ្មានសិទ្ធិប្រើប្រាស់ផ្នែកនេះទេ!")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

# ========== KEYBOARD GENERATORS ==========
def get_main_keyboard():
    """Main menu keyboard"""
    keyboard = [
        ["📚 ទិញសៀវភៅ", "📋 តាមដានការកម្មង"],
        ["❓ Q&A", "👤 អំពីយើង"],
        ["👑 Admin Panel"] if ADMIN_ID else []
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_product_keyboard():
    """Product selection keyboard"""
    keyboard = []
    row = []
    for pid, info in PRODUCTS.items():
        emoji = info.get('emoji', '📚')
        row.append(
            InlineKeyboardButton(
                f"{emoji} {info['name']}", 
                callback_data=f"product_{pid}"
            )
        )
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    
    keyboard.append([
        InlineKeyboardButton("💰 មើលតម្លៃទាំងអស់", callback_data="view_all_prices")
    ])
    keyboard.append([
        InlineKeyboardButton("🔙 ត្រឡប់មេនុយ", callback_data="back_to_main")
    ])
    return InlineKeyboardMarkup(keyboard)

def get_quantity_keyboard():
    """Quantity selection keyboard"""
    keyboard = [
        [
            InlineKeyboardButton("1", callback_data="qty_1"),
            InlineKeyboardButton("2", callback_data="qty_2"),
            InlineKeyboardButton("3", callback_data="qty_3"),
        ],
        [
            InlineKeyboardButton("4", callback_data="qty_4"),
            InlineKeyboardButton("5", callback_data="qty_5"),
            InlineKeyboardButton("6", callback_data="qty_6"),
        ],
        [
            InlineKeyboardButton("7", callback_data="qty_7"),
            InlineKeyboardButton("8", callback_data="qty_8"),
            InlineKeyboardButton("9", callback_data="qty_9"),
        ],
        [
            InlineKeyboardButton("10+", callback_data="qty_custom"),
            InlineKeyboardButton("🔙 ត្រឡប់", callback_data="back_to_products")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_payment_keyboard(order_id):
    """Payment options keyboard with order_id"""
    keyboard = [
        [
            InlineKeyboardButton("📸 ទូទាត់តាម KHQR", callback_data=f"pay_khqr_{order_id}"),
        ],
        [
            InlineKeyboardButton("🏦 ទូទាត់តាម ABA", url=ABA_PAY_URL),
            InlineKeyboardButton("💵 ទូទាត់នៅថ្នាក់", callback_data=f"pay_cash_{order_id}")
        ],
        [
            InlineKeyboardButton("📱 ផ្ញើ screenshot ទូទាត់", callback_data=f"upload_proof_{order_id}"),
        ],
        [
            InlineKeyboardButton("🔙 ត្រឡប់មេនុយ", callback_data="back_to_main"),
            InlineKeyboardButton("📞 ទំនាក់ទំនង", url=f"https://t.me/{DEVELOPER_USERNAME[1:]}")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_confirmation_keyboard():
    """Order confirmation keyboard - NO CANCEL BUTTON"""
    keyboard = [
        [
            InlineKeyboardButton("✅ បញ្ជាក់ការកម្មង", callback_data="confirm_order"),
        ],
        [
            InlineKeyboardButton("✏️ កែសម្រួលព័ត៌មាន", callback_data="edit_order")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_admin_keyboard():
    """Admin panel keyboard"""
    keyboard = [
        ["📊 ស្ថិតិ", "📋 មើលការកម្មងទាំងអស់"],
        ["⏳ ការកម្មងរង់ចាំ", "📸 ពិនិត្យ screenshot"],
        ["📥 ទាញយក Excel", "👥 មើលអ្នកប្រើប្រាស់"],
        ["🔙 មេនុយចម្បង"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_admin_orders_filter_keyboard():
    """Filter keyboard for orders"""
    keyboard = [
        [
            InlineKeyboardButton("📋 ទាំងអស់", callback_data="filter_all"),
            InlineKeyboardButton("⏳ រង់ចាំ", callback_data="filter_pending"),
            InlineKeyboardButton("📸 ពិនិត្យ", callback_data="filter_awaiting_verification")
        ],
        [
            InlineKeyboardButton("✅ បញ្ចប់", callback_data="filter_completed"),
            InlineKeyboardButton("❌ បដិសេធ", callback_data="filter_rejected"),
            InlineKeyboardButton("💰 ថ្ងៃនេះ", callback_data="filter_today")
        ],
        [
            InlineKeyboardButton("📅 សប្តាហ៍នេះ", callback_data="filter_week"),
            InlineKeyboardButton("📅 ខែនេះ", callback_data="filter_month"),
            InlineKeyboardButton("🔍 ស្វែងរក", callback_data="admin_search")
        ],
        [
            InlineKeyboardButton("📊 ស្ថិតិ", callback_data="admin_stats"),
            InlineKeyboardButton("🔙 ត្រឡប់", callback_data="admin_back")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_admin_order_keyboard(order_id, page=1, status_filter='all', date_filter=None):
    """Admin order action keyboard"""
    keyboard = [
        [
            InlineKeyboardButton("✅ បញ្ជាក់ការទូទាត់", callback_data=f"admin_confirm_{order_id}"),
            InlineKeyboardButton("❌ បដិសេធ", callback_data=f"admin_reject_{order_id}")
        ],
        [
            InlineKeyboardButton("📞 ទំនាក់ទំនងអ្នកទិញ", callback_data=f"admin_contact_{order_id}"),
            InlineKeyboardButton("💰 បញ្ចប់ការកម្មង", callback_data=f"admin_complete_{order_id}")
        ],
        [
            InlineKeyboardButton("📝 បន្ថែមចំណាំ", callback_data=f"admin_note_{order_id}"),
            InlineKeyboardButton("🔙 ត្រឡប់", callback_data=f"admin_orders_{page}_{status_filter}_{date_filter or 'none'}")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_pagination_keyboard(page, total_pages, action_prefix, current_filter='all', date_filter=None, search_query=None):
    """Generate pagination keyboard"""
    keyboard = []
    
    # Navigation buttons
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("⬅️ មុន", callback_data=f"{action_prefix}_{page-1}_{current_filter}_{date_filter or 'none'}_{search_query or 'none'}"))
    
    nav_row.append(InlineKeyboardButton(f"📄 {page}/{total_pages}", callback_data="noop"))
    
    if page < total_pages:
        nav_row.append(InlineKeyboardButton("បន្ទាប់ ➡️", callback_data=f"{action_prefix}_{page+1}_{current_filter}_{date_filter or 'none'}_{search_query or 'none'}"))
    
    if nav_row:
        keyboard.append(nav_row)
    
    # Page jump buttons for many pages
    if total_pages > 5:
        page_buttons = []
        for p in range(max(1, page-2), min(total_pages, page+2) + 1):
            if p == page:
                page_buttons.append(InlineKeyboardButton(f"•{p}•", callback_data="noop"))
            else:
                page_buttons.append(InlineKeyboardButton(str(p), callback_data=f"{action_prefix}_{p}_{current_filter}_{date_filter or 'none'}_{search_query or 'none'}"))
        keyboard.append(page_buttons)
    
    # Filter buttons
    keyboard.append([
        InlineKeyboardButton("🔍 ស្វែងរក", callback_data="admin_search"),
        InlineKeyboardButton("📥 បញ្ចេញ", callback_data=f"admin_export_{current_filter}_{date_filter or 'none'}")
    ])
    
    keyboard.append([
        InlineKeyboardButton("📊 ស្ថិតិ", callback_data="admin_stats"),
        InlineKeyboardButton("🔙 ត្រឡប់", callback_data="admin_back")
    ])
    
    return InlineKeyboardMarkup(keyboard)

# ========== COMMAND HANDLERS ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    user = update.effective_user
    add_user(user.id, user.username, user.first_name, user.last_name)
    
    welcome_text = """🎉 **សូមស្វាគមន៍មកកាន់ហាងសៀវភៅរបស់យើង!**

📚 **សៀវភៅដែលមាន:**
📐 Math Book - $1.70
👥 Human & Society - $1.99
💼 Principle of Business - $1.99
💻 Computer Book - $2.50

⚠️ **ព្រមាន:**
- គ្មានការសងប្រាក់វិញសម្រាប់ការទូទាត់ក្លែងក្លាយ
- បន្ទាប់ពីទូទាត់ សូមផ្ញើ **screenshot ទូទាត់** មកយើង

ជ្រើសរើសពីម៉ឺនុយខាងក្រោម! 👇"""
    
    await update.message.reply_text(welcome_text, 
                                   reply_markup=get_main_keyboard(),
                                   parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
    help_text = """❓ **សំណួរធម្មតា**

**របៀបបញ្ជាទិញ:**
1️⃣ ចុច "📚 ទិញសៀវភៅ"
2️⃣ ជ្រើសរើសសៀវភៅដែលបងចង់ទិញ
3️⃣ បំពេញឈ្មោះ ក្រុម និងលេខទូរស័ព្ទ
4️⃣ ជ្រើសរើសចំនួន
5️⃣ ជ្រើសរើសវិធីទូទាត់

**វិធីទូទាត់:**
💰 **KHQR**: ស្កេនកូដ QR
🏦 **ABA Pay**: ចុចតំណភ្ជាប់
💵 **ទូទាត់នៅថ្នាក់**: សម្រាប់អ្នកដែលគ្មានធនាគារ

**ការតាមដាន:** 📋
បងអាចតាមដានស្ថានភាពការកម្មងរបស់បងនៅពេលណាក៏បាន

**ទំនាក់ទំនងអ្នកអភិវឌ្ឍ:** 👨‍💻
""" + DEVELOPER_USERNAME + """

**ចំណាំ:** បន្ទាប់ពីទូទាត់ សូមផ្ញើ **screenshot ទូទាត់** មកយើង"""

    await update.message.reply_text(help_text, parse_mode='Markdown')

# ========== MAIN MENU HANDLERS ==========
async def handle_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle main menu buttons"""
    text = update.message.text
    
    if text == "📚 ទិញសៀវភៅ":
        # Show prices first
        price_text = "💰 **តម្លៃសៀវភៅ:**\n\n"
        for pid, info in PRODUCTS.items():
            emoji = info.get('emoji', '📚')
            price_text += f"{emoji} **{info['name']}**: ${info['price']:.2f}\n"
        
        price_text += "\nចុចប៊ូតុងខាងក្រោមដើម្បីជ្រើសរើសសៀវភៅ:"
        
        keyboard = [
            [InlineKeyboardButton("📚 ជ្រើសរើសសៀវភៅ", callback_data="choose_product")],
            [InlineKeyboardButton("🔙 ត្រឡប់", callback_data="back_to_main")]
        ]
        
        await update.message.reply_text(
            price_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        
    elif text == "📋 តាមដានការកម្មង":
        await track_orders(update, context)
    elif text == "❓ Q&A":
        await help_command(update, context)
    elif text == "👤 អំពីយើង":
        about_text = f"""🏫 **ហាងសៀវភៅសម្រាប់មិត្តរួមថ្នាក់**

យើងជួយបោះពុម្ពសៀវភៅសម្រាប់ការសិក្សាដោយតម្លៃសមរម្យ និងគុណភាពល្អ។

**ព័ត៌មានទំនាក់ទំនង:**
👨‍💻 អ្នកអភិវឌ្ឍ: {DEVELOPER_USERNAME}
📧 ទំនាក់ទំនង: តាមរយៈ Telegram

**ព្រមាន:** ⚠️
- គ្មានការសងប្រាក់វិញ
- ផ្ញើ **screenshot ទូទាត់** ជូនអ្នកគ្រប់គ្រង"""
        await update.message.reply_text(about_text, parse_mode='Markdown')
    elif text == "👑 Admin Panel" and update.effective_user.id == ADMIN_ID:
        await admin_panel(update, context)

# ========== ORDER PROCESSING ==========
async def select_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle product selection"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "choose_product":
        await query.edit_message_text(
            "📚 **សូមជ្រើសរើសសៀវភៅដែលបងចង់ទិញ:**\n\n"
            "ចុចលើសៀវភៅដែលបងចង់ទិញ:",
            reply_markup=get_product_keyboard(),
            parse_mode='Markdown'
        )
    
    elif query.data == "view_all_prices":
        price_text = "💰 **តម្លៃសៀវភៅទាំងអស់:**\n\n"
        for pid, info in PRODUCTS.items():
            emoji = info.get('emoji', '📚')
            price_text += f"{emoji} **{info['name']}**: ${info['price']:.2f}\n"
        
        keyboard = [
            [InlineKeyboardButton("📚 ជ្រើសរើសសៀវភៅ", callback_data="choose_product")],
            [InlineKeyboardButton("🔙 ត្រឡប់", callback_data="back_to_main")]
        ]
        
        await query.edit_message_text(
            price_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    
    elif query.data.startswith("product_"):
        product_id = query.data.split("_")[1]
        product = PRODUCTS[product_id]
        
        context.user_data['product_id'] = product_id
        context.user_data['product_name'] = product['name']
        context.user_data['price'] = product['price']
        context.user_data['product_emoji'] = product.get('emoji', '📚')
        
        # Ask for name
        await query.edit_message_text(
            f"{product['emoji']} **បានជ្រើសរើស: {product['name']}**\n"
            f"💰 តម្លៃ: ${product['price']:.2f}\n\n"
            f"📝 **សូមបំពេញព័ត៌មានសម្រាប់ការកម្មង**\n\n"
            f"សូមវាយបញ្ចូលឈ្មោះពេញរបស់បង:",
            parse_mode='Markdown'
        )
        return NAME
    
    elif query.data == "back_to_main":
        await query.edit_message_text(
            "ជម្រើសដែលមាន:",
            reply_markup=get_main_keyboard()
        )
        return ConversationHandler.END

async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get user's name"""
    context.user_data['name'] = update.message.text
    await update.message.reply_text(
        "👥 **សូមវាយបញ្ចូលក្រុមរបស់បង**\n\n"
        "ឧទាហរណ៍: Civil M3, M4, A1, B2, ជាដើម...",
        parse_mode='Markdown'
    )
    return GROUP

async def get_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get user's group"""
    context.user_data['group'] = update.message.text
    await update.message.reply_text(
        "📞 **សូមវាយបញ្ចូលលេខទូរស័ព្ទរបស់បង**\n\n"
        "ឬចុច /skip ដើម្បីរំលង\n"
        "(លេខទូរស័ព្ទជួយក្នុងការទំនាក់ទំនងបើមានបញ្ហា)",
        parse_mode='Markdown'
    )
    return PHONE

async def get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get user's phone number"""
    if update.message.text != "/skip":
        context.user_data['phone'] = update.message.text
    else:
        context.user_data['phone'] = "មិនបានបញ្ជាក់"
    
    # Save user info
    update_user_info(
        update.effective_user.id,
        context.user_data['group'],
        context.user_data['phone']
    )
    
    # Ask for quantity
    await update.message.reply_text(
        f"🔢 **សូមជ្រើសរើសចំនួនសៀវភៅដែលបងចង់ទិញ**\n\n"
        f"{context.user_data['product_emoji']} សៀវភៅ: {context.user_data['product_name']}\n"
        f"💰 តម្លៃក្នុងមួយ: ${context.user_data['price']:.2f}",
        reply_markup=get_quantity_keyboard(),
        parse_mode='Markdown'
    )
    return QUANTITY

async def select_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quantity selection"""
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("qty_"):
        if query.data == "qty_custom":
            await query.edit_message_text(
                "🔢 **សូមវាយបញ្ចូលចំនួនសៀវភៅដែលបងចង់ទិញ:**\n\n"
                "(វាយបញ្ចូលតែលេខប៉ុណ្ណោះ ឧទាហរណ៍: 2, 5, 10, ...)",
                parse_mode='Markdown'
            )
            return QUANTITY
        
        quantity = int(query.data.split("_")[1])
        context.user_data['quantity'] = quantity
        await show_order_summary(query, context)
        return CONFIRMATION
    
    elif query.data == "back_to_products":
        await query.edit_message_text(
            "📚 សូមជ្រើសរើសសៀវភៅ:",
            reply_markup=get_product_keyboard()
        )
        return ConversationHandler.END

async def get_custom_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get custom quantity"""
    try:
        quantity = int(update.message.text)
        if quantity < 1:
            await update.message.reply_text("❌ សូមវាយបញ្ចូលលេខធំជាង 0")
            return QUANTITY
        if quantity > 50:
            await update.message.reply_text("❌ ចំនួនច្រើនពេក សូមទំនាក់ទំនងអ្នកគ្រប់គ្រង")
            return QUANTITY
            
        context.user_data['quantity'] = quantity
        await show_order_summary_message(update, context)
        return CONFIRMATION
    except ValueError:
        await update.message.reply_text("❌ សូមវាយបញ្ចូលលេខដែលត្រឹមត្រូវ (ឧទាហរណ៍: 1, 2, 3, ...)")
        return QUANTITY

async def show_order_summary_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show order summary for message updates"""
    product_name = context.user_data['product_name']
    price = context.user_data['price']
    quantity = context.user_data['quantity']
    total = price * quantity
    
    summary = f"""📋 **សង្ខេបការកម្មង:**

{context.user_data.get('product_emoji', '📚')} **សៀវភៅ:** {product_name}
👤 **ឈ្មោះ:** {context.user_data['name']}
👥 **ក្រុម:** {context.user_data['group']}
📞 **ទូរស័ព្ទ:** {context.user_data['phone']}
🔢 **ចំនួន:** {quantity}
💰 **តម្លៃសរុប:** ${total:.2f}

**តើបងពិតជាចង់បញ្ជាទិញមែនទេ?**"""
    
    await update.message.reply_text(
        summary,
        reply_markup=get_confirmation_keyboard(),
        parse_mode='Markdown'
    )

async def show_order_summary(query, context: ContextTypes.DEFAULT_TYPE):
    """Show order summary for callback queries"""
    product_name = context.user_data['product_name']
    price = context.user_data['price']
    quantity = context.user_data['quantity']
    total = price * quantity
    
    summary = f"""📋 **សង្ខេបការកម្មង:**

{context.user_data.get('product_emoji', '📚')} **សៀវភៅ:** {product_name}
👤 **ឈ្មោះ:** {context.user_data['name']}
👥 **ក្រុម:** {context.user_data['group']}
📞 **ទូរស័ព្ទ:** {context.user_data['phone']}
🔢 **ចំនួន:** {quantity}
💰 **តម្លៃសរុប:** ${total:.2f}

**តើបងពិតជាចង់បញ្ជាទិញមែនទេ?**"""
    
    await query.edit_message_text(
        summary,
        reply_markup=get_confirmation_keyboard(),
        parse_mode='Markdown'
    )

async def confirm_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle order confirmation"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "confirm_order":
        # Create order in database
        order_id = create_order(
            update.effective_user.id,
            context.user_data['product_name'],
            context.user_data['quantity'],
            context.user_data['price'] * context.user_data['quantity']
        )
        
        context.user_data['order_id'] = order_id
        
        # Send KHQR image
        try:
            # Download KHQR image
            response = requests.get(KHQR_URL)
            if response.status_code == 200:
                photo = BytesIO(response.content)
                photo.name = 'khqr_payment.jpg'
                
                caption = f"""📸 **KHQR សម្រាប់ទូទាត់**

លេខកូដកម្មង: **#{order_id}**
តម្លៃសរុប: **${context.user_data['price'] * context.user_data['quantity']:.2f}**

**សូមស្កេនកូដ QR ខាងលើដើម្បីទូទាត់**
ឬចុចតំណភ្ជាប់ ABA Pay ខាងក្រោម👇"""
                
                await query.message.reply_photo(
                    photo=photo,
                    caption=caption,
                    reply_markup=get_payment_keyboard(order_id),
                    parse_mode='Markdown'
                )
                
                # Send payment instructions separately
                payment_text = f"""💰 **ព័ត៌មានបន្ថែមសម្រាប់ការទូទាត់:**

1️⃣ **KHQR** (រូបខាងលើ): ស្កេនកូដ QR តាមម៉ាស៊ីនអេធីអឹម ឬទូរស័ព្ទ
2️⃣ **ABA Pay**: [ចុចទីនេះដើម្បីទូទាត់តាម ABA]({ABA_PAY_URL})
3️⃣ **ទូទាត់នៅថ្នាក់**: សម្រាប់អ្នកដែលគ្មានធនាគារ

⚠️ **ព្រមានសំខាន់:**
- បន្ទាប់ពីទូទាត់ សូមផ្ញើ **screenshot ទូទាត់** មកយើង
- គ្មានការសងប្រាក់វិញសម្រាប់ការទូទាត់ក្លែងក្លាយ
- ប្រាក់នឹងត្រូវបានផ្ទេរជូនអ្នកគ្រប់គ្រង

លេខកូដកម្មងរបស់បង: **#{order_id}**
តម្លៃសរុប: **${context.user_data['price'] * context.user_data['quantity']:.2f}**

**សូមផ្ញើ screenshot ទូទាត់មកយើង បន្ទាប់ពីបង់ប្រាក់!**"""
                
                await query.message.reply_text(
                    payment_text,
                    reply_markup=get_payment_keyboard(order_id),
                    parse_mode='Markdown',
                    disable_web_page_preview=True
                )
        except Exception as e:
            logger.error(f"Error sending KHQR: {e}")
            # Fallback to text if image fails
            payment_text = f"""💰 **វិធីទូទាត់:**

1️⃣ **KHQR**: {KHQR_URL}
2️⃣ **ABA Pay**: [ចុចទីនេះ]({ABA_PAY_URL})
3️⃣ **ទូទាត់នៅថ្នាក់**

លេខកូដកម្មង: **#{order_id}**

**សូមផ្ញើ screenshot ទូទាត់មកយើង!**"""
            
            await query.edit_message_text(
                payment_text,
                reply_markup=get_payment_keyboard(order_id),
                parse_mode='Markdown'
            )
        
        # Notify admin
        await notify_admin_new_order(context, order_id)
        
        return ConversationHandler.END
    
    elif query.data == "edit_order":
        await query.edit_message_text(
            "✏️ តើបងចង់កែសម្រួលអ្វី?\n\n"
            "សូមវាយបញ្ចូលឈ្មោះថ្មី:",
            parse_mode='Markdown'
        )
        # Set state to edit name
        context.user_data['editing'] = 'name'
        return NAME

async def notify_admin_new_order(context, order_id):
    """Notify admin about new order"""
    try:
        admin_text = f"""🛎️ **ការកម្មងថ្មី!**

📋 **លេខកូដ:** #{order_id}
👤 **អ្នកទិញ:** {context.user_data['name']}
👥 **ក្រុម:** {context.user_data['group']}
📞 **ទូរស័ព្ទ:** {context.user_data['phone']}
📚 **សៀវភៅ:** {context.user_data['product_name']}
🔢 **ចំនួន:** {context.user_data['quantity']}
💰 **តម្លៃសរុប:** ${context.user_data['price'] * context.user_data['quantity']:.2f}

🆔 **User ID:** {context.user_data.get('user_id', 'N/A')}
⏰ **ម៉ោង:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

**សូមរង់ចាំ screenshot ទូទាត់ពីអតិថិជន**"""

        await context.bot.send_message(
            ADMIN_ID,
            admin_text,
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Error notifying admin: {e}")

# ========== PAYMENT HANDLING ==========
async def handle_payment_option(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle payment option selection"""
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("pay_khqr_"):
        order_id = query.data.split("_")[2]
        
        # Send KHQR image again
        try:
            response = requests.get(KHQR_URL)
            if response.status_code == 200:
                photo = BytesIO(response.content)
                photo.name = 'khqr_payment.jpg'
                
                caption = f"""📸 **KHQR សម្រាប់ទូទាត់**

លេខកូដកម្មង: **#{order_id}**

**សូមស្កេនកូដ QR ខាងលើដើម្បីទូទាត់**
បន្ទាប់ពីទូទាត់ សូមផ្ញើ **screenshot ទូទាត់** មកយើង។"""
                
                await query.message.reply_photo(
                    photo=photo,
                    caption=caption,
                    parse_mode='Markdown'
                )
                
                # Update order payment method
                update_order_payment(order_id, "KHQR")
                
                await query.message.reply_text(
                    f"✅ **បានជ្រើសរើសទូទាត់តាម KHQR**\n\n"
                    f"លេខកូដកម្មង: **#{order_id}**\n"
                    f"សូមផ្ញើ **screenshot ទូទាត់** បន្ទាប់ពីបង់ប្រាក់។",
                    parse_mode='Markdown'
                )
        except:
            await query.message.reply_text(
                f"📸 **KHQR សម្រាប់ទូទាត់**\n\n"
                f"{KHQR_URL}\n\n"
                f"លេខកូដកម្មង: **#{order_id}**\n\n"
                f"**សូមផ្ញើ screenshot ទូទាត់មកយើង!**",
                parse_mode='Markdown'
            )
    
    elif query.data.startswith("pay_cash_"):
        order_id = query.data.split("_")[2]
        update_order_payment(order_id, "Cash")
        
        await query.message.reply_text(
            f"💵 **បានជ្រើសរើសទូទាត់នៅថ្នាក់**\n\n"
            f"លេខកូដកម្មង: **#{order_id}**\n\n"
            f"សូមទាក់ទងអ្នកគ្រប់គ្រងក្នុងថ្នាក់ដើម្បីបង់ប្រាក់។\n"
            f"ប្រាក់ត្រូវបានបង់តាមរយៈ: អ្នកគ្រប់គ្រងក្នុងថ្នាក់",
            parse_mode='Markdown'
        )
    
    elif query.data.startswith("upload_proof_"):
        order_id = query.data.split("_")[2]
        context.user_data['awaiting_proof_for'] = order_id
        
        await query.message.reply_text(
            f"📎 **សូមផ្ញើ screenshot ទូទាត់**\n\n"
            f"លេខកូដកម្មង: **#{order_id}**\n\n"
            f"សូមផ្ញើ **screenshot ទូទាត់** (រូបភាព)។\n"
            f"ឬវាយ /start ដើម្បីត្រឡប់មេនុយ",
            parse_mode='Markdown'
        )
    
    elif query.data == "back_to_main":
        await query.edit_message_text(
            "ត្រឡប់ទៅមេនុយចម្បង",
            reply_markup=get_main_keyboard()
        )

# ========== PAYMENT SCREENSHOT HANDLING ==========
async def handle_payment_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle payment screenshot submission"""
    order_id = context.user_data.get('awaiting_proof_for')
    
    if not order_id:
        # Check if user is sending screenshot without clicking button
        # Try to find their latest pending order
        user_orders = get_user_orders(update.effective_user.id)
        pending_orders = [o for o in user_orders if o[4] in ['pending', 'awaiting_verification']]
        
        if pending_orders:
            order_id = pending_orders[0][0]  # Get latest pending order
            context.user_data['awaiting_proof_for'] = order_id
        else:
            await update.message.reply_text(
                "សូមជ្រើសរើស 'ផ្ញើ screenshot ទូទាត់' ពីម៉ឺនុយការកម្មងមុន។\n"
                "ឬមិនទាន់មានការកម្មងរង់ចាំទូទាត់ទេ។",
                reply_markup=get_main_keyboard()
            )
            return
    
    if update.message.photo:
        # Forward to admin
        photo_id = update.message.photo[-1].file_id
        user = update.effective_user
        
        caption = f"""📸 **Screenshot ទូទាត់ថ្មី**

លេខកូដកម្មង: #{order_id}
អ្នកទិញ: {user.first_name}
User ID: {user.id}
ម៉ោង: {datetime.now().strftime('%H:%M:%S')}

**សូមពិនិត្យ និងបញ្ជាក់ការទូទាត់**"""
        
        try:
            # Forward screenshot to admin
            await context.bot.send_photo(
                ADMIN_ID,
                photo=photo_id,
                caption=caption,
                parse_mode='Markdown'
            )
            
            # Update order
            update_order_payment(order_id, "Bank Transfer", "screenshot_provided")
            
            # Notify user
            await update.message.reply_text(
                f"✅ **Screenshot ទូទាត់ត្រូវបានទទួល!**\n\n"
                f"លេខកូដកម្មង: **#{order_id}**\n"
                f"យើងនឹងពិនិត្យ screenshot ហើយប្រាប់បងឆាប់ៗនេះ។\n\n"
                f"សូមអរគុណសម្រាប់ការទូទាត់!",
                reply_markup=get_main_keyboard(),
                parse_mode='Markdown'
            )
            
            # Notify admin to review
            review_keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ បញ្ជាក់ការទូទាត់", callback_data=f"admin_confirm_{order_id}"),
                InlineKeyboardButton("❌ បដិសេធ", callback_data=f"admin_reject_{order_id}")
            ]])
            
            await context.bot.send_message(
                ADMIN_ID,
                f"🔔 **Screenshot រង់ចាំពិនិត្យ**\n\n"
                f"លេខកូដកម្មង: #{order_id}\n"
                f"ចុចប៊ូតុងខាងក្រោមដើម្បីពិនិត្យ:",
                reply_markup=review_keyboard,
                parse_mode='Markdown'
            )
            
            # Clear the awaiting proof state
            context.user_data.pop('awaiting_proof_for', None)
            
        except Exception as e:
            logger.error(f"Error forwarding screenshot: {e}")
            await update.message.reply_text(
                "❌ មានបញ្ហាក្នុងការផ្ញើ screenshot។ សូមព្យាយាមម្តងទៀត។"
            )
    else:
        await update.message.reply_text(
            "សូមផ្ញើរូបភាព screenshot ទូទាត់។"
        )

# ========== ORDER TRACKING ==========
async def track_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user's order history"""
    orders = get_user_orders(update.effective_user.id)
    
    if not orders:
        await update.message.reply_text(
            "📭 **បងមិនទាន់មានការកម្មងណាមួយទេ។**\n\n"
            "ចុច '📚 ទិញសៀវភៅ' ដើម្បីចាប់ផ្តើមកម្មង!",
            reply_markup=get_main_keyboard(),
            parse_mode='Markdown'
        )
        return
    
    response = "📋 **ប្រវត្តិការកម្មងរបស់បង:**\n\n"
    
    for order in orders[:10]:  # Show last 10 orders
        order_id, product, qty, total, status, payment_method, date, notes = order
        
        # Status icons
        status_icons = {
            'pending': '⏳',
            'awaiting_verification': '📸',
            'confirmed': '✅',
            'rejected': '❌',
            'completed': '🎉'
        }
        icon = status_icons.get(status, '📝')
        
        # Status text
        status_text = {
            'pending': 'រង់ចាំទូទាត់',
            'awaiting_verification': 'កំពុងពិនិត្យ screenshot',
            'confirmed': 'បញ្ជាក់ហើយ',
            'rejected': 'បដិសេធ',
            'completed': 'បញ្ចប់ហើយ'
        }
        
        response += f"""**{icon} លេខកូដ: #{order_id}**
📚 សៀវភៅ: {product}
🔢 ចំនួន: {qty}
💰 តម្លៃ: ${total:.2f}
📊 ស្ថានភាព: {status_text.get(status, status)}
💳 វិធីទូទាត់: {payment_method or 'មិនទាន់បានជ្រើសរើស'}
📅 កាលបរិច្ឆេទ: {date}
"""
        
        if notes:
            response += f"📝 ចំណាំ: {notes}\n"
        
        response += "────────────────────\n"
    
    if len(orders) > 10:
        response += f"\n... និង {len(orders)-10} ការកម្មងទៀត"
    
    keyboard = [
        [InlineKeyboardButton("📚 ទិញសៀវភៅទៀត", callback_data="choose_product")],
        [InlineKeyboardButton("🔙 មេនុយចម្បង", callback_data="back_to_main")]
    ]
    
    await update.message.reply_text(
        response,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

# ========== ADMIN PANEL ==========
@admin_only
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show admin panel"""
    stats = get_statistics()
    
    admin_text = f"""👑 **ផ្នែកអភិបាល**

📊 **ស្ថិតិ:**
• សរុបការកម្មង: {stats['total_orders']}
• កំពុងរង់ចាំ: {stats['status_counts'].get('pending', 0) + stats['status_counts'].get('awaiting_verification', 0)}
• ថ្ងៃនេះ: {stats['today_orders']} ការកម្មង
• ប្រាក់ចំណូលសរុប: ${stats['revenue']:.2f}
• អ្នកប្រើប្រាស់សរុប: {stats['total_users']}

**មុខងារ:**"""
    
    await update.message.reply_text(admin_text, 
                                   reply_markup=get_admin_keyboard(),
                                   parse_mode='Markdown')

@admin_only
async def handle_admin_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin commands"""
    text = update.message.text
    
    if text == "📊 ស្ថិតិ":
        await show_admin_stats(update, context)
    elif text == "📋 មើលការកម្មងទាំងអស់":
        await show_admin_orders_filter(update, context)
    elif text == "⏳ ការកម្មងរង់ចាំ":
        await show_admin_orders(update, context, page=1, status_filter='pending')
    elif text == "📸 ពិនិត្យ screenshot":
        await show_admin_orders(update, context, page=1, status_filter='awaiting_verification')
    elif text == "📥 ទាញយក Excel":
        await show_export_options(update, context)
    elif text == "👥 មើលអ្នកប្រើប្រាស់":
        await show_admin_users(update, context, page=1)
    elif text == "🔙 មេនុយចម្បង":
        await update.message.reply_text(
            "ត្រឡប់ទៅមេនុយចម្បង",
            reply_markup=get_main_keyboard()
        )

async def show_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show detailed admin statistics"""
    stats = get_statistics()
    
    # Calculate percentages
    total_orders = stats['total_orders']
    pending = stats['status_counts'].get('pending', 0)
    verifying = stats['status_counts'].get('awaiting_verification', 0)
    completed = stats['status_counts'].get('completed', 0)
    
    pending_pct = (pending / total_orders * 100) if total_orders > 0 else 0
    verifying_pct = (verifying / total_orders * 100) if total_orders > 0 else 0
    completed_pct = (completed / total_orders * 100) if total_orders > 0 else 0
    
    stats_text = f"""📊 **ស្ថិតិលម្អិត**

**📈 ទិដ្ឋភាពការកម្មង:**
• សរុបការកម្មង: {total_orders}
• ថ្ងៃនេះ: {stats['today_orders']} ការកម្មង (${stats['today_revenue']:.2f})
• សប្តាហ៍នេះ: {get_orders_count(date_filter='week')}
• ខែនេះ: {get_orders_count(date_filter='month')}

**📊 ស្ថានភាពការកម្មង:**
• ⏳ រង់ចាំ: {pending} ({pending_pct:.1f}%)
• 📸 កំពុងពិនិត្យ: {verifying} ({verifying_pct:.1f}%)
• ✅ បញ្ចប់: {completed} ({completed_pct:.1f}%)
• ❌ បដិសេធ: {stats['status_counts'].get('rejected', 0)}

**💰 ហិរញ្ញវត្ថុ:**
• ប្រាក់ចំណូលសរុប: ${stats['revenue']:.2f}
• តម្លៃមធ្យមក្នុងមួយការកម្មង: ${(stats['revenue']/completed if completed > 0 else 0):.2f}
• ប្រាក់ចំណូលថ្ងៃនេះ: ${stats['today_revenue']:.2f}

**👥 អ្នកប្រើប្រាស់:**
• សរុបអ្នកប្រើប្រាស់: {stats['total_users']}
• ការកម្មងមធ្យមក្នុងមួយអ្នក: {(total_orders/stats['total_users'] if stats['total_users'] > 0 else 0):.1f}

**📚 ការលក់ផលិតផល:**
"""
    
    for product, sold in stats['product_sales'][:10]:
        stats_text += f"• {product}: លក់ {sold} ក្បាល\n"
    
    if len(stats['product_sales']) > 10:
        stats_text += f"• ... និង {len(stats['product_sales']) - 10} ផលិតផលទៀត\n"
    
    keyboard = [
        [InlineKeyboardButton("📋 មើលការកម្មង", callback_data="admin_orders_1_all_none")],
        [InlineKeyboardButton("👥 មើលអ្នកប្រើប្រាស់", callback_data="admin_users_1")],
        [InlineKeyboardButton("🔙 ត្រឡប់", callback_data="admin_back")]
    ]
    
    await update.message.reply_text(
        stats_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def show_admin_orders_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show order filter options"""
    await update.message.reply_text(
        "🔍 **ជ្រើសរើសតម្រងសម្រាប់ការកម្មង:**\n\n"
        "ជ្រើសរើសតម្រងដើម្បីមើលការកម្មង:",
        reply_markup=get_admin_orders_filter_keyboard(),
        parse_mode='Markdown'
    )

async def show_admin_orders(update: Update, context: ContextTypes.DEFAULT_TYPE, 
                           page=1, status_filter='all', date_filter=None, search_query=None):
    """Show orders with pagination"""
    # Get data
    total_orders = get_orders_count(status_filter, date_filter)
    total_pages = max(1, math.ceil(total_orders / ORDERS_PER_PAGE))
    
    if page > total_pages:
        page = total_pages
    
    orders = get_orders_paginated(page, status_filter, date_filter, search_query)
    
    if not orders:
        no_orders_text = "📭 **គ្មានការកម្មងត្រូវបានរកឃើញ**"
        if search_query:
            no_orders_text += f" សម្រាប់ការស្វែងរក: {search_query}"
        elif status_filter != 'all':
            status_text = {
                'pending': '⏳ រង់ចាំ',
                'awaiting_verification': '📸 កំពុងពិនិត្យ',
                'completed': '✅ បញ្ចប់',
                'rejected': '❌ បដិសេធ'
            }
            no_orders_text += f" ជាមួយស្ថានភាព: {status_text.get(status_filter, status_filter)}"
        elif date_filter:
            date_text = {
                'today': '📅 ថ្ងៃនេះ',
                'week': '📅 សប្តាហ៍នេះ',
                'month': '📅 ខែនេះ'
            }
            no_orders_text += f" សម្រាប់រយៈពេល: {date_text.get(date_filter, date_filter)}"
        
        keyboard = [[InlineKeyboardButton("🔙 ត្រឡប់", callback_data="admin_back")]]
        
        if isinstance(update, Update) and update.message:
            await update.message.reply_text(no_orders_text, 
                                           reply_markup=InlineKeyboardMarkup(keyboard),
                                           parse_mode='Markdown')
        else:
            await update.edit_message_text(no_orders_text,
                                         reply_markup=InlineKeyboardMarkup(keyboard),
                                         parse_mode='Markdown')
        return
    
    # Build response
    if search_query:
        response = f"🔍 **លទ្ធផលស្វែងរក: '{search_query}'**\n\n"
    else:
        filter_text = ""
        if status_filter != 'all':
            status_text = {
                'pending': '⏳ រង់ចាំ',
                'awaiting_verification': '📸 កំពុងពិនិត្យ',
                'completed': '✅ បញ្ចប់',
                'rejected': '❌ បដិសេធ'
            }
            filter_text = f" • ស្ថានភាព: {status_text.get(status_filter, status_filter)}"
        
        if date_filter:
            date_text = {
                'today': '📅 ថ្ងៃនេះ',
                'week': '📅 សប្តាហ៍នេះ',
                'month': '📅 ខែនេះ'
            }
            filter_text += f" • រយៈពេល: {date_text.get(date_filter, date_filter)}"
        
        response = f"📋 **ការកម្មងទាំងអស់**{filter_text}\n\n"
        response += f"📄 **ទំព័រ {page}/{total_pages}** • **សរុប: {total_orders} ការកម្មង**\n\n"
    
    # Add orders
    for order in orders:
        order_id, name, group, phone, product, qty, total, status, payment_method, date, notes = order
        
        # Status icons
        status_icons = {
            'pending': '⏳',
            'awaiting_verification': '📸',
            'confirmed': '✅',
            'rejected': '❌',
            'completed': '🎉'
        }
        icon = status_icons.get(status, '📝')
        
        # Shorten long names
        display_name = name[:15] + "..." if len(name) > 15 else name
        display_group = group[:10] + "..." if len(group) > 10 else group
        
        response += f"""**{icon} #{order_id}** • **{display_name}** ({display_group})
📚 {product} ×{qty} • 💰 ${total:.2f}
💳 {payment_method or 'គ្មានវិធី'} • 📅 {date.split()[0]}
────────────────────
"""
    
    # Add pagination info
    if total_pages > 1:
        response += f"\n📄 **ទំព័រ {page} នៃ {total_pages}** • **{total_orders} ការកម្មងសរុប**"
    
    # Create keyboard
    keyboard = get_pagination_keyboard(page, total_pages, "admin_orders", 
                                      status_filter, date_filter, search_query)
    
    if isinstance(update, Update) and update.message:
        await update.message.reply_text(response, 
                                       reply_markup=keyboard,
                                       parse_mode='Markdown')
    else:
        await update.edit_message_text(response,
                                     reply_markup=keyboard,
                                     parse_mode='Markdown')

async def handle_admin_orders_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin orders pagination"""
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("admin_orders_"):
        parts = query.data.split("_")
        if len(parts) >= 4:
            page = int(parts[2])
            status_filter = parts[3]
            date_filter = parts[4] if parts[4] != 'none' else None
            search_query = parts[5] if len(parts) > 5 and parts[5] != 'none' else None
            await show_admin_orders(query, context, page, status_filter, date_filter, search_query)
    
    elif query.data.startswith("filter_"):
        filter_type = query.data.split("_")[1]
        
        if filter_type in ['today', 'week', 'month']:
            await show_admin_orders(query, context, page=1, date_filter=filter_type)
        elif filter_type == 'all':
            await show_admin_orders(query, context, page=1)
        else:
            await show_admin_orders(query, context, page=1, status_filter=filter_type)
    
    elif query.data == "admin_search":
        context.user_data['awaiting_search'] = True
        await query.message.reply_text(
            "🔍 **ស្វែងរកការកម្មង**\n\n"
            "សូមវាយបញ្ចូលពាក្យស្វែងរក:\n"
            "(ស្វែងរកតាមលេខកូដកម្មង, ឈ្មោះ, ក្រុម, ឬផលិតផល)",
            parse_mode='Markdown'
        )
    
    elif query.data == "admin_stats":
        await show_admin_stats(query, context)
    
    elif query.data.startswith("admin_export_"):
        parts = query.data.split("_")
        status_filter = parts[2]
        date_filter = parts[3] if parts[3] != 'none' else None
        await export_orders_admin(query, context, status_filter, date_filter)
    
    elif query.data == "admin_back":
        await admin_panel(query, context)

async def handle_admin_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin search input"""
    if context.user_data.get('awaiting_search'):
        search_query = update.message.text
        context.user_data['awaiting_search'] = False
        await show_admin_orders(update, context, page=1, search_query=search_query)

async def show_admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE, page=1, search_query=None):
    """Show users with pagination"""
    # Get data
    total_users = get_users_count(search_query)
    total_pages = max(1, math.ceil(total_users / USERS_PER_PAGE))
    
    if page > total_pages:
        page = total_pages
    
    users = get_users_paginated(page, search_query)
    
    if not users:
        no_users_text = "👥 **គ្មានអ្នកប្រើប្រាស់ត្រូវបានរកឃើញ**"
        if search_query:
            no_users_text += f" សម្រាប់ការស្វែងរក: {search_query}"
        
        keyboard = [[InlineKeyboardButton("🔙 ត្រឡប់", callback_data="admin_back")]]
        
        if isinstance(update, Update) and update.message:
            await update.message.reply_text(no_users_text, 
                                           reply_markup=InlineKeyboardMarkup(keyboard),
                                           parse_mode='Markdown')
        else:
            await update.edit_message_text(no_users_text,
                                         reply_markup=InlineKeyboardMarkup(keyboard),
                                         parse_mode='Markdown')
        return
    
    # Build response
    if search_query:
        response = f"🔍 **ការស្វែងរកអ្នកប្រើប្រាស់: '{search_query}'**\n\n"
    else:
        response = f"👥 **អ្នកប្រើប្រាស់ទាំងអស់**\n\n"
        response += f"📄 **ទំព័រ {page}/{total_pages}** • **សរុប: {total_users} អ្នកប្រើប្រាស់**\n\n"
    
    # Add users
    for user in users:
        user_id, first_name, group_name, phone, reg_date, total_orders, total_spent = user
        
        # Shorten long names
        display_name = first_name[:15] + "..." if len(first_name) > 15 else first_name
        display_group = group_name[:10] + "..." if group_name and len(group_name) > 10 else (group_name or "N/A")
        
        response += f"""**👤 {display_name}** ({display_group})
🆔 {user_id} • 📞 {phone or 'N/A'}
📦 ការកម្មង: {total_orders} • 💰 បានចំណាយ: ${total_spent:.2f}
📅 ចូលរួម: {reg_date.split()[0]}
────────────────────
"""
    
    # Create keyboard
    keyboard = []
    
    # Pagination
    if total_pages > 1:
        nav_row = []
        if page > 1:
            nav_row.append(InlineKeyboardButton("⬅️", callback_data=f"admin_users_{page-1}_{search_query or 'none'}"))
        
        nav_row.append(InlineKeyboardButton(f"📄 {page}/{total_pages}", callback_data="noop"))
        
        if page < total_pages:
            nav_row.append(InlineKeyboardButton("➡️", callback_data=f"admin_users_{page+1}_{search_query or 'none'}"))
        
        keyboard.append(nav_row)
    
    # Actions
    keyboard.append([
        InlineKeyboardButton("🔍 ស្វែងរកអ្នកប្រើ", callback_data="admin_search_users"),
        InlineKeyboardButton("📥 បញ្ចេញអ្នកប្រើ", callback_data="admin_export_users")
    ])
    
    keyboard.append([InlineKeyboardButton("🔙 ត្រឡប់", callback_data="admin_back")])
    
    if isinstance(update, Update) and update.message:
        await update.message.reply_text(response, 
                                       reply_markup=InlineKeyboardMarkup(keyboard),
                                       parse_mode='Markdown')
    else:
        await update.edit_message_text(response,
                                     reply_markup=InlineKeyboardMarkup(keyboard),
                                     parse_mode='Markdown')

async def handle_admin_users_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin users pagination"""
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("admin_users_"):
        parts = query.data.split("_")
        if len(parts) >= 4:
            page = int(parts[2])
            search_query = parts[3] if parts[3] != 'none' else None
            await show_admin_users(query, context, page, search_query)
    
    elif query.data == "admin_search_users":
        context.user_data['awaiting_user_search'] = True
        await query.message.reply_text(
            "🔍 **ស្វែងរកអ្នកប្រើប្រាស់**\n\n"
            "សូមវាយបញ្ចូលពាក្យស្វែងរក:\n"
            "(ស្វែងរកតាមឈ្មោះ, ក្រុម, ឬលេខទូរស័ព្ទ)",
            parse_mode='Markdown'
        )
    
    elif query.data == "admin_export_users":
        await export_users_admin(query, context)
    
    elif query.data == "admin_back":
        await admin_panel(query, context)

async def handle_admin_user_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin user search input"""
    if context.user_data.get('awaiting_user_search'):
        search_query = update.message.text
        context.user_data['awaiting_user_search'] = False
        await show_admin_users(update, context, page=1, search_query=search_query)

async def show_export_options(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show export options"""
    keyboard = [
        [
            InlineKeyboardButton("📋 ទាំងអស់", callback_data="export_all"),
            InlineKeyboardButton("⏳ រង់ចាំ", callback_data="export_pending")
        ],
        [
            InlineKeyboardButton("📸 កំពុងពិនិត្យ", callback_data="export_awaiting_verification"),
            InlineKeyboardButton("✅ បញ្ចប់", callback_data="export_completed")
        ],
        [
            InlineKeyboardButton("📅 ថ្ងៃនេះ", callback_data="export_today"),
            InlineKeyboardButton("📅 សប្តាហ៍នេះ", callback_data="export_week")
        ],
        [
            InlineKeyboardButton("📅 ខែនេះ", callback_data="export_month"),
            InlineKeyboardButton("👥 បញ្ជីអ្នកប្រើ", callback_data="export_users")
        ],
        [InlineKeyboardButton("🔙 ត្រឡប់", callback_data="admin_back")]
    ]
    
    await update.message.reply_text(
        "📥 **បញ្ចេញទិន្នន័យទៅជា Excel**\n\n"
        "ជ្រើសរើសទិន្នន័យដែលចង់បញ្ចេញ:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def export_orders_admin(query, context: ContextTypes.DEFAULT_TYPE, status_filter='all', date_filter=None):
    """Export orders to Excel for admin"""
    await query.answer("⏳ កំពុងរៀបចំឯកសារ Excel...")
    
    try:
        filename = export_to_excel(status_filter, date_filter)
        
        # Create filter description
        filter_desc = ""
        if status_filter != 'all':
            filter_desc += f"ស្ថានភាព: {status_filter} • "
        if date_filter:
            filter_desc += f"រយៈពេល: {date_filter} • "
        
        caption = f"📥 **បានបញ្ចេញការកម្មង**\n\n{filter_desc}កាលបរិច្ឆេទ: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        
        await context.bot.send_document(
            chat_id=ADMIN_ID,
            document=open(filename, 'rb'),
            caption=caption,
            parse_mode='Markdown'
        )
        
        os.remove(filename)
        
        await query.message.reply_text("✅ ឯកសារ Excel ត្រូវបានផ្ញើទៅកាន់ Chat របស់អ្នក!")
        
    except Exception as e:
        logger.error(f"Error exporting: {e}")
        await query.message.reply_text(f"❌ កំហុស: {str(e)}")

async def export_users_admin(query, context: ContextTypes.DEFAULT_TYPE):
    """Export users to Excel"""
    await query.answer("⏳ កំពុងរៀបចំឯកសារ Excel អ្នកប្រើប្រាស់...")
    
    try:
        conn = sqlite3.connect('bookshop.db')
        c = conn.cursor()
        c.execute('''SELECT user_id, first_name, group_name, phone, 
                            registration_date, total_orders, total_spent
                     FROM users ORDER BY registration_date DESC''')
        users = c.fetchall()
        conn.close()
        
        df = pd.DataFrame(users, columns=[
            'User ID', 'Name', 'Group', 'Phone', 
            'Registration Date', 'Total Orders', 'Total Spent'
        ])
        
        filename = f'users_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'
        df.to_excel(filename, index=False)
        
        caption = f"👥 **បានបញ្ចេញបញ្ជីអ្នកប្រើប្រាស់**\n\nសរុបអ្នកប្រើប្រាស់: {len(users)}\nកាលបរិច្ឆេទ: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        
        await context.bot.send_document(
            chat_id=ADMIN_ID,
            document=open(filename, 'rb'),
            caption=caption,
            parse_mode='Markdown'
        )
        
        os.remove(filename)
        
        await query.message.reply_text("✅ ឯកសារ Excel អ្នកប្រើប្រាស់ត្រូវបានផ្ញើទៅកាន់ Chat របស់អ្នក!")
        
    except Exception as e:
        logger.error(f"Error exporting users: {e}")
        await query.message.reply_text(f"❌ កំហុស: {str(e)}")

async def handle_export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle export commands from admin panel"""
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("export_"):
        export_type = query.data.split("_")[1]
        
        if export_type == 'users':
            await export_users_admin(query, context)
        else:
            # For orders
            if export_type in ['today', 'week', 'month']:
                await export_orders_admin(query, context, date_filter=export_type)
            else:
                await export_orders_admin(query, context, status_filter=export_type)

async def handle_admin_order_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle order actions from admin"""
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("admin_view_"):
        order_id = int(query.data.split("_")[2])
        order = get_order_details(order_id)
        
        if order:
            (order_id, user_id, product_name, quantity, total_price, 
             status, payment_method, payment_proof, order_date, 
             admin_notes, first_name, group_name, phone, username) = order
            
            status_text = {
                'pending': '⏳ រង់ចាំទូទាត់',
                'awaiting_verification': '📸 កំពុងពិនិត្យ screenshot',
                'confirmed': '✅ បញ្ជាក់ហើយ',
                'rejected': '❌ បដិសេធ',
                'completed': '🎉 បញ្ចប់ហើយ'
            }
            
            response = f"""📋 **ព័ត៌មានលម្អិតការកម្មង:**

**លេខកូដ:** #{order_id}
**អ្នកទិញ:** {first_name}
**ក្រុម:** {group_name}
**ទូរស័ព្ទ:** {phone}
**Telegram:** @{username if username else 'N/A'}
**សៀវភៅ:** {product_name}
**ចំនួន:** {quantity}
**តម្លៃសរុប:** ${total_price:.2f}
**ស្ថានភាព:** {status_text.get(status, status)}
**វិធីទូទាត់:** {payment_method or 'មិនទាន់បានជ្រើសរើស'}
**កាលបរិច្ឆេទ:** {order_date}
**ចំណាំ:** {admin_notes or 'គ្មាន'}"""
            
            # Get page info from callback data if available
            page = 1
            status_filter = 'all'
            date_filter = None
            
            if len(query.data.split("_")) > 3:
                try:
                    page = int(query.data.split("_")[3])
                    status_filter = query.data.split("_")[4]
                    date_filter = query.data.split("_")[5] if query.data.split("_")[5] != 'none' else None
                except:
                    pass
            
            await query.edit_message_text(
                response,
                reply_markup=get_admin_order_keyboard(order_id, page, status_filter, date_filter),
                parse_mode='Markdown'
            )
    
    elif query.data.startswith("admin_confirm_"):
        order_id = int(query.data.split("_")[2])
        user_id = update_order_status(order_id, 'confirmed', 'បានបញ្ជាក់ដោយអ្នកគ្រប់គ្រង')
        
        await query.edit_message_text(f"✅ **ការកម្មង #{order_id} ត្រូវបានបញ្ជាក់!**", parse_mode='Markdown')
        
        # Notify user
        try:
            await context.bot.send_message(
                user_id,
                f"✅ **ការកម្មងរបស់បងត្រូវបានបញ្ជាក់!**\n\n"
                f"លេខកូដកម្មង: **#{order_id}**\n"
                f"សូមអរគុណសម្រាប់ការទិញ!",
                parse_mode='Markdown'
            )
        except:
            pass
        
    elif query.data.startswith("admin_reject_"):
        order_id = int(query.data.split("_")[2])
        user_id = update_order_status(order_id, 'rejected', 'បដិសេធដោយអ្នកគ្រប់គ្រង')
        
        await query.edit_message_text(f"❌ **ការកម្មង #{order_id} ត្រូវបានបដិសេធ!**", parse_mode='Markdown')
        
        # Notify user
        try:
            await context.bot.send_message(
                user_id,
                f"❌ **ការកម្មងរបស់បងត្រូវបានបដិសេធ!**\n\n"
                f"លេខកូដកម្មង: **#{order_id}**\n"
                f"សូមទំនាក់ទំនងអ្នកគ្រប់គ្រងបើមានសំណួរ។",
                parse_mode='Markdown'
            )
        except:
            pass
        
    elif query.data.startswith("admin_complete_"):
        order_id = int(query.data.split("_")[2])
        user_id = update_order_status(order_id, 'completed', 'បញ្ចប់ដោយអ្នកគ្រប់គ្រង')
        
        await query.edit_message_text(f"🎉 **ការកម្មង #{order_id} ត្រូវបានបញ្ចប់!**", parse_mode='Markdown')
        
        # Notify user
        try:
            await context.bot.send_message(
                user_id,
                f"🎉 **ការកម្មងរបស់បងត្រូវបានបញ្ចប់!**\n\n"
                f"លេខកូដកម្មង: **#{order_id}**\n"
                f"សូមអរគុណសម្រាប់ការទិញ! សូមមកទិញម្តងទៀតនៅពេលក្រោយ។",
                parse_mode='Markdown'
            )
        except:
            pass
    
    elif query.data.startswith("admin_contact_"):
        order_id = int(query.data.split("_")[2])
        order = get_order_details(order_id)
        
        if order:
            _, user_id, _, _, _, _, _, _, _, _, first_name, _, phone, username = order
            
            contact_info = f"""📞 **ព័ត៌មានទំនាក់ទំនង:**

លេខកូដកម្មង: #{order_id}
ឈ្មោះ: {first_name}
ទូរស័ព្ទ: {phone}
Telegram: @{username if username else 'គ្មាន'}
User ID: {user_id}

ចុចពីក្រោមដើម្បីទំនាក់ទំនង:"""
            
            keyboard = []
            if username:
                keyboard.append([InlineKeyboardButton("💬 ជជែកតាម Telegram", url=f"https://t.me/{username}")])
            
            keyboard.append([
                InlineKeyboardButton("📝 ផ្ញើសារ", callback_data=f"admin_message_{user_id}"),
                InlineKeyboardButton("🔙 ត្រឡប់", callback_data=f"admin_view_{order_id}")
            ])
            
            await query.edit_message_text(
                contact_info,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )
    
    elif query.data.startswith("admin_note_"):
        order_id = int(query.data.split("_")[2])
        context.user_data['adding_note_for'] = order_id
        
        await query.message.reply_text(
            f"📝 **បន្ថែមចំណាំសម្រាប់ការកម្មង #{order_id}**\n\n"
            f"សូមវាយបញ្ចូលចំណាំរបស់អ្នក:",
            parse_mode='Markdown'
        )
    
    elif query.data == "admin_back":
        await admin_panel(query, context)

async def handle_admin_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin adding note to order"""
    order_id = context.user_data.get('adding_note_for')
    
    if order_id and update.message.text:
        note = update.message.text
        
        conn = sqlite3.connect('bookshop.db')
        c = conn.cursor()
        c.execute('''UPDATE orders SET admin_notes = ? WHERE order_id = ?''',
                  (note, order_id))
        conn.commit()
        conn.close()
        
        await update.message.reply_text(
            f"✅ **បានបន្ថែមចំណាំសម្រាប់ការកម្មង #{order_id}**",
            parse_mode='Markdown'
        )
        
        context.user_data.pop('adding_note_for', None)

# ========== ERROR HANDLER ==========
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors"""
    logger.error(f"Update {update} caused error {context.error}")
    
    try:
        await context.bot.send_message(
            update.effective_chat.id,
            "❌ **មានបញ្ហាកើតឡើង។** សូមព្យាយាមម្តងទៀត ឬទំនាក់ទំនងអ្នកអភិវឌ្ឍ!",
            parse_mode='Markdown'
        )
    except:
        pass

# ========== MAIN FUNCTION ==========
def main():
    """Start the bot"""
    # Initialize database
    init_db()
    
    # Create application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Add conversation handler for ordering
    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(select_product, pattern="^(choose_product|product_|view_all_prices)$"),
            MessageHandler(filters.TEXT & filters.Regex("^📚 ទិញសៀវភៅ$"), 
                          lambda u,c: select_product(u, c) if hasattr(u, 'callback_query') else None)
        ],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
            GROUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_group)],
            PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_phone),
                CommandHandler('skip', get_phone)
            ],
            QUANTITY: [
                CallbackQueryHandler(select_quantity, pattern="^qty_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_custom_quantity)
            ],
            CONFIRMATION: [
                CallbackQueryHandler(confirm_order, pattern="^(confirm_order|edit_order)$"),
            ]
        },
        fallbacks=[
            CommandHandler('start', 
                         lambda u,c: (u.message.reply_text("ត្រឡប់ទៅមេនុយចម្បង", 
                                                         reply_markup=get_main_keyboard()),
                                     ConversationHandler.END))
        ],
        allow_reentry=True
    )
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("admin", admin_panel))
    
    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(handle_payment_option, pattern="^(pay_|upload_proof_|back_to_main)"))
    application.add_handler(CallbackQueryHandler(handle_admin_orders_navigation, pattern="^(admin_orders_|filter_|admin_search|admin_stats|admin_export_|admin_back)"))
    application.add_handler(CallbackQueryHandler(handle_admin_users_navigation, pattern="^(admin_users_|admin_search_users|admin_export_users)"))
    application.add_handler(CallbackQueryHandler(handle_admin_order_action, pattern="^admin_(view|confirm|reject|complete|contact|note)"))
    application.add_handler(CallbackQueryHandler(handle_export_command, pattern="^export_"))
    
    application.add_handler(MessageHandler(filters.PHOTO, handle_payment_screenshot))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_note))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_search))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_user_search))
    
    application.add_handler(MessageHandler(
        filters.TEXT & filters.Regex("^(📚 ទិញសៀវភៅ|📋 តាមដានការកម្មង|❓ Q&A|👤 អំពីយើង|👑 Admin Panel)$"),
        handle_main_menu
    ))
    
    application.add_handler(MessageHandler(
        filters.TEXT & filters.Regex("^(📊 ស្ថិតិ|📋 មើលការកម្មងទាំងអស់|⏳ ការកម្មងរង់ចាំ|📸 ពិនិត្យ screenshot|📥 ទាញយក Excel|👥 មើលអ្នកប្រើប្រាស់|🔙 មេនុយចម្បង)$"),
        handle_admin_commands
    ))
    
    # Add error handler
    application.add_error_handler(error_handler)
    
    # Start the bot
    print("🤖 Bot is starting...")
    print(f"👑 Admin ID: {ADMIN_ID}")
    print(f"👨‍💻 Developer: {DEVELOPER_USERNAME}")
    print(f"💳 KHQR URL: {KHQR_URL}")
    print(f"🏦 ABA Pay URL: {ABA_PAY_URL}")
    print(f"📊 Orders per page: {ORDERS_PER_PAGE}")
    print(f"👥 Users per page: {USERS_PER_PAGE}")
    print(f"✅ Your bot token is already configured!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()