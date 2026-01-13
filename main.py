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
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"  # Replace with your bot token
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
            InlineKeyboardButton("📞 Contact", url=f"https://t.me/{DEVELOPER_USERNAME[1:]}")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_admin_keyboard():
    """Admin panel keyboard"""
    keyboard = [
        ["📊 Statistics", "📋 View All Orders"],
        ["⏳ Pending Orders", "📸 Verify Payments"],
        ["📥 Download Excel", "👥 View Users"],
        ["🔙 Main Menu"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_admin_orders_filter_keyboard():
    """Filter keyboard for orders"""
    keyboard = [
        [
            InlineKeyboardButton("📋 All Orders", callback_data="filter_all"),
            InlineKeyboardButton("⏳ Pending", callback_data="filter_pending"),
            InlineKeyboardButton("📸 Verify", callback_data="filter_awaiting_verification")
        ],
        [
            InlineKeyboardButton("✅ Completed", callback_data="filter_completed"),
            InlineKeyboardButton("❌ Rejected", callback_data="filter_rejected"),
            InlineKeyboardButton("💰 Today", callback_data="filter_today")
        ],
        [
            InlineKeyboardButton("📅 This Week", callback_data="filter_week"),
            InlineKeyboardButton("📅 This Month", callback_data="filter_month"),
            InlineKeyboardButton("🔍 Search", callback_data="admin_search")
        ],
        [
            InlineKeyboardButton("📊 Stats", callback_data="admin_stats"),
            InlineKeyboardButton("🔙 Back", callback_data="admin_back")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_admin_order_keyboard(order_id, page=1, status_filter='all', date_filter=None):
    """Admin order action keyboard"""
    keyboard = [
        [
            InlineKeyboardButton("✅ Confirm Payment", callback_data=f"admin_confirm_{order_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"admin_reject_{order_id}")
        ],
        [
            InlineKeyboardButton("📞 Contact Buyer", callback_data=f"admin_contact_{order_id}"),
            InlineKeyboardButton("💰 Complete Order", callback_data=f"admin_complete_{order_id}")
        ],
        [
            InlineKeyboardButton("📝 Add Note", callback_data=f"admin_note_{order_id}"),
            InlineKeyboardButton("🔙 Back", callback_data=f"admin_orders_{page}_{status_filter}_{date_filter or 'none'}")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_pagination_keyboard(page, total_pages, action_prefix, current_filter='all', date_filter=None, search_query=None):
    """Generate pagination keyboard"""
    keyboard = []
    
    # Navigation buttons
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"{action_prefix}_{page-1}_{current_filter}_{date_filter or 'none'}_{search_query or 'none'}"))
    
    nav_row.append(InlineKeyboardButton(f"📄 {page}/{total_pages}", callback_data="noop"))
    
    if page < total_pages:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"{action_prefix}_{page+1}_{current_filter}_{date_filter or 'none'}_{search_query or 'none'}"))
    
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
        InlineKeyboardButton("🔍 Search", callback_data="admin_search"),
        InlineKeyboardButton("📥 Export", callback_data=f"admin_export_{current_filter}_{date_filter or 'none'}")
    ])
    
    keyboard.append([
        InlineKeyboardButton("📊 Stats", callback_data="admin_stats"),
        InlineKeyboardButton("🔙 Back", callback_data="admin_back")
    ])
    
    return InlineKeyboardMarkup(keyboard)

def get_confirmation_keyboard():
    """Order confirmation keyboard"""
    keyboard = [
        [
            InlineKeyboardButton("✅ Confirm Order", callback_data="confirm_order"),
            InlineKeyboardButton("✏️ Edit", callback_data="edit_order")
        ],
        [
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_order")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

# ========== COMMAND HANDLERS ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    user = update.effective_user
    add_user(user.id, user.username, user.first_name, user.last_name)
    
    welcome_text = """🎉 **សូមស្វាគមន៍មកកាន់ហាងសៀវភៅរបស់យើង!**

ជម្រើសសៀវភៅដែលមាន:
📐 Math Book - $1.70
👥 Human & Society - $1.99
💼 Principle of Business - $1.99
💻 Computer Book - $2.50

**Warning:** ⚠️
- No refund for fake payment screenshot
- Send payment screenshot after payment

ជ្រើសរើសពីម៉ឺនុយខាងក្រោម! 👇"""
    
    await update.message.reply_text(welcome_text, 
                                   reply_markup=get_main_keyboard(),
                                   parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
    help_text = """❓ **Q&A**

**How to Order:**
1. Click "📚 ទិញសៀវភៅ"
2. Choose book you want to buy
3. Fill name, Group, and phone number
4. Choose quantity
5. Choose payment method

**Payment Methods:**
💰 **KHQR**: Scan QR code
🏦 **ABA Pay**: Click link
💵 **Pay at Class**: For people no bank

**Order Tracking:** 📋
You can track your order status anytime

**Developer Contact:** 👨‍💻
""" + DEVELOPER_USERNAME + """

**Note:** After payment, send payment screenshot to admin"""
    
    await update.message.reply_text(help_text, parse_mode='Markdown')

# ========== MAIN MENU HANDLERS ==========
async def handle_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle main menu buttons"""
    text = update.message.text
    
    if text == "📚 ទិញសៀវភៅ":
        # Show prices first
        price_text = "💰 **Book Prices:**\n\n"
        for pid, info in PRODUCTS.items():
            emoji = info.get('emoji', '📚')
            price_text += f"{emoji} **{info['name']}**: ${info['price']:.2f}\n"
        
        price_text += "\nClick button below to choose book:"
        
        keyboard = [
            [InlineKeyboardButton("📚 Choose Book", callback_data="choose_product")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]
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
        about_text = f"""🏫 **Book Shop for Classmates**

We help print books for study with good price and quality.

**Contact Info:**
👨‍💻 Developer: {DEVELOPER_USERNAME}
📧 Contact: Via Telegram

**Warning:** ⚠️
- No refund
- Send payment screenshot to admin"""
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
            "Click on book you want to buy:",
            reply_markup=get_product_keyboard(),
            parse_mode='Markdown'
        )
    
    elif query.data == "view_all_prices":
        price_text = "💰 **All Book Prices:**\n\n"
        for pid, info in PRODUCTS.items():
            emoji = info.get('emoji', '📚')
            price_text += f"{emoji} **{info['name']}**: ${info['price']:.2f}\n"
        
        keyboard = [
            [InlineKeyboardButton("📚 Choose Book", callback_data="choose_product")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]
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
            f"{product['emoji']} **Selected: {product['name']}**\n"
            f"💰 Price: ${product['price']:.2f}\n\n"
            f"📝 **សូមបំពេញព័ត៌មានសម្រាប់ការកម្មង**\n\n"
            f"សូមវាយបញ្ចូលឈ្មោះពេញរបស់បង:",
            parse_mode='Markdown'
        )
        return NAME
    
    elif query.data == "back_to_main":
        await query.edit_message_text(
            "Available options:",
            reply_markup=get_main_keyboard()
        )
        return ConversationHandler.END

async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get user's name"""
    context.user_data['name'] = update.message.text
    await update.message.reply_text(
        "👥 **សូមវាយបញ្ចូល Group របស់បង**\n\n"
        "Example: Civil M3, M4, A1, B2, ...",
        parse_mode='Markdown'
    )
    return GROUP

async def get_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get user's group"""
    context.user_data['group'] = update.message.text
    await update.message.reply_text(
        "📞 **សូមវាយបញ្ចូលលេខទូរស័ព្ទរបស់បង**\n\n"
        "Or click /skip to skip\n"
        "(Phone number help for contact if have problem)",
        parse_mode='Markdown'
    )
    return PHONE

async def get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get user's phone number"""
    if update.message.text != "/skip":
        context.user_data['phone'] = update.message.text
    else:
        context.user_data['phone'] = "Not specified"
    
    # Save user info
    update_user_info(
        update.effective_user.id,
        context.user_data['group'],
        context.user_data['phone']
    )
    
    # Ask for quantity
    await update.message.reply_text(
        f"🔢 **សូមជ្រើសរើសចំនួនសៀវភៅដែលបងចង់ទិញ**\n\n"
        f"{context.user_data['product_emoji']} Book: {context.user_data['product_name']}\n"
        f"💰 Price each: ${context.user_data['price']:.2f}",
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
                "(Type only number example: 2, 5, 10, ...)",
                parse_mode='Markdown'
            )
            return QUANTITY
        
        quantity = int(query.data.split("_")[1])
        context.user_data['quantity'] = quantity
        await show_order_summary(query, context)
        return CONFIRMATION
    
    elif query.data == "back_to_products":
        await query.edit_message_text(
            "📚 Please choose book:",
            reply_markup=get_product_keyboard()
        )
        return ConversationHandler.END

async def get_custom_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get custom quantity"""
    try:
        quantity = int(update.message.text)
        if quantity < 1:
            await update.message.reply_text("❌ Please type number greater than 0")
            return QUANTITY
        if quantity > 50:
            await update.message.reply_text("❌ Too many quantity, please contact admin")
            return QUANTITY
            
        context.user_data['quantity'] = quantity
        await show_order_summary_message(update, context)
        return CONFIRMATION
    except ValueError:
        await update.message.reply_text("❌ Please type correct number (example: 1, 2, 3, ...)")
        return QUANTITY

async def show_order_summary_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show order summary for message updates"""
    product_name = context.user_data['product_name']
    price = context.user_data['price']
    quantity = context.user_data['quantity']
    total = price * quantity
    
    summary = f"""📋 **Order Summary:**

{context.user_data.get('product_emoji', '📚')} **Book:** {product_name}
👤 **Name:** {context.user_data['name']}
👥 **Group:** {context.user_data['group']}
📞 **Phone:** {context.user_data['phone']}
🔢 **Quantity:** {quantity}
💰 **Total Price:** ${total:.2f}

**Do you confirm this order?**"""
    
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
    
    summary = f"""📋 **Order Summary:**

{context.user_data.get('product_emoji', '📚')} **Book:** {product_name}
👤 **Name:** {context.user_data['name']}
👥 **Group:** {context.user_data['group']}
📞 **Phone:** {context.user_data['phone']}
🔢 **Quantity:** {quantity}
💰 **Total Price:** ${total:.2f}

**Do you confirm this order?**"""
    
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
                
                caption = f"""📸 **KHQR for Payment**

Order Code: **#{order_id}**
Total Price: **${context.user_data['price'] * context.user_data['quantity']:.2f}**

**Please scan QR code above to pay**
Or click ABA Pay link below👇"""
                
                await query.message.reply_photo(
                    photo=photo,
                    caption=caption,
                    reply_markup=get_payment_keyboard(order_id),
                    parse_mode='Markdown'
                )
                
                # Send payment instructions separately
                payment_text = f"""💰 **Additional Payment Info:**

1. **KHQR** (image above): Scan QR code via ATM or phone
2. **ABA Pay**: [Click here to pay via ABA]({ABA_PAY_URL})
3. **Pay at Class**: For people no bank account

⚠️ **Important Warning:**
- After payment, send payment screenshot to admin
- No refund for fake payment screenshot
- Money will transfer to admin

Your Order Code: **#{order_id}**
Total Price: **${context.user_data['price'] * context.user_data['quantity']:.2f}**"""
                
                await query.message.reply_text(
                    payment_text,
                    reply_markup=get_payment_keyboard(order_id),
                    parse_mode='Markdown',
                    disable_web_page_preview=True
                )
        except Exception as e:
            logger.error(f"Error sending KHQR: {e}")
            # Fallback to text if image fails
            payment_text = f"""💰 **Payment Methods:**

1. **KHQR**: {KHQR_URL}
2. **ABA Pay**: [Click here]({ABA_PAY_URL})
3. **Pay at Class**

Order Code: **#{order_id}**"""
            
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
            "✏️ What you want to edit?\n\n"
            "Type /cancel to start again",
            reply_markup=get_main_keyboard()
        )
        return ConversationHandler.END
    
    elif query.data == "cancel_order":
        await query.edit_message_text(
            "❌ **Order cancelled.**\n\n"
            "You can start again anytime.",
            reply_markup=get_main_keyboard(),
            parse_mode='Markdown'
        )
        return ConversationHandler.END

async def notify_admin_new_order(context, order_id):
    """Notify admin about new order"""
    try:
        admin_text = f"""🛎️ **New Order!**

📋 **Code:** #{order_id}
👤 **Buyer:** {context.user_data['name']}
👥 **Group:** {context.user_data['group']}
📞 **Phone:** {context.user_data['phone']}
📚 **Book:** {context.user_data['product_name']}
🔢 **Quantity:** {context.user_data['quantity']}
💰 **Total Price:** ${context.user_data['price'] * context.user_data['quantity']:.2f}

🆔 **User ID:** {context.user_data.get('user_id', 'N/A')}
⏰ **Time:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"""

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
                
                caption = f"""📸 **KHQR for Payment**

Order Code: **#{order_id}**

**Please scan QR code above to pay**
After payment, send payment screenshot to admin."""
                
                await query.message.reply_photo(
                    photo=photo,
                    caption=caption,
                    parse_mode='Markdown'
                )
                
                # Update order payment method
                update_order_payment(order_id, "KHQR")
                
                await query.message.reply_text(
                    f"✅ **Selected KHQR Payment**\n\n"
                    f"Order Code: **#{order_id}**\n"
                    f"Please send payment screenshot after payment.",
                    parse_mode='Markdown'
                )
        except:
            await query.message.reply_text(
                f"📸 **KHQR for Payment**\n\n"
                f"{KHQR_URL}\n\n"
                f"Order Code: **#{order_id}**",
                parse_mode='Markdown'
            )
    
    elif query.data.startswith("pay_cash_"):
        order_id = query.data.split("_")[2]
        update_order_payment(order_id, "Cash")
        
        await query.message.reply_text(
            f"💵 **Selected Pay at Class**\n\n"
            f"Order Code: **#{order_id}**\n\n"
            f"Please contact admin in class to pay cash.\n"
            f"Payment via: Admin in class",
            parse_mode='Markdown'
        )
    
    elif query.data.startswith("upload_proof_"):
        order_id = query.data.split("_")[2]
        context.user_data['awaiting_proof_for'] = order_id
        
        await query.message.reply_text(
            f"📎 **Please send payment screenshot**\n\n"
            f"Order Code: **#{order_id}**\n\n"
            f"Please send payment screenshot image.\n"
            f"Or type /cancel to cancel.",
            parse_mode='Markdown'
        )
    
    elif query.data == "back_to_main":
        await query.edit_message_text(
            "Back to main menu",
            reply_markup=get_main_keyboard()
        )

# ========== PAYMENT PROOF HANDLING ==========
async def handle_payment_proof(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle payment proof submission"""
    order_id = context.user_data.get('awaiting_proof_for')
    
    if not order_id:
        await update.message.reply_text(
            "Please select 'Send payment screenshot' from order menu first.",
            reply_markup=get_main_keyboard()
        )
        return
    
    if update.message.photo:
        # Forward to admin
        photo_id = update.message.photo[-1].file_id
        caption = f"📸 **Payment Screenshot**\n\nOrder Code: #{order_id}\nBuyer: {update.effective_user.first_name}"
        
        try:
            # Forward to admin
            await context.bot.send_photo(
                ADMIN_ID,
                photo=photo_id,
                caption=caption,
                parse_mode='Markdown'
            )
            
            # Update order
            update_order_payment(order_id, "Bank Transfer", "proof_provided")
            
            await update.message.reply_text(
                f"✅ **Payment screenshot received!**\n\n"
                f"Order Code: **#{order_id}**\n"
                f"We will check and notify you soon.\n\n"
                f"Thank you for payment!",
                reply_markup=get_main_keyboard(),
                parse_mode='Markdown'
            )
            
            # Notify admin to review
            await context.bot.send_message(
                ADMIN_ID,
                f"🔔 **Screenshot Waiting for Review**\n\n"
                f"Order Code: #{order_id}\n"
                f"Click /admin to review and confirm payment.",
                parse_mode='Markdown'
            )
            
            # Clear the awaiting proof state
            context.user_data.pop('awaiting_proof_for', None)
            
        except Exception as e:
            logger.error(f"Error forwarding proof: {e}")
            await update.message.reply_text(
                "❌ Error sending screenshot. Please try again."
            )
    else:
        await update.message.reply_text(
            "Please send payment screenshot image."
        )

# ========== ORDER TRACKING ==========
async def track_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user's order history"""
    orders = get_user_orders(update.effective_user.id)
    
    if not orders:
        await update.message.reply_text(
            "📭 **You don't have any orders yet.**\n\n"
            "Click '📚 ទិញសៀវភៅ' to start ordering!",
            reply_markup=get_main_keyboard(),
            parse_mode='Markdown'
        )
        return
    
    response = "📋 **Your Order History:**\n\n"
    
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
            'pending': 'Waiting Payment',
            'awaiting_verification': 'Checking Screenshot',
            'confirmed': 'Confirmed',
            'rejected': 'Rejected',
            'completed': 'Completed'
        }
        
        response += f"""**{icon} Code: #{order_id}**
📚 Book: {product}
🔢 Quantity: {qty}
💰 Price: ${total:.2f}
📊 Status: {status_text.get(status, status)}
💳 Payment: {payment_method or 'Not selected'}
📅 Date: {date}
"""
        
        if notes:
            response += f"📝 Note: {notes}\n"
        
        response += "────────────────────\n"
    
    if len(orders) > 10:
        response += f"\n... and {len(orders)-10} more orders"
    
    keyboard = [
        [InlineKeyboardButton("📚 Buy More Books", callback_data="choose_product")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="back_to_main")]
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
    
    admin_text = f"""👑 **Admin Panel**

📊 **Statistics:**
• Total Orders: {stats['total_orders']}
• Waiting: {stats['status_counts'].get('pending', 0) + stats['status_counts'].get('awaiting_verification', 0)}
• Today Orders: {stats['today_orders']}
• Total Revenue: ${stats['revenue']:.2f}
• Total Users: {stats['total_users']}

**Functions:**"""
    
    await update.message.reply_text(admin_text, 
                                   reply_markup=get_admin_keyboard(),
                                   parse_mode='Markdown')

@admin_only
async def handle_admin_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin commands"""
    text = update.message.text
    
    if text == "📊 Statistics":
        await show_admin_stats(update, context)
    elif text == "📋 View All Orders":
        await show_admin_orders_filter(update, context)
    elif text == "⏳ Pending Orders":
        await show_admin_orders(update, context, page=1, status_filter='pending')
    elif text == "📸 Verify Payments":
        await show_admin_orders(update, context, page=1, status_filter='awaiting_verification')
    elif text == "📥 Download Excel":
        await show_export_options(update, context)
    elif text == "👥 View Users":
        await show_admin_users(update, context, page=1)
    elif text == "🔙 Main Menu":
        await update.message.reply_text(
            "Back to main menu",
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
    
    stats_text = f"""📊 **Detailed Statistics**

**📈 Orders Overview:**
• Total Orders: {total_orders}
• Today: {stats['today_orders']} orders (${stats['today_revenue']:.2f})
• This Week: {get_orders_count(date_filter='week')}
• This Month: {get_orders_count(date_filter='month')}

**📊 Order Status:**
• ⏳ Pending: {pending} ({pending_pct:.1f}%)
• 📸 Verifying: {verifying} ({verifying_pct:.1f}%)
• ✅ Completed: {completed} ({completed_pct:.1f}%)
• ❌ Rejected: {stats['status_counts'].get('rejected', 0)}

**💰 Financial:**
• Total Revenue: ${stats['revenue']:.2f}
• Avg Order Value: ${(stats['revenue']/completed if completed > 0 else 0):.2f}
• Today Revenue: ${stats['today_revenue']:.2f}

**👥 Users:**
• Total Users: {stats['total_users']}
• Avg Orders per User: {(total_orders/stats['total_users'] if stats['total_users'] > 0 else 0):.1f}

**📚 Product Sales:**
"""
    
    for product, sold in stats['product_sales'][:10]:
        stats_text += f"• {product}: {sold} sold\n"
    
    if len(stats['product_sales']) > 10:
        stats_text += f"• ... and {len(stats['product_sales']) - 10} more products\n"
    
    keyboard = [
        [InlineKeyboardButton("📋 View Orders", callback_data="admin_orders_1_all_none")],
        [InlineKeyboardButton("👥 View Users", callback_data="admin_users_1")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_back")]
    ]
    
    await update.message.reply_text(
        stats_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def show_admin_orders_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show order filter options"""
    await update.message.reply_text(
        "🔍 **Select Filter for Orders:**\n\n"
        "Choose filter to view orders:",
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
        no_orders_text = "📭 **No orders found**"
        if search_query:
            no_orders_text += f" for search: {search_query}"
        elif status_filter != 'all':
            status_text = {
                'pending': '⏳ Pending',
                'awaiting_verification': '📸 Verifying',
                'completed': '✅ Completed',
                'rejected': '❌ Rejected'
            }
            no_orders_text += f" with status: {status_text.get(status_filter, status_filter)}"
        elif date_filter:
            date_text = {
                'today': '📅 Today',
                'week': '📅 This Week',
                'month': '📅 This Month'
            }
            no_orders_text += f" for period: {date_text.get(date_filter, date_filter)}"
        
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="admin_back")]]
        
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
        response = f"🔍 **Search Results: '{search_query}'**\n\n"
    else:
        filter_text = ""
        if status_filter != 'all':
            status_text = {
                'pending': '⏳ Pending',
                'awaiting_verification': '📸 Verifying',
                'completed': '✅ Completed',
                'rejected': '❌ Rejected'
            }
            filter_text = f" • Status: {status_text.get(status_filter, status_filter)}"
        
        if date_filter:
            date_text = {
                'today': '📅 Today',
                'week': '📅 This Week',
                'month': '📅 This Month'
            }
            filter_text += f" • Period: {date_text.get(date_filter, date_filter)}"
        
        response = f"📋 **All Orders**{filter_text}\n\n"
        response += f"📄 **Page {page}/{total_pages}** • **Total: {total_orders} orders**\n\n"
    
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
💳 {payment_method or 'No method'} • 📅 {date.split()[0]}
────────────────────
"""
    
    # Add pagination info
    if total_pages > 1:
        response += f"\n📄 **Page {page} of {total_pages}** • **{total_orders} total orders**"
    
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
            "🔍 **Search Orders**\n\n"
            "Please type search keyword:\n"
            "(Search by Order ID, Name, Group, or Product)",
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
        no_users_text = "👥 **No users found**"
        if search_query:
            no_users_text += f" for search: {search_query}"
        
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="admin_back")]]
        
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
        response = f"🔍 **User Search: '{search_query}'**\n\n"
    else:
        response = f"👥 **All Users**\n\n"
        response += f"📄 **Page {page}/{total_pages}** • **Total: {total_users} users**\n\n"
    
    # Add users
    for user in users:
        user_id, first_name, group_name, phone, reg_date, total_orders, total_spent = user
        
        # Shorten long names
        display_name = first_name[:15] + "..." if len(first_name) > 15 else first_name
        display_group = group_name[:10] + "..." if group_name and len(group_name) > 10 else (group_name or "N/A")
        
        response += f"""**👤 {display_name}** ({display_group})
🆔 {user_id} • 📞 {phone or 'N/A'}
📦 Orders: {total_orders} • 💰 Spent: ${total_spent:.2f}
📅 Joined: {reg_date.split()[0]}
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
        InlineKeyboardButton("🔍 Search Users", callback_data="admin_search_users"),
        InlineKeyboardButton("📥 Export Users", callback_data="admin_export_users")
    ])
    
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_back")])
    
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
            "🔍 **Search Users**\n\n"
            "Please type search keyword:\n"
            "(Search by Name, Group, or Phone)",
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
            InlineKeyboardButton("📋 All Orders", callback_data="export_all"),
            InlineKeyboardButton("⏳ Pending", callback_data="export_pending")
        ],
        [
            InlineKeyboardButton("📸 Verifying", callback_data="export_awaiting_verification"),
            InlineKeyboardButton("✅ Completed", callback_data="export_completed")
        ],
        [
            InlineKeyboardButton("📅 Today", callback_data="export_today"),
            InlineKeyboardButton("📅 This Week", callback_data="export_week")
        ],
        [
            InlineKeyboardButton("📅 This Month", callback_data="export_month"),
            InlineKeyboardButton("👥 Users List", callback_data="export_users")
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_back")]
    ]
    
    await update.message.reply_text(
        "📥 **Export Data to Excel**\n\n"
        "Choose what data to export:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def export_orders_admin(query, context: ContextTypes.DEFAULT_TYPE, status_filter='all', date_filter=None):
    """Export orders to Excel for admin"""
    await query.answer("⏳ Preparing Excel file...")
    
    try:
        filename = export_to_excel(status_filter, date_filter)
        
        # Create filter description
        filter_desc = ""
        if status_filter != 'all':
            filter_desc += f"Status: {status_filter} • "
        if date_filter:
            filter_desc += f"Period: {date_filter} • "
        
        caption = f"📥 **Exported Orders**\n\n{filter_desc}Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        
        await context.bot.send_document(
            chat_id=ADMIN_ID,
            document=open(filename, 'rb'),
            caption=caption,
            parse_mode='Markdown'
        )
        
        os.remove(filename)
        
        await query.message.reply_text("✅ Excel file sent to your chat!")
        
    except Exception as e:
        logger.error(f"Error exporting: {e}")
        await query.message.reply_text(f"❌ Error: {str(e)}")

async def export_users_admin(query, context: ContextTypes.DEFAULT_TYPE):
    """Export users to Excel"""
    await query.answer("⏳ Preparing users Excel file...")
    
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
        
        caption = f"👥 **Exported Users List**\n\nTotal Users: {len(users)}\nDate: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        
        await context.bot.send_document(
            chat_id=ADMIN_ID,
            document=open(filename, 'rb'),
            caption=caption,
            parse_mode='Markdown'
        )
        
        os.remove(filename)
        
        await query.message.reply_text("✅ Users Excel file sent to your chat!")
        
    except Exception as e:
        logger.error(f"Error exporting users: {e}")
        await query.message.reply_text(f"❌ Error: {str(e)}")

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
                'pending': '⏳ Waiting Payment',
                'awaiting_verification': '📸 Checking Screenshot',
                'confirmed': '✅ Confirmed',
                'rejected': '❌ Rejected',
                'completed': '🎉 Completed'
            }
            
            response = f"""📋 **Order Details:**

**Code:** #{order_id}
**Buyer:** {first_name}
**Group:** {group_name}
**Phone:** {phone}
**Telegram:** @{username if username else 'N/A'}
**Book:** {product_name}
**Quantity:** {quantity}
**Total Price:** ${total_price:.2f}
**Status:** {status_text.get(status, status)}
**Payment:** {payment_method or 'Not selected'}
**Date:** {order_date}
**Note:** {admin_notes or 'None'}"""
            
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
        user_id = update_order_status(order_id, 'confirmed', 'Confirmed by admin')
        
        await query.edit_message_text(f"✅ **Order #{order_id} confirmed!**", parse_mode='Markdown')
        
        # Notify user
        try:
            await context.bot.send_message(
                user_id,
                f"✅ **Your order confirmed!**\n\n"
                f"Order Code: **#{order_id}**\n"
                f"Thank you for buying!",
                parse_mode='Markdown'
            )
        except:
            pass
        
    elif query.data.startswith("admin_reject_"):
        order_id = int(query.data.split("_")[2])
        user_id = update_order_status(order_id, 'rejected', 'Rejected by admin')
        
        await query.edit_message_text(f"❌ **Order #{order_id} rejected!**", parse_mode='Markdown')
        
        # Notify user
        try:
            await context.bot.send_message(
                user_id,
                f"❌ **Your order rejected!**\n\n"
                f"Order Code: **#{order_id}**\n"
                f"Please contact admin if have question.",
                parse_mode='Markdown'
            )
        except:
            pass
        
    elif query.data.startswith("admin_complete_"):
        order_id = int(query.data.split("_")[2])
        user_id = update_order_status(order_id, 'completed', 'Completed by admin')
        
        await query.edit_message_text(f"🎉 **Order #{order_id} completed!**", parse_mode='Markdown')
        
        # Notify user
        try:
            await context.bot.send_message(
                user_id,
                f"🎉 **Your order completed!**\n\n"
                f"Order Code: **#{order_id}**\n"
                f"Thank you for buying! Please buy again later.",
                parse_mode='Markdown'
            )
        except:
            pass
    
    elif query.data.startswith("admin_contact_"):
        order_id = int(query.data.split("_")[2])
        order = get_order_details(order_id)
        
        if order:
            _, user_id, _, _, _, _, _, _, _, _, first_name, _, phone, username = order
            
            contact_info = f"""📞 **Contact Info:**

Order Code: #{order_id}
Name: {first_name}
Phone: {phone}
Telegram: @{username if username else 'None'}
User ID: {user_id}

Click below to contact:"""
            
            keyboard = []
            if username:
                keyboard.append([InlineKeyboardButton("💬 Chat via Telegram", url=f"https://t.me/{username}")])
            
            keyboard.append([
                InlineKeyboardButton("📝 Send Message", callback_data=f"admin_message_{user_id}"),
                InlineKeyboardButton("🔙 Back", callback_data=f"admin_view_{order_id}")
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
            f"📝 **Add note for Order #{order_id}**\n\n"
            f"Please type your note:",
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
            f"✅ **Added note for Order #{order_id}**",
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
            "❌ **Error occurred.** Please try again or contact developer!",
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
                CallbackQueryHandler(confirm_order, pattern="^(confirm_order|edit_order|cancel_order)$"),
            ]
        },
        fallbacks=[
            CommandHandler('cancel', 
                         lambda u,c: (u.message.reply_text("❌ Order cancelled", 
                                                         reply_markup=get_main_keyboard()),
                                     ConversationHandler.END))
        ],
        allow_reentry=True
    )
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(CommandHandler("cancel", 
                                          lambda u,c: u.message.reply_text("✅ Cancelled",
                                                                         reply_markup=get_main_keyboard())))
    
    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(handle_payment_option, pattern="^(pay_|upload_proof_|back_to_main)"))
    application.add_handler(CallbackQueryHandler(handle_admin_orders_navigation, pattern="^(admin_orders_|filter_|admin_search|admin_stats|admin_export_|admin_back)"))
    application.add_handler(CallbackQueryHandler(handle_admin_users_navigation, pattern="^(admin_users_|admin_search_users|admin_export_users)"))
    application.add_handler(CallbackQueryHandler(handle_admin_order_action, pattern="^admin_(view|confirm|reject|complete|contact|note)"))
    application.add_handler(CallbackQueryHandler(handle_export_command, pattern="^export_"))
    
    application.add_handler(MessageHandler(filters.PHOTO, handle_payment_proof))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_note))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_search))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_user_search))
    
    application.add_handler(MessageHandler(
        filters.TEXT & filters.Regex("^(📚 ទិញសៀវភៅ|📋 តាមដានការកម្មង|❓ Q&A|👤 អំពីយើង|👑 Admin Panel)$"),
        handle_main_menu
    ))
    
    application.add_handler(MessageHandler(
        filters.TEXT & filters.Regex("^(📊 Statistics|📋 View All Orders|⏳ Pending Orders|📸 Verify Payments|📥 Download Excel|👥 View Users|🔙 Main Menu)$"),
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
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()